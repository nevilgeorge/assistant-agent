"""Claude launch permissions and correlated MCP startup discovery."""

import asyncio
import io
import json
import shlex
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant_agent import sandbox
from assistant_agent.chat import claude_process


class ControlStream:
    """Respond to control requests without performing a model call."""

    def __init__(self, statuses=None, *, malformed=False, eof=False, silence=False):
        self.messages = asyncio.Queue()
        self.writes = []
        self.statuses = list(statuses or [])
        self.malformed = malformed
        self.eof = eof
        self.silence = silence
        self.closed = False

    async def __aenter__(self):
        return self

    async def write_in(self, data):
        request = json.loads(data)
        self.writes.append(request)
        if request["type"] != "control_request" or self.silence:
            return
        if self.eof:
            self.messages.put_nowait(None)
            return
        subtype = request["request"]["subtype"]
        payload = self.statuses.pop(0) if subtype == "mcp_status" else {}
        response = {"type": "control_response", "response": {
            "subtype": "success", "request_id": request["request_id"], "response": payload,
        }}
        if self.malformed:
            response["response"]["response"] = []
        self.messages.put_nowait(SimpleNamespace(stream=1, data=json.dumps(response).encode()+b"\n"))

    async def read_out(self):
        return await self.messages.get()

    async def close(self):
        self.closed = True


def handle():
    return sandbox.SandboxHandle("container", "user", "abc", Path("/tmp"), Path("/tmp"))


async def launch(stream, *, gmail=True):
    service = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(start=lambda: stream)),
        run=AsyncMock(return_value=sandbox.ExecResult(0, "")),
    )
    received, failed = [], []
    process = await claude_process.ClaudeProcess.create(
        "conversation", received.append, failed.append, service, handle(),
        gmail_enabled=gmail, mcp_token="synthetic-grant" if gmail else None,
    )
    return process, service, received, failed


def connected():
    return {"mcpServers": [{"name": "gmail", "status": "connected", "tools": [
        {"name": name} for name in claude_process.GMAIL_TOOLS
    ], "config": {"headers": {"Authorization": "synthetic-secret"}}}]}


async def test_exact_permissions_and_exec_only_token_injection():
    process, service, _, _ = await launch(ControlStream())
    command = service.exec.call_args.args[0]
    environment = service.exec.call_args.kwargs["environment"]
    assert environment["ASSISTANT_MCP_TOKEN"] == "synthetic-grant"
    assert environment["MCP_TIMEOUT"] == "10000"
    assert "synthetic-grant" not in command
    assert "synthetic-grant" not in repr(process.__dict__)
    inner = shlex.split(command)[-1]
    for flag in ("--restricted", "--no-session-persistence", "--strict-mcp-config",
                 "--add-dir /input", "--permission-mode dontAsk", "Read,Glob,Grep,Bash"):
        assert flag in inner
    assert "--disallowedTools" not in inner
    for tool in claude_process.GMAIL_TOOLS:
        assert tool in inner
    await process.close()


async def test_chat_only_has_no_token_and_denies_mcp():
    process, service, _, _ = await launch(ControlStream(), gmail=False)
    assert "ASSISTANT_MCP_TOKEN" not in service.exec.call_args.kwargs["environment"]
    command = shlex.split(service.exec.call_args.args[0])[-1]
    assert "--disallowedTools 'mcp__*'" in command
    assert "Gmail is unavailable in this conversation. Reset to retry." in command
    assert not any(name in command for name in claude_process.GMAIL_TOOLS)
    await process.close()


@pytest.mark.parametrize("bare_names", [False, True])
async def test_discovery_without_prompt_consumes_sensitive_control_responses(caplog, bare_names):
    status = connected()
    if bare_names:
        for tool in status["mcpServers"][0]["tools"]:
            tool["name"] = tool["name"].removeprefix("mcp__gmail__")
    stream = ControlStream([{"mcpServers": [{"name": "gmail", "status": "pending"}]}, status])
    process, _, received, failed = await launch(stream)
    await process.discover_gmail()
    assert [item["request"]["subtype"] for item in stream.writes] == [
        "initialize", "mcp_status", "mcp_status",
    ]
    assert not received and not failed
    assert "synthetic-secret" not in caplog.text
    assert not process.control_requests
    await process.send("first")
    await process.send("second")
    assert [item["message"]["content"] for item in stream.writes if item["type"] == "user"] == [
        "first", "second",
    ]
    await process.close()


@pytest.mark.parametrize("status", [
    {"mcpServers": [{"name": "gmail", "status": "failed"}]},
    {"mcpServers": [{"name": "gmail", "status": "needs-auth"}]},
    {"mcpServers": [{"name": "gmail", "status": "connected", "tools": []}]},
])
async def test_discovery_unavailable_is_separate_from_transport_failure(status):
    process, _, received, failed = await launch(ControlStream([status]))
    with pytest.raises(claude_process.GmailUnavailable):
        await process.discover_gmail()
    assert not received and not failed and not process.is_closed
    await process.close()


@pytest.mark.parametrize("kwargs", [{"malformed": True}, {"eof": True}])
async def test_protocol_failure_and_eof_abort_discovery(kwargs):
    process, _, received, failed = await launch(ControlStream([connected()], **kwargs))
    with pytest.raises(claude_process.ClaudeProtocolError):
        await process.discover_gmail()
    assert not received and len(failed) == 1
    await process.close()


async def test_discovery_deadline(monkeypatch):
    monkeypatch.setattr(claude_process, "DISCOVERY_SECONDS", 0.01)
    process, _, received, failed = await launch(ControlStream(silence=True))
    with pytest.raises(claude_process.GmailUnavailable, match="gmail_discovery_timeout"):
        await process.discover_gmail()
    assert not process.control_requests and not received and not failed
    await process.close()


async def test_termination_failure_is_not_swallowed():
    process, service, _, _ = await launch(ControlStream())
    service.run.return_value = sandbox.ExecResult(1, "")
    with pytest.raises(sandbox.SandboxError, match="termination failed"):
        await process.close()
    assert process.docker_stream.closed


@pytest.mark.parametrize("enabled", [True, False])
async def test_archive_provisions_root_owned_secret_free_config(enabled):
    container = SimpleNamespace(put_archive=AsyncMock())
    service = sandbox.Sandbox.__new__(sandbox.Sandbox)
    service._client = lambda role: SimpleNamespace(containers=SimpleNamespace(
        get=AsyncMock(return_value=container),
    ))
    await service.provision_mcp(handle(), enabled)
    destination, archive_bytes = container.put_archive.call_args.args
    assert destination == "/run"
    with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
        directory = archive.getmember("assistant")
        config_file = archive.getmember("assistant/mcp.json")
        assert directory.uid == directory.gid == config_file.uid == config_file.gid == 0
        assert directory.mode == 0o755 and config_file.mode == 0o644
        config = json.load(archive.extractfile(config_file))
    if enabled:
        assert config == {"mcpServers": {"gmail": {"type": "http",
            "url": "http://app:8000/mcp/gmail",
            "headers": {"Authorization": "Bearer ${ASSISTANT_MCP_TOKEN}"}}}}
    else:
        assert config == {"mcpServers": {}}


@pytest.mark.parametrize("status", [
    {}, {"mcpServers": {}}, {"mcpServers": [None]},
    {"mcpServers": [{"name": "gmail", "status": "connected", "tools": [None]}]},
])
async def test_malformed_status_is_protocol_failure(status):
    process, _, received, failed = await launch(ControlStream([status]))
    with pytest.raises(claude_process.ClaudeProtocolError):
        await process.discover_gmail()
    assert not received and not failed
    await process.close()
