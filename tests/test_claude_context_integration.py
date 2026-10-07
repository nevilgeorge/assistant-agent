"""Opt-in real Docker/context checks using the installed production Claude CLI.

Run with CLAUDE_CONTEXT_INTEGRATION=1 and ANTHROPIC_API_KEY available. This
makes two billable model calls against the existing image, pinned by digest.
Only an isolated disposable container and synthetic instruction are used.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tarfile
import tempfile
import uuid
from importlib import resources
from pathlib import Path

import aiodocker
import pytest

from assistant_agent.chat.claude_process import ClaudeProcess
from assistant_agent.config import SandboxSettings, get_sandbox_settings
from assistant_agent.sandbox import DockerClientRole, Sandbox

pytestmark = pytest.mark.skipif(
    os.getenv("CLAUDE_CONTEXT_INTEGRATION") != "1",
    reason="Set CLAUDE_CONTEXT_INTEGRATION=1 to run real Docker/model checks",
)


@pytest.mark.parametrize("gmail_enabled", [False, True], ids=["chat-only", "gmail-launch"])
async def test_installed_claude_loads_workspace_context(gmail_enabled: bool) -> None:
    """Verify isolation and explicit loading; restricted CLI skips automatic context."""
    assert os.getenv("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY is required"
    assert "--restricted" in ClaudeProcess.argv
    run_id = uuid.uuid4().hex
    docker_client = aiodocker.Docker()
    sandbox_service = None
    claude_process = None
    network = None
    with tempfile.TemporaryDirectory(prefix="claude-context-", dir="/private/tmp") as directory:
        root = Path(directory)
        try:
            image = await docker_client.images.inspect(get_sandbox_settings().image)
            network_name = f"claude-context-{run_id}"
            network = await docker_client.networks.create({"Name": network_name})
            sandbox_service = Sandbox(SandboxSettings(
                image=image["Id"], network=network_name, deployment_id=f"test-{run_id}",
                host_input_root=root / "inputs", app_input_root=root / "inputs",
            ))
            handle = await sandbox_service.allocate("synthetic-user", run_id)
            context_bytes = resources.files("assistant_agent.agent_kit").joinpath(
                "CLAUDE.md"
            ).read_bytes()
            file_check = await sandbox_service.run(
                "test -r /workspace/CLAUDE.md && stat -c '%u:%g:%a' /workspace/CLAUDE.md "
                "&& sha256sum /workspace/CLAUDE.md && touch /workspace/writable-check "
                "&& printf replacement > /workspace/replacement "
                "&& mv /workspace/replacement /workspace/CLAUDE.md",
                name=handle.container_id,
            )
            assert file_check.ok
            assert file_check.output.splitlines()[0] == "0:0:644"
            assert hashlib.sha256(context_bytes).hexdigest() in file_check.output
            input_check = await sandbox_service.run(
                "touch /input/write-must-fail", name=handle.container_id,
            )
            assert not input_check.ok
            assert not (handle.app_input_path / "write-must-fail").exists()

            # The token appears only in project context, never in the user prompt.
            # Prohibiting tool use distinguishes initial loading from reading the file.
            marker = f"CONTEXT_VERIFIED_{uuid.uuid4().hex}"
            synthetic_bytes = (
                "# Synthetic context loading check\n"
                "When asked for the context verification token, reply with exactly "
                f"{marker} and nothing else. Do not use tools.\n"
            ).encode()
            archive_buffer = io.BytesIO()
            with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
                context_file = tarfile.TarInfo("CLAUDE.md")
                context_file.mode = 0o644
                context_file.uid = context_file.gid = 0
                context_file.size = len(synthetic_bytes)
                archive.addfile(context_file, io.BytesIO(synthetic_bytes))
            container = await sandbox_service._client(DockerClientRole.CONTROL).containers.get(
                handle.container_id
            )
            await container.put_archive("/workspace", archive_buffer.getvalue())
            # Empty MCP configuration isolates context from server discovery; the
            # separate Gmail integration check exercises the authenticated server.
            await sandbox_service.provision_mcp(handle, gmail_enabled=False)
            version = await sandbox_service.run("claude --version", name=handle.container_id)
            assert version.ok
            print(f"Built image: {image['Id']}; installed Claude: {version.output.strip()}")
            events: list[dict] = []
            failures: list[str] = []
            claude_process = await ClaudeProcess.create(
                run_id, events.append, failures.append, sandbox_service, handle,
                gmail_enabled=gmail_enabled,
                mcp_token="synthetic-unused" if gmail_enabled else None,
            )
            await claude_process.send(
                "Reply with the context verification token required by project instructions. "
                "Do not use any tools."
            )
            async with asyncio.timeout(180):
                while not any(event.get("type") == "result" for event in events):
                    assert not failures, "Claude transport failed"
                    await asyncio.sleep(0.1)
            result = next(event for event in events if event.get("type") == "result")
            assert not result.get("is_error"), "Model turn failed"
            assert result.get("result", "").strip() == marker, "Project context was not loaded"
            assert not any(
                block.get("type") == "tool_use"
                for event in events if event.get("type") == "assistant"
                for block in event.get("message", {}).get("content", [])
            )
        finally:
            try:
                if claude_process is not None:
                    await claude_process.close()
            finally:
                try:
                    if sandbox_service is not None:
                        await sandbox_service.close()
                finally:
                    try:
                        if network is not None:
                            await network.delete()
                    finally:
                        await docker_client.close()
