import asyncio
import json
import logging
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiodocker.stream import Message

from assistant_agent import chat, sandbox
from assistant_agent.chat import ChatError, ConversationManager, constants
from assistant_agent.chat import debug_logging


class FakeSandbox:
    def __init__(self):
        self.allocated = []
        self.destroyed = []
        self.reconcile = AsyncMock()
        self.retry_cleanup = AsyncMock()
        self.close = AsyncMock()
        self.provision_mcp = AsyncMock()

    async def allocate(self, user_id, conversation_id):
        handle = sandbox.SandboxHandle(
            f"container-{conversation_id}", user_id, conversation_id,
            Path("/host/session-inputs") / conversation_id,
            Path("/app/session-inputs") / conversation_id,
        )
        self.allocated.append(handle)
        return handle

    async def destroy(self, handle):
        self.destroyed.append(handle)


def fake_handle():
    return sandbox.SandboxHandle(
        "test-container", "test-user", "test-conversation",
        Path("/host/input"), Path("/app/input"),
    )


class FakeProcess:
    def __init__(self, token, receive, failed):
        self.receive = receive
        self.failed = failed
        self.prompts = []
        self.is_closed = False

    async def discover_gmail(self):
        pass

    async def send(self, prompt):
        self.prompts.append(prompt)

    async def close(self):
        self.is_closed = True

    @classmethod
    async def create(cls, token, receive, failed, service, handle, *, before_close=None, mcp_token=None, gmail_enabled=False):
        process = cls(token, receive, failed)
        process.sandbox_handle = handle
        return process

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
    manager = ConversationManager(FakeProcess, service=FakeSandbox())
    yield manager
    await manager.close()


async def test_multiple_turns_stream_without_restarting_process(manager):
    first = await manager.submit("alice", "Remember blue")
    conversation = manager.get("alice")
    process = conversation.claude_process
    with pytest.raises(ChatError, match="progress"):
        await manager.submit("alice", "overlap")
    process.text("Bl")
    process.text("ue")
    # Whole assistant events must not duplicate streaming deltas.
    process.receive(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Blue"}]}}
    )
    assert conversation.snapshot()["transcript"][-1]["text"] == "Blue"
    assert conversation.active_turn_id == first["turn_id"]
    process.finish()
    second = await manager.submit("alice", "What color?")
    assert conversation.claude_process is process and first["turn_id"] != second["turn_id"]
    assert process.prompts == ["Remember blue", "What color?"]
    process.text("Blue again")
    process.finish()
    assert [e["type"] for e in conversation.replay_events].count("turn_completion") == 2
    assert conversation.snapshot()["active_turn"] is None


async def test_isolation_reset_failure_and_capacity(manager):
    manager.max_conversations = 2
    await manager.submit("alice", "private")
    await manager.submit("bob", "other")
    assert manager.get("alice").claude_process is not manager.get("bob").claude_process
    with pytest.raises(ChatError) as exc:
        manager.get("carol")
    assert exc.value.status == 503
    old = manager.get("alice")
    old.claude_process.failed("Ended")
    await old.cleanup_task
    assert old.claude_process.is_closed and old.has_failed
    with pytest.raises(ChatError, match="new conversation"):
        await manager.submit("alice", "follow-up")
    await manager.reset("alice")
    assert old.replay_events[-1]["type"] == "conversation_reset"
    assert manager.get("alice").conversation_id != old.conversation_id
    with pytest.raises(ChatError, match="Reload"):
        await manager.submit("alice", "stale", old.conversation_id)


async def test_timeout_does_not_apply_to_later_turn(manager):
    first = await manager.submit("alice", "one")
    conversation = manager.get("alice")
    conversation.claude_process.finish()
    await manager.submit("alice", "two")
    manager._timeout(conversation, first["turn_id"])
    assert not conversation.has_failed
    manager._timeout(conversation, conversation.active_turn_id)
    await conversation.cleanup_task
    assert conversation.has_failed and conversation.claude_process.is_closed
    assert conversation.replay_events[-1]["type"] == "turn_failure"


async def test_limits_and_shutdown(manager):
    await manager.submit("alice", "one")
    conversation = manager.get("alice")
    conversation.transcript_size_bytes = 2 * 1024 * 1024
    conversation.claude_process.text("overflow")
    await conversation.cleanup_task
    assert conversation.has_failed and conversation.claude_process.is_closed
    await manager.close()
    assert not manager.conversations


async def test_result_failure_retains_partial_output(manager):
    await manager.submit("alice", "one")
    conversation = manager.get("alice")
    conversation.claude_process.text("partial")
    conversation.claude_process.receive({"type": "result", "subtype": "error_max_turns", "is_error": True})
    await conversation.cleanup_task
    assert conversation.snapshot()["transcript"][-1]["text"] == "partial"
    assert conversation.has_failed and conversation.claude_process.is_closed


class Gate:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.made = []

    async def create(self, token, receive, failed, service, handle, *, before_close=None, mcp_token=None, gmail_enabled=False):
        self.entered.set()
        await self.release.wait()
        self.made.append(FakeProcess(token, receive, failed))
        return self.made[-1]


async def test_spawn_does_not_block_other_callers(manager):
    manager.process_factory = gate = Gate()
    task = asyncio.create_task(manager.submit("alice", "one"))
    await gate.entered.wait()
    conversation = manager.get("alice")
    assert manager.get("bob") is not conversation
    assert conversation.snapshot()["active_turn"] is None
    with pytest.raises(ChatError, match="progress"):
        await manager.submit("alice", "two")
    conversation.last_activity_monotonic = 0
    await manager.sweep_once()
    assert manager.conversations["alice"] is conversation
    gate.release.set()
    outcome = await task
    assert outcome["turn_id"] == conversation.active_turn_id
    assert conversation.claude_process.prompts == ["one"] and not conversation.is_starting


@pytest.mark.parametrize("shutdown", [False, True], ids=["reset", "shutdown"])
async def test_reset_or_shutdown_during_spawn(manager, shutdown):
    manager.process_factory = gate = Gate()
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
    assert gate.made[0].is_closed and gate.made[0].prompts == []
    assert [e["type"] for e in conversation.replay_events] == ["conversation_reset"]
    assert not conversation.is_starting and not conversation.active_turn_id


async def test_request_cancellation_preserves_reserved_submission(manager):
    manager.process_factory = gate = Gate()
    task = asyncio.create_task(manager.submit("alice", "one"))
    await gate.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.release.set()
    await asyncio.gather(*manager.background_tasks)
    conversation = manager.get("alice")
    assert conversation.claude_process.prompts == ["one"] and not conversation.is_starting
    assert not manager.background_tasks


async def test_cancel_shutdown_still_joins_startup(manager):
    manager.process_factory = gate = Gate()
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
    assert gate.made[0].is_closed and not manager.background_tasks


async def test_transport_failure_during_spawn(manager):
    made = []

    class Factory:
        @staticmethod
        async def create(token, receive, failed, service, handle, **kwargs):
            made.append(FakeProcess(token, receive, failed))
            failed("Ended")
            return made[-1]

    manager.process_factory = Factory
    with pytest.raises(ChatError, match="conversation ended"):
        await manager.submit("alice", "one")
    conversation = manager.get("alice")
    assert made[0].is_closed and not made[0].prompts
    assert [e["type"] for e in conversation.replay_events] == ["turn_failure"]
    assert not conversation.is_starting


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_spawn_failure_releases_reservation(manager, error):
    manager.process_factory = SimpleNamespace(create=AsyncMock(side_effect=error()))
    with pytest.raises(ChatError if error is RuntimeError else error):
        await manager.submit("alice", "one")
    conversation = manager.get("alice")
    assert not conversation.is_starting and not conversation.replay_events
    assert conversation.has_failed is (error is RuntimeError)


async def test_cleanup_runs_once_before_any_spawn(manager, monkeypatch):
    order = []

    async def cleanup():
        order.append("cleanup")
        await asyncio.sleep(0.01)

    class Factory(FakeProcess):
        @classmethod
        async def create(cls, *args, **kwargs):
            order.append("spawn")
            return await super().create(*args, **kwargs)

    manager.sandbox_service.reconcile = cleanup
    manager.process_factory = Factory
    manager.needs_cleanup = True
    await asyncio.gather(*(manager.submit(user, "one") for user in ("alice", "bob")))
    assert order == ["cleanup", "spawn", "spawn"] and not manager.needs_cleanup


async def test_failed_cleanup_blocks_spawn_and_is_retried(manager, monkeypatch):
    cleanup = AsyncMock(side_effect=RuntimeError("sandbox down"))
    manager.sandbox_service.reconcile = cleanup
    factory = AsyncMock()
    manager.process_factory = SimpleNamespace(create=factory)
    manager.needs_cleanup = True
    with pytest.raises(ChatError, match="unavailable"):
        await manager.submit("alice", "one")
    assert manager.needs_cleanup and not factory.called
    await manager.reset("alice")
    cleanup.side_effect = None
    manager.process_factory = FakeProcess
    await manager.submit("alice", "two")
    assert cleanup.await_count == 2 and not manager.needs_cleanup


async def test_idle_conversations_expire_but_active_conversations_stay(manager):
    await manager.submit("active", "one")
    idle = manager.get("idle")
    idle.last_activity_monotonic = 0
    await manager.sweep_once()
    assert "idle" not in manager.conversations
    assert idle.replay_events[-1]["type"] == "conversation_reset"
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
async def transport(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-api-key")
    stream = FakeStream()
    service = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(start=lambda: stream)),
        run=AsyncMock(return_value=sandbox.ExecResult(0, "")),
    )
    received, failures = [], []
    process = await chat.ClaudeProcess.create("test", received.append, failures.append, service, fake_handle())
    yield process, stream, service, received, failures
    if process.teardown_task is None or not process.teardown_task.done():
        await process.close()
    assert process.output_reader_task.done() and stream.closed == 1


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
    conversation.claude_process.text("partial")
    conversation.claude_process.finish()
    conversation.claude_process.send = AsyncMock(side_effect=OSError("closed"))
    await manager.submit("alice", "two")
    await conversation.cleanup_task
    assert conversation.has_failed and conversation.claude_process.is_closed
    assert conversation.transcript_messages[1]["text"] == "partial"


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
        chat.ClaudeProcess.create("test", lambda m: None, lambda m: None, service, fake_handle())
    )
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else sandbox.SandboxError):
        await task
    assert stream.closed == 1


@pytest.mark.skipif(os.getenv("CHAT_DOCKER_INTEGRATION") != "1", reason="Opt-in real Claude test")
async def test_real_claude_multiple_turns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify two real turns stream to the UI and log completed content to stderr."""
    monkeypatch.setenv("CLAUDE_DEBUG_STREAM", "true")
    monkeypatch.setenv("APP_ENV", "development")
    debug_logger = logging.Logger("test.real_claude_debug", level=logging.INFO)
    debug_logger.addHandler(logging.StreamHandler(sys.stderr))
    debug_logger.propagate = False
    monkeypatch.setattr(debug_logging, "_get_debug_logger", lambda: debug_logger)
    manager = ConversationManager()
    try:
        await manager.start()
        await manager.submit("integration", "Remember the codeword cobalt. Reply with only OK.")
        conversation = manager.get("integration")
        async with asyncio.timeout(125):
            while conversation.active_turn_id:
                await asyncio.sleep(0.1)
        assert not conversation.has_failed
        process = conversation.claude_process
        await manager.submit(
            "integration", "What codeword did I give you? Reply with only that word."
        )
        async with asyncio.timeout(125):
            while conversation.active_turn_id:
                await asyncio.sleep(0.1)
        assert not conversation.has_failed and conversation.claude_process is process
        assert "cobalt" in conversation.transcript_messages[-1]["text"].lower()
        assert any(e["type"] == "assistant_delta" for e in conversation.replay_events)
        records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
        content_records = [record for record in records if record["stream"] == "stdout"]
        assert content_records
        assert all(record["event"]["type"] in {"assistant", "user"}
                   for record in content_records)
        assert all(record["conversation_id"] == conversation.conversation_id
                   and record["container_id"] == process.sandbox_handle.container_id
                   and record["turn_id"] for record in content_records)
        assert len({record["turn_id"] for record in content_records}) == 2
        assert any("cobalt" in block.get("text", "").lower()
                   for record in content_records for block in record["event"]["content"]
                   if block["type"] == "text")
        await manager.reset("integration")
        assert process.is_closed
        assert not (await process.execution.inspect())["Running"]
    finally:
        await manager.close()


async def test_shutdown_joins_cleanup_of_conversation_removed_by_sweeper(manager):
    await manager.submit("alice", "one")
    conversation = manager.get("alice")
    conversation.claude_process.finish()
    conversation.last_activity_monotonic = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()
        conversation.claude_process.is_closed = True

    conversation.claude_process.close = close
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
    assert conversation.claude_process.is_closed and not manager.background_tasks
    assert conversation.turn_timeout_handle.cancelled()


async def test_startup_deadline_releases_stream(monkeypatch):
    monkeypatch.setattr(constants, "STARTUP_SECONDS", 0.01)
    stream = FakeStream()

    async def run(command, **kwargs):
        if "kill -TERM" not in command:
            await asyncio.Future()
        return sandbox.ExecResult(0, "")

    service = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(start=lambda: stream)), run=run
    )
    with pytest.raises(TimeoutError):
        await chat.ClaudeProcess.create("test", lambda m: None, lambda m: None, service, fake_handle())
    assert stream.closed == 1


async def test_teardown_deadline_still_closes_stream(transport, monkeypatch):
    process, stream, service, received, failures = transport
    monkeypatch.setattr(constants, "TEARDOWN_SECONDS", 0.01)

    async def blocked(*args, **kwargs):
        await asyncio.Future()

    service.run = blocked
    with pytest.raises(sandbox.SandboxError, match="termination"):
        await process.close()
    assert stream.closed == 1 and process.output_reader_task.done()


async def test_assignment_is_allocated_on_prompt_and_reused(manager):
    alice = manager.get("alice")
    assert not manager.sandbox_service.allocated
    await manager.submit("alice", "one")
    alice.claude_process.finish()
    await manager.submit("alice", "two")
    await manager.submit("bob", "one")
    assert len(manager.sandbox_service.allocated) == 2
    first, second = manager.sandbox_service.allocated
    assert first.user_id == "alice" and second.user_id == "bob"
    assert first.conversation_id == alice.conversation_id
    assert alice.claude_process.sandbox_handle is first
    assert manager.get("bob").claude_process.sandbox_handle is second
    assert first.container_id != second.container_id
    assert first.host_input_path != second.host_input_path
    assert first.app_input_path != second.app_input_path
    await manager.reset("alice")
    assert manager.sandbox_service.destroyed == [first]


@pytest.mark.parametrize("retirement", ["reset", "shutdown"])
async def test_retirement_during_allocation_destroys_unattached_handle(manager, retirement):
    entered, release = asyncio.Event(), asyncio.Event()
    allocate = manager.sandbox_service.allocate

    async def blocked(user, conversation):
        entered.set()
        await release.wait()
        return await allocate(user, conversation)

    manager.sandbox_service.allocate = blocked
    factory = AsyncMock()
    manager.process_factory = SimpleNamespace(create=factory)
    task = asyncio.create_task(manager.submit("alice", "one"))
    await entered.wait()
    if retirement == "reset":
        await manager.reset("alice")
        closing = None
    else:
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(ChatError):
        await task
    if closing:
        await closing
    assert not factory.called
    assert manager.sandbox_service.destroyed == manager.sandbox_service.allocated
    assert len(manager.sandbox_service.destroyed) == 1


async def test_cancelled_request_preserves_allocation(manager):
    entered, release = asyncio.Event(), asyncio.Event()
    allocate = manager.sandbox_service.allocate

    async def blocked(user, conversation):
        entered.set()
        await release.wait()
        return await allocate(user, conversation)

    manager.sandbox_service.allocate = blocked
    task = asyncio.create_task(manager.submit("alice", "one"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.gather(*manager.background_tasks)
    assert manager.get("alice").claude_process.prompts == ["one"]
    assert len(manager.sandbox_service.allocated) == 1 and not manager.sandbox_service.destroyed


async def test_process_failure_destroys_assignment_without_process(manager):
    manager.process_factory = SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("attachment")))
    with pytest.raises(ChatError):
        await manager.submit("alice", "one")
    assert manager.get("alice").claude_process is None
    assert manager.sandbox_service.destroyed == manager.sandbox_service.allocated
    assert len(manager.sandbox_service.destroyed) == 1


async def test_container_destruction_runs_after_process_cleanup_failure(manager):
    await manager.submit("alice", "one")
    conversation = manager.get("alice")
    conversation.claude_process.close = AsyncMock(side_effect=RuntimeError("termination failed"))
    await manager.reset("alice")
    assert manager.sandbox_service.destroyed == manager.sandbox_service.allocated


async def test_exec_credentials_are_only_process_environment(transport):
    process, stream, service, received, failures = transport
    call = service.exec.call_args
    assert call.kwargs["name"] == "test-container"
    assert call.kwargs["environment"] == {
        "ANTHROPIC_API_KEY": "secret-api-key", "MCP_TIMEOUT": "10000",
    }
    assert "ANTHROPIC_API_KEY" not in call.args[0]
    assert "secret-api-key" not in call.args[0]
    assert all(call.kwargs["name"] == "test-container" for call in service.run.call_args_list)


class FakeAccessService:
    def __init__(self):
        self.issued = []
        self.revoked = []
        self.invalidate_outstanding = AsyncMock()
        self.retry_failed_revocations = AsyncMock()

    async def issue(self, user_id, conversation_id, container_id):
        self.issued.append((user_id, conversation_id, container_id))
        return SimpleNamespace(
            token_id="grant-id", raw_token="private-bearer-token",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )

    async def revoke_conversation(self, conversation_id):
        self.revoked.append(conversation_id)
        return True


@pytest.fixture
async def access_manager():
    access_service = FakeAccessService()
    conversation_manager = ConversationManager(
        FakeProcess, service=FakeSandbox(), access_service=access_service,
    )
    yield conversation_manager, access_service
    await conversation_manager.close()


async def test_access_issue_before_process_start_and_fixed_between_turns(access_manager, caplog):
    conversation_manager, access_service = access_manager
    observed = []

    class InspectingFactory(FakeProcess):
        @classmethod
        async def create(
            cls, conversation_id, receive, failed, service, handle, *, before_close=None, mcp_token=None, gmail_enabled=False,
        ):
            observed.append(access_service.issued[-1])
            conversation = conversation_manager.get(handle.user_id)
            assert conversation.is_starting and conversation.claude_process is None
            assert conversation_manager.resolve_live_assignment(
                handle.user_id, handle.conversation_id, handle.container_id,
            ) is handle
            return await super().create(
                conversation_id, receive, failed, service, handle, before_close=before_close,
            )

    conversation_manager.process_factory = InspectingFactory
    await conversation_manager.submit("alice", "one")
    conversation = conversation_manager.get("alice")
    deadline = conversation.access_token_expires_at
    assert observed == [("alice", conversation.conversation_id, conversation.sandbox_handle.container_id)]
    assert conversation.access_token_id == "grant-id"
    assert conversation.snapshot()["gmail_status"] == "ready"
    assert any(event["type"] == "gmail_status" and event["gmail_status"] == "ready"
               for event in conversation.replay_events)
    conversation.claude_process.finish()
    assert conversation_manager.resolve_live_assignment(
        "alice", conversation.conversation_id, conversation.sandbox_handle.container_id,
    ) is conversation.sandbox_handle
    await conversation_manager.submit("alice", "two")
    assert len(access_service.issued) == 1 and conversation.access_token_expires_at == deadline
    assert "private-bearer-token" not in repr(vars(conversation))
    assert "private-bearer-token" not in repr(conversation.snapshot())
    assert "private-bearer-token" not in caplog.text


async def test_live_assignment_lookup_denies_wrong_or_inactive_identity(access_manager):
    conversation_manager, _ = access_manager
    assert conversation_manager.resolve_live_assignment("unknown", "unknown", "unknown") is None
    assert not conversation_manager.conversations
    await conversation_manager.submit("alice", "one")
    conversation = conversation_manager.get("alice")
    container_id = conversation.sandbox_handle.container_id
    resolver = conversation_manager.resolve_live_assignment
    assert resolver("bob", conversation.conversation_id, container_id) is None
    assert resolver("alice", "different", container_id) is None
    assert resolver("alice", conversation.conversation_id, "different") is None
    for field in ("has_failed", "is_retired"):
        setattr(conversation, field, True)
        assert resolver("alice", conversation.conversation_id, container_id) is None
        setattr(conversation, field, False)
    for field in ("sandbox_handle", "access_token_id", "access_token_expires_at"):
        original_value = getattr(conversation, field)
        setattr(conversation, field, None)
        assert resolver("alice", conversation.conversation_id, container_id) is None
        setattr(conversation, field, original_value)
    sandbox_handle = conversation.sandbox_handle
    for field in ("user_id", "conversation_id", "container_id"):
        conversation.sandbox_handle = replace(sandbox_handle, **{field: "different"})
        assert resolver("alice", conversation.conversation_id, container_id) is None
    conversation.sandbox_handle = sandbox_handle
    conversation_manager.stopped = True
    assert resolver("alice", conversation.conversation_id, container_id) is None
    conversation_manager.stopped = False
    conversation.access_token_expires_at = datetime.now(UTC)
    assert resolver("alice", conversation.conversation_id, container_id) is None


@pytest.mark.parametrize("is_starting", [False, True])
@pytest.mark.parametrize("process_state", ["missing", "closed"])
async def test_live_assignment_authorization_independent_of_process_readiness(
    access_manager, is_starting, process_state,
):
    conversation_manager, _ = access_manager
    await conversation_manager.submit("alice", "one")
    conversation = conversation_manager.get("alice")
    process = conversation.claude_process
    conversation.is_starting = is_starting
    if process_state == "missing":
        conversation.claude_process = None
    else:
        process.is_closed = True
    try:
        assert conversation_manager.resolve_live_assignment(
            "alice", conversation.conversation_id, conversation.sandbox_handle.container_id,
        ) is conversation.sandbox_handle
    finally:
        conversation.is_starting = False
        conversation.claude_process = process
        process.is_closed = False


@pytest.mark.parametrize("revocation_fails", [False, True])
async def test_revocation_immediately_denies_assignment_without_retirement(
    access_manager, revocation_fails,
):
    conversation_manager, access_service = access_manager
    await conversation_manager.submit("alice", "one")
    conversation = conversation_manager.get("alice")
    expiry = conversation.access_token_expires_at
    entered, release = asyncio.Event(), asyncio.Event()

    async def revoke(conversation_id):
        assert conversation_id == conversation.conversation_id
        entered.set()
        await release.wait()
        if revocation_fails:
            raise RuntimeError("database unavailable")
        return True

    async def retire_files(conversation_id):
        assert conversation_id == conversation.conversation_id
        assert conversation.access_token_id is None
        assert conversation_manager.resolve_live_assignment(
            "alice", conversation_id, conversation.sandbox_handle.container_id,
        ) is None

    access_service.revoke_conversation = revoke
    conversation.session_files_service = SimpleNamespace(retire=AsyncMock(side_effect=retire_files))
    revoking = asyncio.create_task(conversation.revoke_access())
    await entered.wait()
    try:
        assert not conversation.has_failed and not conversation.is_retired
        assert not conversation.claude_process.is_closed
        assert conversation.access_token_id is None
        assert conversation.access_token_expires_at == expiry
        assert conversation_manager.resolve_live_assignment(
            "alice", conversation.conversation_id, conversation.sandbox_handle.container_id,
        ) is None
    finally:
        release.set()
        await revoking
    assert conversation.access_token_id is None
    conversation.session_files_service.retire.assert_awaited_once_with(conversation.conversation_id)


@pytest.mark.parametrize("retirement", ["reset", "failure", "timeout", "shutdown"])
async def test_access_revoked_before_process_and_container_cleanup(access_manager, retirement):
    conversation_manager, access_service = access_manager
    await conversation_manager.submit("alice", "one")
    conversation = conversation_manager.get("alice")
    original_close = conversation.claude_process.close
    original_destroy = conversation_manager.sandbox_service.destroy

    async def close():
        assert conversation.conversation_id in access_service.revoked
        assert conversation_manager.resolve_live_assignment(
            "alice", conversation.conversation_id, conversation.sandbox_handle.container_id,
        ) is None
        await original_close()

    async def destroy(handle):
        assert conversation.conversation_id in access_service.revoked
        await original_destroy(handle)

    conversation.claude_process.close = close
    conversation_manager.sandbox_service.destroy = destroy
    if retirement == "reset":
        await conversation_manager.reset("alice")
    elif retirement == "shutdown":
        await conversation_manager.close()
    else:
        if retirement == "failure":
            conversation.fail("transport ended")
        else:
            conversation_manager._timeout(conversation, conversation.active_turn_id)
        await conversation.cleanup_task
    assert access_service.revoked == [conversation.conversation_id]
    assert conversation.claude_process.is_closed


async def test_failed_revocation_does_not_prevent_cleanup_and_sweeper_retries(access_manager):
    conversation_manager, access_service = access_manager
    await conversation_manager.submit("alice", "one")
    conversation = conversation_manager.get("alice")
    access_service.revoke_conversation = AsyncMock(side_effect=RuntimeError("database unavailable"))
    await conversation_manager.reset("alice")
    assert conversation.claude_process.is_closed
    assert conversation_manager.sandbox_service.destroyed == [conversation.sandbox_handle]
    await conversation_manager.sweep_once()
    access_service.retry_failed_revocations.assert_awaited_once()


@pytest.mark.parametrize("revocation_fails", [False, True])
@pytest.mark.parametrize("retirement", ["reset", "failure", "shutdown", "disconnect", "expiry"])
async def test_download_retirement_drained_before_assignment_cleanup(
    access_manager, revocation_fails, retirement,
):
    conversation_manager, access_service = access_manager
    retirement_started = asyncio.Event()
    downloads_drained = asyncio.Event()

    async def retire_files(conversation_id):
        assert conversation_id == conversation.conversation_id
        assert conversation_manager.resolve_live_assignment(
            "alice", conversation_id, conversation.sandbox_handle.container_id,
        ) is None
        if not revocation_fails:
            assert conversation_id in access_service.revoked
        retirement_started.set()
        await downloads_drained.wait()

    conversation_manager.session_files_service = SimpleNamespace(retire=AsyncMock(side_effect=retire_files))
    await conversation_manager.submit("alice", "one")
    conversation = conversation_manager.get("alice")
    if revocation_fails:
        access_service.revoke_conversation = AsyncMock(side_effect=RuntimeError("unavailable"))
    if retirement == "reset":
        cleanup_task = asyncio.create_task(conversation_manager.reset("alice"))
    elif retirement == "shutdown":
        cleanup_task = asyncio.create_task(conversation_manager.close())
    elif retirement == "disconnect":
        conversation_manager.block_user("alice")
        cleanup_task = asyncio.create_task(conversation_manager.reset("alice"))
    elif retirement == "expiry":
        conversation.access_token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        cleanup_task = asyncio.create_task(conversation_manager.sweep_once())
    else:
        conversation.fail("process failed")
        cleanup_task = conversation.cleanup_task
    await retirement_started.wait()
    assert not conversation.claude_process.is_closed
    assert not conversation_manager.sandbox_service.destroyed
    downloads_drained.set()
    await cleanup_task
    assert conversation.claude_process.is_closed
    assert conversation_manager.sandbox_service.destroyed == [conversation.sandbox_handle]
    conversation_manager.session_files_service.retire.assert_awaited_once_with(conversation.conversation_id)


@pytest.mark.parametrize("failure_stage", ["issue", "process"])
async def test_access_issue_and_process_failures_revoke_and_destroy(access_manager, failure_stage):
    conversation_manager, access_service = access_manager
    conversation_manager.session_files_service = SimpleNamespace(retire=AsyncMock())
    if failure_stage == "issue":
        access_service.issue = AsyncMock(side_effect=RuntimeError("database unavailable"))
    else:
        conversation_manager.process_factory = SimpleNamespace(
            create=AsyncMock(side_effect=RuntimeError("startup failed")),
        )
    with pytest.raises(ChatError, match="unavailable"):
        await conversation_manager.submit("alice", "one")
    conversation = conversation_manager.get("alice")
    assert access_service.revoked == [conversation.conversation_id]
    assert conversation.has_failed and not conversation.is_starting
    assert conversation_manager.sandbox_service.destroyed == [conversation.sandbox_handle]
    conversation_manager.session_files_service.retire.assert_awaited_once_with(conversation.conversation_id)


@pytest.mark.parametrize("retirement", ["reset", "shutdown", "disconnect"])
async def test_retirement_during_token_issue_revokes_late_grant(access_manager, retirement):
    conversation_manager, access_service = access_manager
    entered, release = asyncio.Event(), asyncio.Event()
    original_issue = access_service.issue

    async def issue(*args):
        assert conversation_manager.resolve_live_assignment(*args) is None
        entered.set()
        await release.wait()
        return await original_issue(*args)

    access_service.issue = issue
    process_factory = SimpleNamespace(create=AsyncMock())
    conversation_manager.process_factory = process_factory
    submitting = asyncio.create_task(conversation_manager.submit("alice", "one"))
    await entered.wait()
    conversation = conversation_manager.get("alice")
    closing = None
    if retirement == "shutdown":
        closing = asyncio.create_task(conversation_manager.close())
        await asyncio.sleep(0)
    else:
        if retirement == "disconnect":
            conversation_manager.block_user("alice")
        await conversation_manager.reset("alice")
    assert conversation.is_retired
    release.set()
    with pytest.raises(ChatError):
        await submitting
    if closing is not None:
        await closing
    assert not process_factory.create.called
    assert access_service.revoked[-1] == conversation.conversation_id
    if retirement != "shutdown":
        assert len(access_service.revoked) >= 2
    assert conversation_manager.sandbox_service.destroyed == [conversation.sandbox_handle]
    if retirement == "disconnect":
        with pytest.raises(ChatError, match="unavailable"):
            await conversation_manager.submit("alice", "new")


@pytest.mark.parametrize("via_sweeper", [False, True])
async def test_access_expiry_retires_active_conversation(access_manager, via_sweeper):
    conversation_manager, access_service = access_manager
    await conversation_manager.submit("alice", "one")
    conversation = conversation_manager.get("alice")
    conversation.access_token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if via_sweeper:
        await conversation_manager.sweep_once()
        assert "alice" not in conversation_manager.conversations
    else:
        with pytest.raises(ChatError, match="expired"):
            await conversation_manager.submit("alice", "two")
        await conversation.cleanup_task
    assert conversation.is_retired and conversation.claude_process.is_closed
    assert access_service.revoked == [conversation.conversation_id]
    assert conversation.claude_process.prompts == ["one"]


async def test_restart_invalidation_independent_of_docker_cleanup_and_retried(access_manager):
    conversation_manager, access_service = access_manager
    access_service.invalidate_outstanding.side_effect = RuntimeError("database unavailable")
    await conversation_manager.start()
    conversation_manager.sandbox_service.reconcile.assert_awaited_once()
    assert conversation_manager.needs_access_invalidation
    assert not conversation_manager.needs_cleanup
    with pytest.raises(ChatError, match="unavailable"):
        await conversation_manager.submit("alice", "one")
    assert not conversation_manager.sandbox_service.allocated
    await conversation_manager.reset("alice")
    access_service.invalidate_outstanding.side_effect = None
    await conversation_manager.sweep_once()
    assert not conversation_manager.needs_access_invalidation
    await asyncio.gather(
        conversation_manager.submit("alice", "one"),
        conversation_manager.submit("bob", "two"),
    )
    assert access_service.invalidate_outstanding.await_count == 3


async def test_restart_invalidation_succeeds_even_when_docker_unavailable(access_manager):
    conversation_manager, access_service = access_manager
    conversation_manager.sandbox_service.reconcile.side_effect = RuntimeError("docker unavailable")
    await conversation_manager.start()
    access_service.invalidate_outstanding.assert_awaited_once()
    assert not conversation_manager.needs_access_invalidation
    assert conversation_manager.needs_cleanup


async def test_cancelled_issuance_request_remains_owned_by_manager(access_manager):
    conversation_manager, access_service = access_manager
    entered, release = asyncio.Event(), asyncio.Event()
    original_issue = access_service.issue

    async def issue(*args):
        entered.set()
        await release.wait()
        return await original_issue(*args)

    access_service.issue = issue
    submitting = asyncio.create_task(conversation_manager.submit("alice", "one"))
    await entered.wait()
    submitting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submitting
    release.set()
    await asyncio.gather(*conversation_manager.background_tasks)
    conversation = conversation_manager.get("alice")
    assert conversation.access_token_id == "grant-id"
    assert conversation.claude_process.prompts == ["one"]
    assert not access_service.revoked


async def test_cancelled_reset_does_not_cancel_grant_revocation(access_manager):
    conversation_manager, access_service = access_manager
    await conversation_manager.submit("alice", "one")
    conversation = conversation_manager.get("alice")
    entered, release = asyncio.Event(), asyncio.Event()
    original_revoke = access_service.revoke_conversation

    async def revoke(conversation_id):
        entered.set()
        await release.wait()
        return await original_revoke(conversation_id)

    access_service.revoke_conversation = revoke
    resetting = asyncio.create_task(conversation_manager.reset("alice"))
    await entered.wait()
    assert conversation.is_retired and not conversation.claude_process.is_closed
    resetting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resetting
    release.set()
    await conversation_manager.close()
    assert access_service.revoked == [conversation.conversation_id]
    assert conversation.claude_process.is_closed


async def test_idle_retirement_revokes_grant(access_manager):
    conversation_manager, access_service = access_manager
    await conversation_manager.submit("alice", "one")
    conversation = conversation_manager.get("alice")
    conversation.claude_process.finish()
    conversation.last_activity_monotonic = 0
    await conversation_manager.sweep_once()
    assert access_service.revoked == [conversation.conversation_id]
    assert conversation.claude_process.is_closed and conversation.is_retired


async def test_real_transport_waits_for_revocation_before_process_cleanup():
    stream = FakeStream()
    service = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(start=lambda: stream)),
        run=AsyncMock(return_value=sandbox.ExecResult(0, "")),
    )
    failed = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def revoke_access():
        assert failed == ["The assistant conversation ended. Start a new conversation."]
        entered.set()
        await release.wait()

    process = await chat.ClaudeProcess.create(
        "test", lambda message: None, failed.append, service, fake_handle(),
        before_close=revoke_access,
    )
    stream.messages.put_nowait(None)
    await entered.wait()
    assert process.is_closed and stream.closed == 0
    assert service.run.await_count == 1  # Only process readiness has run.
    release.set()
    await process.close()
    assert stream.closed == 1 and service.run.await_count == 2


@pytest.mark.parametrize("cancel", [False, True])
async def test_real_startup_failure_waits_for_revocation_before_cleanup(cancel):
    stream = FakeStream()
    ready_entered, revocation_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    commands = []

    async def run(command, **kwargs):
        commands.append(command)
        if "kill -TERM" in command:
            return sandbox.ExecResult(0, "")
        ready_entered.set()
        if cancel:
            await asyncio.Future()
        return sandbox.ExecResult(1, "")

    async def revoke_access():
        revocation_entered.set()
        await release.wait()

    service = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(start=lambda: stream)), run=run,
    )
    starting = asyncio.create_task(chat.ClaudeProcess.create(
        "test", lambda message: None, lambda message: None, service, fake_handle(),
        before_close=revoke_access,
    ))
    await ready_entered.wait()
    if cancel:
        starting.cancel()
    await revocation_entered.wait()
    assert stream.closed == 0 and len(commands) == 1
    release.set()
    with pytest.raises(asyncio.CancelledError if cancel else sandbox.SandboxError):
        await starting
    assert stream.closed == 1 and "kill -TERM" in commands[-1]


async def test_real_process_cleanup_proceeds_when_revocation_hook_fails():
    stream = FakeStream()
    service = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(start=lambda: stream)),
        run=AsyncMock(return_value=sandbox.ExecResult(0, "")),
    )
    revoke_access = AsyncMock(side_effect=RuntimeError("database unavailable"))
    process = await chat.ClaudeProcess.create(
        "test", lambda message: None, lambda message: None, service, fake_handle(),
        before_close=revoke_access,
    )
    await process.close()
    revoke_access.assert_awaited_once()
    assert stream.closed == 1 and service.run.await_count == 2


@pytest.mark.parametrize("startup_failure", [False, True])
async def test_manager_wires_revocation_into_real_process_cleanup(access_manager, startup_failure, monkeypatch):
    conversation_manager, access_service = access_manager
    conversation_manager.process_factory = chat.ClaudeProcess
    monkeypatch.setattr(chat.ClaudeProcess, "discover_gmail", AsyncMock())
    stream = FakeStream()
    commands = []
    entered, release = asyncio.Event(), asyncio.Event()
    original_revoke = access_service.revoke_conversation

    async def revoke_access(conversation_id):
        entered.set()
        await release.wait()
        return await original_revoke(conversation_id)

    async def run(command, **kwargs):
        commands.append(command)
        if "kill -TERM" in command or not startup_failure:
            return sandbox.ExecResult(0, "")
        return sandbox.ExecResult(1, "")

    access_service.revoke_conversation = revoke_access
    conversation_manager.sandbox_service.exec = AsyncMock(
        return_value=SimpleNamespace(start=lambda: stream),
    )
    conversation_manager.sandbox_service.run = run
    submitting = asyncio.create_task(conversation_manager.submit("alice", "one"))
    if not startup_failure:
        await submitting
        stream.messages.put_nowait(None)
    await entered.wait()
    conversation = conversation_manager.get("alice")
    assert conversation_manager.resolve_live_assignment(
        "alice", conversation.conversation_id, conversation.sandbox_handle.container_id,
    ) is None
    assert stream.closed == 0
    assert not any("kill -TERM" in command for command in commands)
    release.set()
    if startup_failure:
        with pytest.raises(ChatError, match="unavailable"):
            await submitting
    else:
        await conversation.cleanup_task
    assert conversation.conversation_id in access_service.revoked
    assert stream.closed == 1
    assert any("kill -TERM" in command for command in commands)


@pytest.mark.parametrize("configuration_failure", [False, True])
async def test_gmail_fallback_once_preserves_chat_and_expiry(access_manager, configuration_failure):
    from assistant_agent.chat.claude_process import GmailUnavailable

    manager, access = access_manager
    launches = []

    class DiscoveryFactory(FakeProcess):
        @classmethod
        async def create(cls, *args, mcp_token=None, gmail_enabled=False, **kwargs):
            process = await super().create(*args, **kwargs)
            launches.append((gmail_enabled, mcp_token, process))
            process.discover_gmail = AsyncMock(side_effect=GmailUnavailable("missing_tools"))
            return process

    manager.process_factory = DiscoveryFactory
    manager.session_files_service = SimpleNamespace(retire=AsyncMock())
    if configuration_failure:
        manager.sandbox_service.provision_mcp.side_effect = [RuntimeError("config"), None]
    conversation = manager.get("alice")
    await manager.submit("alice", "first")
    assert len(launches) == (1 if configuration_failure else 2)
    assert launches[-1][:2] == (False, None)
    if not configuration_failure:
        assert launches[0][0] and launches[0][1] == "private-bearer-token"
        assert launches[0][2].is_closed and launches[0][2].prompts == []
    assert launches[-1][2].prompts == ["first"]
    assert conversation.snapshot()["gmail_status"] == "unavailable"
    assert [event["gmail_status"] for event in conversation.replay_events
            if event["type"] == "gmail_status"] == ["unavailable"]
    assert conversation.access_token_id is None and conversation.access_token_expires_at is not None
    assert len(access.issued) == 1 and access.revoked
    manager.session_files_service.retire.assert_awaited()
    conversation.claude_process.finish()
    await manager.submit("alice", "second")
    assert conversation.claude_process.prompts == ["first", "second"]
    assert conversation.snapshot()["gmail_status"] == "unavailable"
    assert len(launches) == (1 if configuration_failure else 2)
    assert "private-bearer-token" not in repr(vars(conversation))


@pytest.mark.parametrize("failure", ["transport", "termination", "replacement"])
async def test_gmail_startup_normal_failures_do_not_select_fallback(access_manager, failure):
    from assistant_agent.chat.claude_process import GmailUnavailable

    manager, _ = access_manager
    launches = []

    class FailureFactory(FakeProcess):
        @classmethod
        async def create(cls, *args, **kwargs):
            launches.append(kwargs)
            if not kwargs["gmail_enabled"]:
                raise RuntimeError("replacement")
            process = await super().create(*args, **kwargs)
            error = RuntimeError("transport") if failure == "transport" else GmailUnavailable("missing")
            process.discover_gmail = AsyncMock(side_effect=error)
            if failure == "termination":
                process.close = AsyncMock(side_effect=RuntimeError("termination"))
            return process

    manager.process_factory = FailureFactory
    with pytest.raises(ChatError):
        await manager.submit("alice", "first")
    assert len(launches) == (2 if failure == "replacement" else 1)
    assert manager.get("alice").has_failed
    assert not manager.get("alice").transcript_messages
    assert manager.sandbox_service.destroyed == manager.sandbox_service.allocated


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("retirement", ["reset", "shutdown", "expiry"])
async def test_retirement_during_gmail_launch_attempt(access_manager, attempt, retirement):
    from assistant_agent.chat.claude_process import GmailUnavailable

    manager, _ = access_manager
    entered, release = asyncio.Event(), asyncio.Event()
    launches = []

    class GateFactory(FakeProcess):
        @classmethod
        async def create(cls, *args, **kwargs):
            process = await super().create(*args, **kwargs)
            launches.append(process)
            if len(launches) == attempt:
                entered.set()
                await release.wait()
            process.discover_gmail = AsyncMock(side_effect=GmailUnavailable("missing"))
            return process

    manager.process_factory = GateFactory
    submitting = asyncio.create_task(manager.submit("alice", "first"))
    await entered.wait()
    conversation = manager.get("alice")
    if retirement == "reset":
        await manager.reset("alice")
    elif retirement == "expiry":
        conversation.access_token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await manager.sweep_once()
    else:
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(ChatError):
        await submitting
    if retirement == "shutdown":
        await closing
    assert all(process.is_closed and not process.prompts for process in launches)
    assert manager.sandbox_service.destroyed == manager.sandbox_service.allocated


@pytest.mark.parametrize("deadline", ["STARTUP_SECONDS", "GMAIL_STARTUP_SECONDS"])
async def test_gmail_startup_deadlines_do_not_send_prompt(access_manager, monkeypatch, deadline):
    manager, access = access_manager
    monkeypatch.setattr(constants, deadline, 0.01)
    gate = Gate()
    manager.process_factory = gate
    with pytest.raises(ChatError):
        await manager.submit("alice", "first")
    assert access.revoked
    assert manager.sandbox_service.destroyed == manager.sandbox_service.allocated
    assert not manager.get("alice").transcript_messages
