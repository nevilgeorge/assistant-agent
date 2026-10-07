"""Completed Claude content logs preserve streaming and exclude configuration."""

import asyncio
import io
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from assistant_agent import sandbox
from assistant_agent.chat import claude_process, debug_logging
from assistant_agent.chat.conversation import Conversation


class RecordedStream:
    """Feed Docker chunks and finish without simulating an unexpected disconnect."""

    def __init__(self, chunks: list[tuple[int, bytes]]) -> None:
        self.chunks = iter(chunks)
        self.process: claude_process.ClaudeProcess | None = None
        self.writes: list[bytes] = []

    async def read_out(self) -> SimpleNamespace | None:
        await asyncio.sleep(0)
        chunk = next(self.chunks, None)
        if chunk is None:
            assert self.process is not None
            self.process.is_closed = True
            return None
        stream, data = chunk
        return SimpleNamespace(stream=stream, data=data)

    async def write_in(self, data: bytes) -> None:
        self.writes.append(data)


def make_process(
    chunks: list[tuple[int, bytes]], *, conversation_id: str = "conversation",
    container_id: str = "container",
) -> claude_process.ClaudeProcess:
    """Create a reader with a synthetic sandbox and deterministic stream."""
    handle = sandbox.SandboxHandle(
        container_id, "user", conversation_id, Path("/tmp"), Path("/tmp")
    )
    process = claude_process.ClaudeProcess(SimpleNamespace(), handle)
    stream = RecordedStream(chunks)
    stream.process = process
    process.docker_stream = stream
    return process


def make_conversation(process: claude_process.ClaudeProcess, turn_id: str = "turn") -> Conversation:
    """Attach a synthetic process to an active conversation without starting Claude."""
    conversation = Conversation("user", SimpleNamespace(), asyncio.create_task)
    conversation.conversation_id = process.sandbox_handle.conversation_id
    conversation.sandbox_handle = process.sandbox_handle
    conversation.claude_process = process
    conversation.active_turn_id = turn_id
    conversation.transcript_messages = [{"role": "assistant", "text": ""}]
    conversation.turn_started_monotonic = time.monotonic()
    conversation.turn_timeout_handle = SimpleNamespace(cancel=lambda: None)
    return conversation


def debug_records(output: io.StringIO) -> list[dict[str, Any]]:
    """Decode each newline-delimited JSON record emitted to the debug sink."""
    return [json.loads(line) for line in output.getvalue().splitlines()]


def event_bytes(event: dict[str, Any]) -> bytes:
    """Encode a CLI event without ASCII escaping so Unicode crosses chunk boundaries."""
    return (json.dumps(event, ensure_ascii=False) + "\n").encode()


@pytest.fixture
def debug_output(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Capture dedicated debug records independently from the application logger."""
    output = io.StringIO()
    debug_logger = logging.Logger("test.claude_debug", level=logging.INFO)
    debug_logger.addHandler(logging.StreamHandler(output))
    debug_logger.propagate = False
    monkeypatch.setattr(debug_logging, "_get_debug_logger", lambda: debug_logger)
    monkeypatch.delenv("APP_ENV", raising=False)
    return output


@pytest.mark.parametrize("setting,enabled", [
    (None, False), ("", False), ("false", False), ("0", False), ("yes", False),
    ("true", True), ("1", True),
])
async def test_debug_switch_is_read_for_each_conversation(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO,
    setting: str | None, enabled: bool,
) -> None:
    if setting is None:
        monkeypatch.delenv("CLAUDE_DEBUG_STREAM", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_DEBUG_STREAM", setting)
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": "response"}]}}
    process = make_process([(1, event_bytes(event)), (2, b"diagnostic")])
    conversation = make_conversation(process)
    received, failed = [], []

    def receive(event: dict[str, Any]) -> None:
        received.append(event)
        conversation.receive(event)

    await process._read(receive, failed.append)
    assert conversation.debug_stream_enabled is enabled
    assert received == [event] and not failed
    assert bool(debug_output.getvalue()) is enabled


@pytest.mark.parametrize("setting", ["true", "1"])
@pytest.mark.parametrize("app_env", ["production", " production "])
async def test_production_disables_debug(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO, setting: str, app_env: str,
) -> None:
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", setting)
    monkeypatch.setenv("APP_ENV", app_env)
    process = make_process([(1, event_bytes({"type": "result", "result": "private"})),
                            (2, b"private stderr")])
    conversation = make_conversation(process)
    conversation.receive({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "private"},
    ]}})
    await process._read(conversation.receive, pytest.fail)
    assert not conversation.debug_stream_enabled
    assert debug_output.getvalue() == ""


async def test_completed_content_and_unicode_stdout_with_ignored_stderr(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO,
) -> None:
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", "true")
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "response 🌍\nsecond line"},
            {"type": "thinking", "thinking": "emitted thinking"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/input/mail"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "full tool output"},
        ]}},
        {"type": "stream_event", "event": {"delta": {
            "type": "thinking_delta", "thinking": "partial thought",
        }}},
        {"type": "result", "result": "complete result"},
    ]
    text = events[0]["message"]["content"][0]["text"]
    events.insert(0, {"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": text},
    }})
    events[-1]["subtype"] = "success"
    first_event = event_bytes(events[0])
    unicode_start = first_event.index("🌍".encode())
    process = make_process([
        (1, first_event[:unicode_start + 1]),
        (2, b"stderr \xf0\x9f"),
        (1, first_event[unicode_start + 1:]),
        (2, b"\x8c\x8d\ninvalid \xff tail \xe2"),
        (1, b"".join(event_bytes(event) for event in events[1:])),
    ])
    conversation = make_conversation(process)
    received, failed = [], []

    def receive(event: dict[str, Any]) -> None:
        received.append(event)
        conversation.receive(event)

    await process._read(receive, failed.append)
    records = debug_records(debug_output)
    assert received == events and not failed
    stdout_records = [record for record in records if record["stream"] == "stdout"]
    assert [record["event"] for record in stdout_records] == [
        {"type": event["type"], "content": event["message"]["content"]}
        for event in events[1:3]
    ]
    assert all(record["turn_id"] == "turn" for record in stdout_records)
    assert conversation.transcript_messages[-1]["text"] == text
    assert [event["type"] for event in conversation.replay_events] == [
        "assistant_delta", "turn_completion",
    ]
    assert len(records) == 2
    assert all(record["stream"] == "stdout" for record in records)
    assert all(record["conversation_id"] == "conversation" for record in records)
    assert all(record["container_id"] == "container" for record in records)


async def test_excludes_configuration_control_and_outbound_prompts(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO,
) -> None:
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", "true")
    system = {"type": "system", "subtype": "init", "api_key": "config-secret"}
    control = {"type": "control_response", "response": {
        "subtype": "success", "request_id": "unknown", "response": {"token": "token-secret"},
    }}
    process = make_process([(1, event_bytes(system) + event_bytes(control))])
    await process.send("outbound-secret")
    received, failed = [], []
    await process._read(received.append, failed.append)
    assert received == [system] and not failed
    assert debug_output.getvalue() == ""


@pytest.mark.parametrize("event_type", ["control_request", "control_cancel_request"])
async def test_unexpected_control_requests_are_not_debug_logged(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO, event_type: str,
) -> None:
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", "true")
    process = make_process([(1, event_bytes({"type": event_type, "secret": "control-secret"}))])
    process.request_close = lambda: setattr(process, "is_closed", True)
    received, failed = [], []
    await process._read(received.append, failed.append)
    assert not received and len(failed) == 1
    assert process.transport_error is not None
    assert debug_output.getvalue() == ""


async def test_concurrent_conversations_tag_each_record(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO,
) -> None:
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", "1")
    processes = [make_process(
        [(1, event_bytes({"type": "assistant", "message": {"content": [
            {"type": "text", "text": str(index)},
        ]}})),
         (2, str(index).encode())],
        conversation_id=f"conversation-{index}", container_id=f"container-{index}",
    ) for index in range(2)]
    conversations = [make_conversation(process, f"turn-{index}")
                     for index, process in enumerate(processes)]
    await asyncio.gather(*(process._read(conversation.receive, pytest.fail)
                          for process, conversation in zip(processes, conversations, strict=True)))
    records = [json.loads(line) for line in debug_output.getvalue().splitlines()]
    assert len(records) == 2
    for record in records:
        assert record["stream"] == "stdout"
        content = record["event"]["content"][0]["text"]
        assert record["conversation_id"] == f"conversation-{content}"
        assert record["container_id"] == f"container-{content}"
        assert record["turn_id"] == f"turn-{content}"


@pytest.mark.parametrize("failure_stage", ["configuration", "emission"])
async def test_logging_failure_does_not_interrupt_event_delivery(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO, failure_stage: str,
) -> None:
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", "true")

    def fail_logging(*args: Any) -> None:
        """Simulate an unavailable logging sink or logger configuration."""
        raise RuntimeError("sink")

    debug_logger = SimpleNamespace(info=fail_logging)
    monkeypatch.setattr(debug_logging, "_get_debug_logger", (
        fail_logging if failure_stage == "configuration" else lambda: debug_logger
    ))
    events = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "response"}]}},
              {"type": "result", "subtype": "success", "result": "done"}]
    process = make_process([(1, event_bytes(events[0])), (2, b"diagnostic"),
                            (1, event_bytes(events[1]))])
    conversation = make_conversation(process)
    received, failed = [], []

    def receive(event: dict[str, Any]) -> None:
        received.append(event)
        conversation.receive(event)

    await process._read(receive, failed.append)
    assert received == events and not failed
    assert process.transport_error is None
    assert conversation.transcript_messages[-1]["text"] == "response"


def test_stderr_handler_failure_does_not_print_logging_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_write(message: str) -> None:
        """Simulate app stderr being unavailable."""
        raise OSError("stderr closed")

    diagnostic_output = io.StringIO()
    monkeypatch.setattr("sys.stderr", diagnostic_output)
    handler = debug_logging._DebugStreamHandler(SimpleNamespace(write=fail_write))
    debug_logger = logging.Logger("test.failed_sink", level=logging.INFO)
    debug_logger.addHandler(handler)
    debug_logger.info("sensitive response")
    assert diagnostic_output.getvalue() == ""


def test_dedicated_logger_is_configured_once_and_writes_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = io.StringIO()
    monkeypatch.setattr("sys.stderr", output)
    fresh_logger = logging.Logger("test.fresh_debug")
    original_get_logger = logging.getLogger
    monkeypatch.setattr(debug_logging.logging, "getLogger", lambda name=None: (
        fresh_logger if name == "assistant_agent.claude_debug_stream" else original_get_logger(name)
    ))
    first_logger = debug_logging._get_debug_logger()
    second_logger = debug_logging._get_debug_logger()
    assert first_logger is second_logger
    assert len(first_logger.handlers) == 1
    assert first_logger.level == logging.INFO
    assert first_logger.propagate is False
    first_logger.info('{"event":"one"}')
    assert output.getvalue().splitlines() == ['{"event":"one"}']


def test_process_has_no_debug_logging_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", "true")
    process = make_process([])
    assert not hasattr(process, "debug_stream_enabled")
    assert not hasattr(process, "conversation_id")
    assert not hasattr(process, "_debug_record")


async def test_transport_output_never_emits_debug_records(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO,
) -> None:
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", "true")
    events = [
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {
            "type": "text_delta", "text": "partial",
        }}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "full"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "tool"}]}},
        {"type": "result", "subtype": "success", "result": "full"},
    ]
    process = make_process([
        (1, b"".join(event_bytes(event) for event in events)),
        (2, "private stderr 🌍\n".encode() + b"\xff"),
    ])
    received: list[dict[str, Any]] = []
    await process._read(received.append, pytest.fail)
    assert received == events
    assert debug_output.getvalue() == ""


async def test_streaming_deltas_keep_ui_events_without_debug_records(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO,
) -> None:
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", "true")
    conversation = make_conversation(make_process([]))
    for text in ["first", " second"]:
        conversation.receive({"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {"type": "text_delta", "text": text},
        }})
    conversation.receive({"type": "result", "subtype": "success", "result": "first second"})
    assert debug_output.getvalue() == ""
    assert conversation.transcript_messages[-1]["text"] == "first second"
    assert [event["type"] for event in conversation.replay_events] == [
        "assistant_delta", "assistant_delta", "turn_completion",
    ]


async def test_result_only_fallback_is_logged_once(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO,
) -> None:
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", "true")
    conversation = make_conversation(make_process([]))
    event = {"type": "result", "subtype": "success", "result": "fallback 🌍\nline two"}
    conversation.receive(event)
    conversation.receive(event)
    records = debug_records(debug_output)
    assert len(records) == 1
    assert records[0]["event"] == {
        "type": "assistant", "content": [{"type": "text", "text": event["result"]}],
    }
    assert records[0]["turn_id"] == "turn"
    assert conversation.transcript_messages[-1]["text"] == event["result"]


@pytest.mark.parametrize("state", ["inactive", "failed", "retired"])
async def test_ignored_turn_messages_are_not_logged(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO, state: str,
) -> None:
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", "true")
    conversation = make_conversation(make_process([]))
    if state == "inactive":
        conversation.active_turn_id = None
    elif state == "failed":
        conversation.has_failed = True
    else:
        conversation.retire()
        conversation.replay_events.clear()
    conversation.receive({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "ignored private thinking"},
    ]}})
    conversation.receive({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "ignored tool output"},
    ]}})
    conversation.receive({"type": "result", "subtype": "success", "result": "ignored"})
    assert debug_output.getvalue() == ""
    assert not conversation.replay_events


async def test_conversation_excludes_system_control_and_failed_results(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO,
) -> None:
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", "true")
    conversation = make_conversation(make_process([]))
    conversation.request_close = lambda: None
    for event in [
        {"type": "system", "subtype": "init", "secret": "configuration"},
        {"type": "control_response", "response": {"secret": "control"}},
        {"type": "control_request", "request": {"secret": "control"}},
        {"type": "result", "subtype": "error", "is_error": True, "result": "private error"},
    ]:
        conversation.receive(event)
    assert debug_output.getvalue() == ""
    assert conversation.has_failed


@pytest.mark.parametrize("setting", ["false", "true"])
async def test_logging_does_not_change_ignored_malformed_message_behavior(
    monkeypatch: pytest.MonkeyPatch, debug_output: io.StringIO, setting: str,
) -> None:
    """Ignore malformed content where the existing UI path does not consume it."""
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", setting)
    conversation = make_conversation(make_process([]))
    conversation.has_streamed_turn_text = True
    conversation.receive({"type": "user", "message": None})
    conversation.receive({"type": "assistant", "message": None})
    assert not conversation.has_failed
    assert not conversation.replay_events
    assert debug_output.getvalue() == ""


def test_unserializable_record_does_not_interrupt_logging(
    debug_output: io.StringIO,
) -> None:
    debug_logging.emit_debug_record({"event": object()})
    assert debug_output.getvalue() == ""
