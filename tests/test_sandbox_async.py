"""Public aiodocker API contracts and opt-in Compose sandbox checks."""

import asyncio
import os
import shlex
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiodocker.stream import Message

from assistant_agent import chat, sandbox
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
                await service.run("echo test")
        else:
            result = await service.run("echo test", name="override", workdir="/tmp")
            assert result == sandbox.ExecResult(code, "雪�")
        assert stream.closed == 1
        clients[0].containers.get.assert_awaited_with(
            "override" if code is not None else sandbox.container_name()
        )
        args = container.exec.call_args.kwargs
        assert args == dict(
            stdin=False,
            stdout=True,
            stderr=True,
            tty=False,
            user="agent",
            workdir="/tmp" if code is not None else "/workspace",
        )
        # Interactive execs use another HTTP pool; repeat control calls reuse theirs.
        await service.exec("read", stdin=True)
        await service.exec("true")
        assert len(clients) == 2
    assert all(client.close.await_count == 1 for client in clients)


async def test_command_deadline_closes_stream():
    stream = CommandStream()
    async with sandbox.Sandbox() as service:
        service.exec = AsyncMock(return_value=SimpleNamespace(start=lambda: stream))
        with pytest.raises(sandbox.SandboxError):
            await service.run("hang", timeout=0.01)
        assert stream.closed == 1


pytestmark_docker = pytest.mark.skipif(
    os.getenv("SANDBOX_DOCKER_INTEGRATION") != "1", reason="Opt-in Docker transport tests"
)


@pytestmark_docker
async def test_docker_exit_codes_and_separate_interactive_streams():
    async with sandbox.Sandbox() as service:
        result = await service.run("printf '\\377'; printf error >&2; exit 7")
        # Docker may deliver the two file descriptors in either order.
        assert result.exit_code == 7 and result.output in {"�error", "error�"}
        execution = await service.exec(
            'while IFS= read -r line; do printf "out:%s\\n" "$line"; '
            'printf "err:%s\\n" "$line" >&2; done',
            stdin=True,
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
        try:
            await manager.submit("test", "first")
            conversation = manager.get("test")
            process = conversation.process
            async with asyncio.timeout(5):
                while conversation.active:
                    await asyncio.sleep(0.01)
            await manager.submit("test", "雪 second")
            async with asyncio.timeout(5):
                while conversation.active:
                    await asyncio.sleep(0.01)
            assert not conversation.failed and conversation.process is process
            assert conversation.transcript[-1]["text"] == "雪 second"
            child = (await service.run(f"cat {shlex.quote(child_path)}")).output.strip()
            assert child.isdigit()
            await manager.reset("test")
            assert process.reader.done() and process.closed
            assert not (await process.execution.inspect())["Running"]
            # A dead descendant can remain a zombie until the container's init reaps it.
            state = await service.run(
                f'test ! -e /proc/{child}/stat || test "$(cut -d " " -f 3 /proc/{child}/stat)" = Z'
            )
            assert state.ok
            assert (await service.run(f"test ! -e {shlex.quote(process.path)}")).ok
        finally:
            await manager.close()
            await service.run(f"rm -f {shlex.quote(child_path)}")


async def test_failed_stream_open_releases_partial_attachment():
    class BrokenOpen(CommandStream):
        async def __aenter__(self):
            self.opened = True
            raise RuntimeError("attach failed")

    stream = BrokenOpen()
    async with sandbox.Sandbox() as service:
        service.exec = AsyncMock(return_value=SimpleNamespace(start=lambda: stream))
        with pytest.raises(sandbox.SandboxError, match="attach failed"):
            await service.run("true")
        assert stream.closed == 1
