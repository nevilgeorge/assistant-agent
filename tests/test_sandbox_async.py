"""Public aiodocker API contracts and opt-in Compose sandbox checks."""

import asyncio
import io
import os
import shlex
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import aiodocker
from aiodocker.stream import Message

from assistant_agent import chat, sandbox
from assistant_agent.config import SandboxSettings, get_sandbox_settings
from assistant_agent.daily_logs import DailyLogService
from test_chat import FakeStream


class CommandStream(FakeStream):
    async def __aexit__(self, *exc):
        await self.close()


@pytest.mark.parametrize("code", [0, 7, None])
async def test_command_collects_ordered_bytes_and_inspects_exit(monkeypatch, code):
    stream = CommandStream()
    for item in [Message(1, b"\xe9"), Message(2, b"\x9b"), Message(1, b"\xaa\xff"), None]:
        stream.messages.put_nowait(item)
    execution = SimpleNamespace(
        start=lambda: stream, inspect=AsyncMock(return_value={"ExitCode": code})
    )
    container = {"State": {"Status": "running"}}

    class Container(dict):
        exec = AsyncMock(return_value=execution)

    container = Container(container)
    clients = []

    def docker(**kwargs):
        client = SimpleNamespace(
            containers=SimpleNamespace(get=AsyncMock(return_value=container)), close=AsyncMock()
        )
        clients.append(client)
        return client

    monkeypatch.setattr(sandbox.aiodocker, "Docker", docker)
    async with sandbox.Sandbox() as service:
        assert not clients  # no connection on construction
        if code is None:
            with pytest.raises(sandbox.SandboxError, match="exit status"):
                await service.run("echo test", name="override")
        else:
            result = await service.run("echo test", name="override", workdir="/tmp")
            assert result == sandbox.ExecResult(code, "雪�")
        assert stream.closed == 1
        clients[0].containers.get.assert_awaited_with(
            "override"
        )
        args = container.exec.call_args.kwargs
        assert args == dict(
            stdin=False,
            stdout=True,
            stderr=True,
            tty=False,
            user="agent",
            workdir="/tmp" if code is not None else "/workspace",
            environment=None,
        )
        # Interactive execs use another HTTP pool; repeat control calls reuse theirs.
        await service.exec("read", stdin=True, name="override")
        await service.exec("true", name="override")
        assert len(clients) == 2
    assert all(client.close.await_count == 1 for client in clients)


async def test_command_deadline_closes_stream():
    stream = CommandStream()
    async with sandbox.Sandbox() as service:
        service.exec = AsyncMock(return_value=SimpleNamespace(start=lambda: stream))
        with pytest.raises(sandbox.SandboxError):
            await service.run("hang", name="test", timeout=0.01)
        assert stream.closed == 1


pytestmark_docker = pytest.mark.skipif(
    os.getenv("SANDBOX_DOCKER_INTEGRATION") != "1", reason="Opt-in Docker transport tests"
)


@pytestmark_docker
async def test_docker_exit_codes_and_separate_interactive_streams():
    async with sandbox.Sandbox() as service:
        handle = await service.allocate("test", uuid.uuid4().hex)
        result = await service.run("printf '\\377'; printf error >&2; exit 7", name=handle.container_id)
        # Docker may deliver the two file descriptors in either order.
        assert result.exit_code == 7 and result.output in {"�error", "error�"}
        execution = await service.exec(
            'while IFS= read -r line; do printf "out:%s\\n" "$line"; '
            'printf "err:%s\\n" "$line" >&2; done',
            stdin=True,
            name=handle.container_id,
        )
        async with execution.start() as stream:
            for text in ("first", "雪 second"):
                await stream.write_in((text + "\n").encode())
                outputs = {1: b"", 2: b""}
                async with asyncio.timeout(5):
                    while not all(b"\n" in data for data in outputs.values()):
                        message = await stream.read_out()
                        assert message is not None
                        outputs[message.stream] += message.data
                assert outputs == {1: f"out:{text}\n".encode(), 2: f"err:{text}\n".encode()}
        async with asyncio.timeout(5):
            while (await execution.inspect())["Running"]:
                await asyncio.sleep(0.05)


@pytestmark_docker
async def test_docker_process_reuse_and_descendant_termination(monkeypatch):
    child_path = f"/tmp/assistant-test-child-{uuid.uuid4().hex}"
    program = f"""
import json, subprocess, sys
child = subprocess.Popen(['bash', '-c', 'trap "" TERM; exec sleep 300'])
with open({child_path!r}, 'w') as f: f.write(str(child.pid))
for line in sys.stdin:
    prompt = json.loads(line)['message']['content']
    print('private diagnostic', file=sys.stderr, flush=True)
    print(json.dumps(dict(type='result', subtype='success', result=prompt)), flush=True)
"""
    monkeypatch.setattr(chat.ClaudeProcess, "argv", ["python3", "-u", "-c", program])
    async with sandbox.Sandbox() as service:
        manager = chat.ConversationManager(service=service)
        user_id = uuid.uuid4().hex
        try:
            await manager.submit(user_id, "first")
            conversation = manager.get(user_id)
            process = conversation.claude_process
            async with asyncio.timeout(5):
                while conversation.active_turn_id:
                    await asyncio.sleep(0.01)
            await manager.submit(user_id, "雪 second")
            async with asyncio.timeout(5):
                while conversation.active_turn_id:
                    await asyncio.sleep(0.01)
            assert not conversation.has_failed and conversation.claude_process is process
            assert conversation.transcript_messages[-1]["text"].startswith("Current user time: ")
            assert conversation.transcript_messages[-1]["text"].endswith("\n\n雪 second")
            handle = conversation.sandbox_handle
            child = (await service.run(f"cat {shlex.quote(child_path)}", name=handle.container_id)).output.strip()
            assert child.isdigit()
            await manager.reset(user_id)
            assert process.output_reader_task.done() and process.is_closed
            with pytest.raises(aiodocker.DockerError) as missing:
                await service._client(sandbox.DockerClientRole.CONTROL).containers.get(handle.container_id)
            assert missing.value.status == 404
            assert not handle.app_input_path.exists()
        finally:
            await manager.close()



async def test_failed_stream_open_releases_partial_attachment():
    class BrokenOpen(CommandStream):
        async def __aenter__(self):
            self.opened = True
            raise RuntimeError("attach failed")

    stream = BrokenOpen()
    async with sandbox.Sandbox() as service:
        service.exec = AsyncMock(return_value=SimpleNamespace(start=lambda: stream))
        with pytest.raises(sandbox.SandboxError, match="attach failed"):
            await service.run("true", name="test")
        assert stream.closed == 1


async def test_context_archive_contains_packaged_daily_log_guidance():
    """Check the actual provisioned archive, including its daily-log instructions."""
    container = SimpleNamespace(put_archive=AsyncMock())
    service = sandbox.Sandbox()
    await service._provision_context_files(container)
    destination, archive_bytes = container.put_archive.call_args.args
    assert destination == "/workspace"
    expected_context = resources.files("assistant_agent.agent_kit").joinpath("CLAUDE.md").read_bytes()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
        context_file = archive.getmember("CLAUDE.md")
        assert context_file.uid == context_file.gid == 0 and context_file.mode == 0o644
        context = archive.extractfile(context_file).read()
        assert context == expected_context
    assert b"# Daily logs" in context
    assert b"/daily-logs/YYYY-MM-DD.md" in context
    assert b"latest supplied" in context
    assert b"Never create, rename, or delete daily-log" in context


@pytestmark_docker
async def test_docker_input_publication_workspace_environment_and_inspection():
    async with sandbox.Sandbox() as service:
        handle = await service.allocate("input-test", uuid.uuid4().hex)
        context = await service.run("cat /workspace/CLAUDE.md", name=handle.container_id)
        assert context.ok
        assert context.output.encode() == resources.files("assistant_agent.agent_kit").joinpath(
            "CLAUDE.md"
        ).read_bytes()
        metadata = await service.run(
            "stat -c '%u:%g:%a' /workspace/CLAUDE.md; test -r /workspace/CLAUDE.md",
            name=handle.container_id,
        )
        assert metadata.ok and metadata.output.strip() == "0:0:644"
        temporary = handle.app_input_path / "temporary"
        published = handle.app_input_path / "published.txt"
        temporary.write_text("atomic publication")
        temporary.chmod(0o644)
        temporary.replace(published)
        result = await service.run(
            'cat /input/published.txt; touch /workspace/writable; '
            'if touch /input/forbidden 2>/dev/null; then exit 9; fi; '
            'printf "\\n%s" "$PRIVATE_TEST_VALUE"',
            name=handle.container_id,
            environment={"PRIVATE_TEST_VALUE": "secret; $(touch /workspace/injected)"},
        )
        assert result.ok
        assert result.output == "atomic publication\nsecret; $(touch /workspace/injected)"
        assert (await service.run("test ! -e /workspace/injected", name=handle.container_id)).ok
        container = await service._client(sandbox.DockerClientRole.CONTROL).containers.get(
            handle.container_id
        )
        details = await container.show()
        assert not any("PRIVATE_TEST_VALUE" in value for value in details["Config"]["Env"])
        assert not any("GOOGLE" in value or "ANTHROPIC" in value
                       for value in details["Config"]["Env"])
        mounts = details["Mounts"]
        assert len(mounts) == 1 and mounts[0]["Destination"] == "/input"
        assert not mounts[0]["RW"] and mounts[0]["Source"] == str(handle.host_input_path)
        host = details["HostConfig"]
        assert host["PidsLimit"] == 2048 and host["Memory"] == 2 * 1024**3
        assert host["NanoCpus"] == 1_000_000_000 and host["Init"]
        assert host["CapDrop"] == ["ALL"]
        assert host["SecurityOpt"] == ["no-new-privileges:true"]
        assert host["RestartPolicy"]["Name"] == "no" and not host["PortBindings"]
        assert list(details["NetworkSettings"]["Networks"]) == [service.settings.network]


async def test_exec_environment_uses_docker_argument_without_shell_interpolation(monkeypatch, caplog):
    class Container(dict):
        exec = AsyncMock()
    container = Container(State={"Status": "running"})
    client = SimpleNamespace(containers=SimpleNamespace(get=AsyncMock(return_value=container)),
                             close=AsyncMock())
    monkeypatch.setattr(sandbox.aiodocker, "Docker", lambda **kwargs: client)
    private = {"ANTHROPIC_API_KEY": "private; $(touch /workspace/injected)"}
    async with sandbox.Sandbox() as service:
        await service.exec("claude --version", name="chosen", environment=private)
    assert container.exec.call_args.args == (["bash", "-lc", "claude --version"],)
    assert container.exec.call_args.kwargs["environment"] is private
    assert private["ANTHROPIC_API_KEY"] not in caplog.text


@pytest.mark.skipif(
    os.getenv("CLAUDE_DAILY_LOGS_INTEGRATION") != "1",
    reason="Set CLAUDE_DAILY_LOGS_INTEGRATION=1 for real Docker/model daily-log checks",
)
@pytest.mark.parametrize("gmail_enabled", [False, True], ids=["chat-only", "gmail-launch"])
async def test_installed_claude_daily_log_permissions(gmail_enabled: bool) -> None:
    """Exercise installed CLI reads, edits, and denied writes with synthetic files."""
    assert os.getenv("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY is required"
    run_id = uuid.uuid4().hex
    docker_client = aiodocker.Docker()
    network = None
    service = None
    daily_log_service = None
    process = None
    with tempfile.TemporaryDirectory(prefix="claude-daily-logs-", dir="/private/tmp") as directory:
        root = Path(directory)
        try:
            image = await docker_client.images.inspect(get_sandbox_settings().image)
            network_name = f"daily-logs-{run_id}"
            network = await docker_client.networks.create({"Name": network_name})
            settings = SandboxSettings(
                image=image["Id"], network=network_name, deployment_id=f"test-{run_id}",
                host_input_root=root / "inputs", app_input_root=root / "inputs",
                host_user_data_root=root / "user-data", app_user_data_root=root / "user-data",
            )
            service = sandbox.Sandbox(settings)
            daily_log_service = DailyLogService(settings)
            timestamp = datetime(2026, 10, 12, 14, 35, tzinfo=UTC)
            daily_log = await daily_log_service.prepare(run_id, "UTC", timestamp)
            dated_file = daily_log.app_daily_logs_path / "2026-10-12.md"
            marker = f"PRIVATE_DAILY_ENTRY_{uuid.uuid4().hex}"
            original_content = dated_file.read_text() + f"\n{marker}\n"
            dated_file.write_text(original_content)
            handle = await service.allocate(
                run_id, run_id, host_daily_logs_path=daily_log.host_daily_logs_path,
            )
            context = await service.run("cat /workspace/CLAUDE.md", name=handle.container_id)
            assert context.ok
            assert context.output.encode() == resources.files("assistant_agent.agent_kit").joinpath(
                "CLAUDE.md"
            ).read_bytes()
            input_file = handle.app_input_path / "outside.md"
            input_file.write_text("input original\n")
            input_file.chmod(0o644)
            assert (await service.run(
                "printf 'workspace original\\n' > /workspace/outside.md",
                name=handle.container_id,
            )).ok
            # Server discovery is covered separately; preserve Gmail launch flags
            # while keeping this synthetic filesystem check independent of a server.
            await service.provision_mcp(handle, gmail_enabled=False)
            events: list[dict] = []
            failures: list[str] = []
            process = await chat.ClaudeProcess.create(
                run_id, events.append, failures.append, service, handle,
                gmail_enabled=gmail_enabled,
                mcp_token="synthetic-unused" if gmail_enabled else None,
            )

            async def complete_turn(prompt: str) -> dict:
                """Send a prompt and await one model result without exposing file contents."""
                events.clear()
                await process.send(f"Current user time: {timestamp.isoformat()}\n\n{prompt}")
                async with asyncio.timeout(180):
                    while not any(event.get("type") == "result" for event in events):
                        assert not failures, "Claude transport failed"
                        await asyncio.sleep(0.1)
                result = next(event for event in events if event.get("type") == "result")
                assert not result.get("is_error"), "Model turn failed"
                return result

            appended_entry = f"Verified daily entry {uuid.uuid4().hex}"
            result = await complete_turn(
                "Read today's daily log with the Read tool. Report the private entry you find, "
                f"and use Edit to append exactly this entry while preserving all existing text: "
                f"{appended_entry}."
            )
            assert marker in result.get("result", ""), "Daily-log read did not return the entry"
            updated_content = dated_file.read_text()
            assert original_content in updated_content and appended_entry in updated_content
            tools = {
                block.get("name") for event in events if event.get("type") == "assistant"
                for block in event.get("message", {}).get("content", [])
                if block.get("type") == "tool_use"
            }
            assert {"Read", "Edit"}.issubset(tools)
            denied = await complete_turn(
                "Test the tool permission boundary. Attempt Write on /workspace/forbidden.md "
                "with content forbidden, and attempt Edit on /input/outside.md replacing "
                "'input original' with 'input changed'. Use those tools directly, not Bash. "
                "Report any permission refusals."
            )
            assert denied.get("permission_denials"), "Expected CLI permission denials"
            assert input_file.read_text() == "input original\n"
            assert (await service.run(
                "test ! -e /workspace/forbidden.md && "
                "test \"$(cat /workspace/outside.md)\" = 'workspace original'",
                name=handle.container_id,
            )).ok
            await process.close()
            process = None
            await service.destroy(handle)
            assert dated_file.read_text() == updated_content
        finally:
            try:
                if process is not None:
                    await process.close()
            finally:
                try:
                    if service is not None:
                        await service.close()
                finally:
                    try:
                        if daily_log_service is not None:
                            await daily_log_service.aclose()
                    finally:
                        try:
                            if network is not None:
                                await network.delete()
                        finally:
                            await docker_client.close()
