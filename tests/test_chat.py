import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiodocker.stream import Message

from assistant_agent import chat, sandbox
from assistant_agent.chat import ChatError, ConversationManager


class FakeProcess:
    def __init__(self, token, receive, failed):
        self.receive = receive
        self.failed = failed
        self.prompts = []
        self.closed = False

    async def send(self, prompt):
        self.prompts.append(prompt)

    async def close(self):
        self.closed = True

    @classmethod
    async def create(cls, token, receive, failed, service):
        return cls(token, receive, failed)

    def text(self, text):
        self.receive(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                },
            }
        )

    def finish(self):
        self.receive({"type": "result", "subtype": "success", "result": "fallback"})


@pytest.fixture
async def manager():
    manager = ConversationManager(FakeProcess)
    yield manager
    await manager.close()


async def test_multiple_turns_stream_without_restarting_process(manager):
    first = await manager.submit("alice", "Remember blue")
    conversation = manager.get("alice")
    process = conversation.process
    with pytest.raises(ChatError, match="progress"):
        await manager.submit("alice", "overlap")
    process.text("Bl")
    process.text("ue")
    # Whole assistant events must not duplicate streaming deltas.
    process.receive(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Blue"}]}}
    )
    assert conversation.snapshot()["transcript"][-1]["text"] == "Blue"
    assert conversation.active == first["turn_id"]
    process.finish()
    second = await manager.submit("alice", "What color?")
    assert conversation.process is process and first["turn_id"] != second["turn_id"]
    assert process.prompts == ["Remember blue", "What color?"]
    process.text("Blue again")
    process.finish()
    assert [e["type"] for e in conversation.events].count("turn_completion") == 2
    assert conversation.snapshot()["active_turn"] is None


async def test_isolation_reset_failure_and_capacity(manager):
    manager.max_conversations = 2
    await manager.submit("alice", "private")
    await manager.submit("bob", "other")
    assert manager.get("alice").process is not manager.get("bob").process
    with pytest.raises(ChatError) as exc:
        manager.get("carol")
    assert exc.value.status == 503
    old = manager.get("alice")
    old.process.failed("Ended")
    await old.teardown
    assert old.process.closed and old.failed
    with pytest.raises(ChatError, match="new conversation"):
        await manager.submit("alice", "follow-up")
    await manager.reset("alice")
    assert old.events[-1]["type"] == "conversation_reset"
    assert manager.get("alice").id != old.id
    with pytest.raises(ChatError, match="Reload"):
        await manager.submit("alice", "stale", old.id)


async def test_timeout_does_not_apply_to_later_turn(manager):
    first = await manager.submit("alice", "one")
    conversation = manager.get("alice")
    conversation.process.finish()
    await manager.submit("alice", "two")
    manager._timeout(conversation, first["turn_id"])
    assert not conversation.failed
    manager._timeout(conversation, conversation.active)
    await conversation.teardown
    assert conversation.failed and conversation.process.closed
    assert conversation.events[-1]["type"] == "turn_failure"


async def test_limits_and_shutdown(manager):
    await manager.submit("alice", "one")
    conversation = manager.get("alice")
    conversation.bytes = 2 * 1024 * 1024
    conversation.process.text("overflow")
    await conversation.teardown
    assert conversation.failed and conversation.process.closed
    await manager.close()
    assert not manager.conversations


async def test_result_failure_retains_partial_output(manager):
    await manager.submit("alice", "one")
    conversation = manager.get("alice")
    conversation.process.text("partial")
    conversation.process.receive({"type": "result", "subtype": "error_max_turns", "is_error": True})
    await conversation.teardown
    assert conversation.snapshot()["transcript"][-1]["text"] == "partial"
    assert conversation.failed and conversation.process.closed


class Gate:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.made = []

    async def create(self, token, receive, failed, service):
        self.entered.set()
        await self.release.wait()
        self.made.append(FakeProcess(token, receive, failed))
        return self.made[-1]


async def test_spawn_does_not_block_other_callers(manager):
    manager.factory = gate = Gate()
    task = asyncio.create_task(manager.submit("alice", "one"))
    await gate.entered.wait()
    conversation = manager.get("alice")
    assert manager.get("bob") is not conversation
    assert conversation.snapshot()["active_turn"] is None
    with pytest.raises(ChatError, match="progress"):
        await manager.submit("alice", "two")
    conversation.last_used = 0
    await manager.sweep_once()
    assert manager.conversations["alice"] is conversation
    gate.release.set()
    outcome = await task
    assert outcome["turn_id"] == conversation.active
    assert conversation.process.prompts == ["one"] and not conversation.starting


@pytest.mark.parametrize("shutdown", [False, True], ids=["reset", "shutdown"])
async def test_reset_or_shutdown_during_spawn(manager, shutdown):
    manager.factory = gate = Gate()
    task = asyncio.create_task(manager.submit("alice", "one"))
    await gate.entered.wait()
    conversation = manager.get("alice")
    if shutdown:
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
    else:
        await manager.reset("alice")
    assert "alice" not in manager.conversations
    gate.release.set()
    with pytest.raises(ChatError) as exc:
        await task
    if shutdown:
        await closing
    assert exc.value.status == 503
    assert gate.made[0].closed and gate.made[0].prompts == []
    assert [e["type"] for e in conversation.events] == ["conversation_reset"]
    assert not conversation.starting and not conversation.active


async def test_request_cancellation_preserves_reserved_submission(manager):
    manager.factory = gate = Gate()
    task = asyncio.create_task(manager.submit("alice", "one"))
    await gate.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.release.set()
    await asyncio.gather(*manager.work)
    conversation = manager.get("alice")
    assert conversation.process.prompts == ["one"] and not conversation.starting
    assert not manager.work


async def test_cancel_shutdown_still_joins_startup(manager):
    manager.factory = gate = Gate()
    task = asyncio.create_task(manager.submit("alice", "one"))
    await gate.entered.wait()
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    gate.release.set()
    with pytest.raises(ChatError):
        await task
    await manager.close()
    assert gate.made[0].closed and not manager.work


async def test_transport_failure_during_spawn(manager):
    made = []

    class Factory:
        @staticmethod
        async def create(token, receive, failed, service):
            made.append(FakeProcess(token, receive, failed))
            failed("Ended")
            return made[-1]

    manager.factory = Factory
    with pytest.raises(ChatError, match="conversation ended"):
        await manager.submit("alice", "one")
    conversation = manager.get("alice")
    assert made[0].closed and not made[0].prompts
    assert [e["type"] for e in conversation.events] == ["turn_failure"]
    assert not conversation.starting


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_spawn_failure_releases_reservation(manager, error):
    manager.factory = SimpleNamespace(create=AsyncMock(side_effect=error()))
    with pytest.raises(ChatError if error is RuntimeError else error):
        await manager.submit("alice", "one")
    conversation = manager.get("alice")
    assert not conversation.starting and not conversation.events
    assert conversation.failed is (error is RuntimeError)


async def test_cleanup_runs_once_before_any_spawn(manager, monkeypatch):
    order = []

    async def cleanup(service):
        order.append("cleanup")
        await asyncio.sleep(0.01)

    class Factory(FakeProcess):
        @classmethod
        async def create(cls, *args):
            order.append("spawn")
            return await super().create(*args)

    monkeypatch.setattr(chat, "cleanup_orphans", cleanup)
    manager.factory = Factory
    manager.needs_cleanup = True
    await asyncio.gather(*(manager.submit(user, "one") for user in ("alice", "bob")))
    assert order == ["cleanup", "spawn", "spawn"] and not manager.needs_cleanup


async def test_failed_cleanup_blocks_spawn_and_is_retried(manager, monkeypatch):
    cleanup = AsyncMock(side_effect=RuntimeError("sandbox down"))
    monkeypatch.setattr(chat, "cleanup_orphans", cleanup)
    factory = AsyncMock()
    manager.factory = SimpleNamespace(create=factory)
    manager.needs_cleanup = True
    with pytest.raises(ChatError, match="unavailable"):
        await manager.submit("alice", "one")
    assert manager.needs_cleanup and not factory.called
    await manager.reset("alice")
    cleanup.side_effect = None
    manager.factory = FakeProcess
    await manager.submit("alice", "two")
    assert cleanup.await_count == 2 and not manager.needs_cleanup


async def test_idle_conversations_expire_but_active_conversations_stay(manager):
    await manager.submit("active", "one")
    idle = manager.get("idle")
    idle.last_used = 0
    await manager.sweep_once()
    assert "idle" not in manager.conversations
    assert idle.events[-1]["type"] == "conversation_reset"
    assert "active" in manager.conversations


class FakeStream:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.writes = []
        self.closed = 0
        self.opened = False

    async def __aenter__(self):
        self.opened = True
        return self

    async def read_out(self):
        return await self.messages.get()

    async def write_in(self, data):
        self.writes.append(data)

    async def close(self):
        self.closed += 1


@pytest.fixture
async def transport():
    stream = FakeStream()
    service = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(start=lambda: stream)),
        run=AsyncMock(return_value=sandbox.ExecResult(0, "")),
    )
    received, failures = [], []
    process = await chat.ClaudeProcess.create("test", received.append, failures.append, service)
    yield process, stream, service, received, failures
    await process.close()
    assert process.reader.done() and stream.closed == 1


async def test_transport_reads_split_unicode_json_and_drains_stderr(transport):
    process, stream, service, received, failures = transport
    assert stream.opened
    await process.send("雪 $(touch nope)")
    assert json.loads(stream.writes[0])["message"]["content"] == "雪 $(touch nope)"
    payload = json.dumps({"type": "assistant", "text": "雪"}, ensure_ascii=False).encode() + b"\n"
    stream.messages.put_nowait(Message(2, b"private diagnostic"))
    for byte in payload:
        stream.messages.put_nowait(Message(1, bytes([byte])))
    await asyncio.sleep(0)
    assert received == [{"type": "assistant", "text": "雪"}] and not failures
    await asyncio.gather(process.close(), process.close())
    assert "kill -TERM" in service.run.call_args[0][0]
    assert service.run.await_count == 2


@pytest.mark.parametrize(
    "message",
    [
        None,
        Message(1, b"not JSON\n"),
        Message(9, b"bad"),
        Message(1, b"x" * (chat.PROTOCOL_LIMIT + 1)),
        Message(1, b"x" * (chat.PROTOCOL_LIMIT + 1) + b"\n"),
    ],
)
async def test_transport_invalid_output_fails_and_cleans_up(transport, message):
    process, stream, service, received, failures = transport
    stream.messages.put_nowait(message)
    await asyncio.sleep(0)
    await process.close()
    assert len(failures) == 1 and not received


async def test_send_failure_retains_partial_output_and_closes(manager):
    await manager.submit("alice", "one")
    conversation = manager.get("alice")
    conversation.process.text("partial")
    conversation.process.finish()
    conversation.process.send = AsyncMock(side_effect=OSError("closed"))
    await manager.submit("alice", "two")
    await conversation.teardown
    assert conversation.failed and conversation.process.closed
    assert conversation.transcript[1]["text"] == "partial"


@pytest.mark.parametrize("cancel", [False, True])
async def test_startup_failure_or_cancellation_releases_attachment(cancel):
    stream = FakeStream()
    entered = asyncio.Event()

    async def readiness(*args, **kwargs):
        if "kill -TERM" in args[0]:
            return sandbox.ExecResult(0, "")
        entered.set()
        if cancel:
            await asyncio.Future()
        return sandbox.ExecResult(1, "")

    service = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(start=lambda: stream)), run=readiness
    )
    task = asyncio.create_task(
        chat.ClaudeProcess.create("test", lambda m: None, lambda m: None, service)
    )
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else sandbox.SandboxError):
        await task
    assert stream.closed == 1


@pytest.mark.skipif(os.getenv("CHAT_DOCKER_INTEGRATION") != "1", reason="Opt-in real Claude test")
async def test_real_claude_multiple_turns():
    manager = ConversationManager()
    try:
        await manager.start()
        await manager.submit("integration", "Remember the codeword cobalt. Reply with only OK.")
        conversation = manager.get("integration")
        async with asyncio.timeout(125):
            while conversation.active:
                await asyncio.sleep(0.1)
        assert not conversation.failed
        process = conversation.process
        await manager.submit(
            "integration", "What codeword did I give you? Reply with only that word."
        )
        async with asyncio.timeout(125):
            while conversation.active:
                await asyncio.sleep(0.1)
        assert not conversation.failed and conversation.process is process
        assert "cobalt" in conversation.transcript[-1]["text"].lower()
        assert any(e["type"] == "assistant_delta" for e in conversation.events)
        await manager.reset("integration")
        assert process.closed
        assert not (await process.execution.inspect())["Running"]
    finally:
        await manager.close()


async def test_shutdown_joins_cleanup_of_conversation_removed_by_sweeper(manager):
    await manager.submit("alice", "one")
    conversation = manager.get("alice")
    conversation.process.finish()
    conversation.last_used = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()
        conversation.process.closed = True

    conversation.process.close = close
    sweep = asyncio.create_task(manager.sweep_once())
    await entered.wait()
    sweep.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sweep
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert conversation.process.closed and not manager.work
    assert conversation.timer.cancelled()


async def test_startup_deadline_releases_stream(monkeypatch):
    monkeypatch.setattr(chat, "STARTUP_SECONDS", 0.01)
    stream = FakeStream()

    async def run(command, **kwargs):
        if "kill -TERM" not in command:
            await asyncio.Future()
        return sandbox.ExecResult(0, "")

    service = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(start=lambda: stream)), run=run
    )
    with pytest.raises(TimeoutError):
        await chat.ClaudeProcess.create("test", lambda m: None, lambda m: None, service)
    assert stream.closed == 1


async def test_teardown_deadline_still_closes_stream(transport, monkeypatch):
    process, stream, service, received, failures = transport
    monkeypatch.setattr(chat, "TEARDOWN_SECONDS", 0.01)

    async def blocked(*args, **kwargs):
        await asyncio.Future()

    service.run = blocked
    await process.close()
    assert stream.closed == 1 and process.reader.done()
