"""Opt-in installed-CLI checks; synthetic Gmail only, with real HTTP and Docker.

Run with CLAUDE_GMAIL_INTEGRATION=1 and ANTHROPIC_API_KEY available. This makes
billable model calls. The existing built image is pinned by digest during the run;
no build, pull, live Google account, or existing app container is used.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiodocker
import pytest
import uvicorn
from fastapi import FastAPI

from assistant_agent.chat.claude_process import ClaudeProcess
from assistant_agent.config import SandboxSettings, get_sandbox_settings
from assistant_agent.database import Base, User, make_async_engine, make_async_session_factory
from assistant_agent.gmail_mcp import GmailMCP
from assistant_agent.gmail_service import (
    Attachment, BinaryContent, EmailContent, SearchResult, ThreadResult,
)
from assistant_agent.sandbox import DockerClientRole, Sandbox
from assistant_agent.sandbox_access import SandboxAccessService
from assistant_agent.session_files import SessionFilesService


pytestmark = pytest.mark.skipif(
    os.getenv("CLAUDE_GMAIL_INTEGRATION") != "1",
    reason="Set CLAUDE_GMAIL_INTEGRATION=1 to run real Docker/model checks",
)


async def test_installed_claude_authenticated_gmail_and_permissions() -> None:
    """Discover without a model call, exercise tools, and enforce local restrictions."""
    assert os.getenv("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY is required"
    run_id = uuid.uuid4().hex
    docker_client = aiodocker.Docker()
    network = relay = sandbox_service = claude_process = None
    server_task = None
    file_service = None
    database_engine = None
    http_socket = socket.socket()
    http_socket.bind(("0.0.0.0", 0))
    http_socket.listen(128)
    port = http_socket.getsockname()[1]
    # /private/tmp avoids macOS /var and /tmp symlink ancestors, deliberately
    # rejected by the production session-file and sandbox path checks.
    with tempfile.TemporaryDirectory(prefix="claude-gmail-", dir="/private/tmp") as directory:
        root = Path(directory)
        try:
            image = await docker_client.images.inspect(get_sandbox_settings().image)
            image_id = image["Id"]
            network_name = f"claude-gmail-{run_id}"
            network = await docker_client.networks.create({"Name": network_name})
            # Relay the fixed production app:8000 URL to our real host HTTP
            # endpoint. Reuse node from the built image; never pull a proxy image.
            relay_script = (
                "const http=require('http');http.createServer((req,res)=>{"
                f"const upstream=http.request({{hostname:'host.docker.internal',port:{port},"
                "path:req.url,method:req.method,headers:{...req.headers,host:'app:8000'}},r=>{"
                "res.writeHead(r.statusCode,r.headers);r.pipe(res)});"
                "upstream.on('error',()=>{res.writeHead(502);res.end()});req.pipe(upstream)"
                "}).listen(8000,'0.0.0.0')"
            )
            relay = await docker_client.containers.create({
                "Image": image_id, "Cmd": ["node", "-e", relay_script], "User": "agent",
                "HostConfig": {"NetworkMode": network_name},
                "NetworkingConfig": {"EndpointsConfig": {
                    network_name: {"Aliases": ["app"]},
                }},
            }, name=f"claude-gmail-relay-{run_id}")
            await relay.start()
            sandbox_service = Sandbox(SandboxSettings(
                image=image_id, network=network_name, deployment_id=f"test-{run_id}",
                host_input_root=root / "inputs", app_input_root=root / "inputs",
            ))
            handle = await sandbox_service.allocate("synthetic-user", run_id)
            container = await sandbox_service._client(DockerClientRole.CONTROL).containers.get(
                handle.container_id
            )
            container_config = container["Config"]
            forbidden = ("GOOGLE", "CREDENTIAL_ENCRYPTION_KEY", "DATABASE_URL",
                         "ASSISTANT_MCP_TOKEN", "ANTHROPIC_API_KEY")
            assert not any(
                any(entry.split("=", 1)[0].startswith(name) for name in forbidden)
                for entry in container_config.get("Env", [])
            )
            assert container["HostConfig"]["Binds"] == [f"{handle.host_input_path}:/input:ro"]
            versions = await sandbox_service.run(
                "claude --version && codex --version", name=handle.container_id,
            )
            assert versions.ok
            print(f"Built image: {image_id}; installed CLIs: {versions.output.strip()}")
            os_write = await sandbox_service.run(
                "touch /input/os-write-must-fail", name=handle.container_id,
            )
            assert not os_write.ok and not (handle.app_input_path / "os-write-must-fail").exists()

            database_engine = make_async_engine(f"sqlite+aiosqlite:///{root / 'test.db'}")
            session_factory = make_async_session_factory(database_engine)
            async with database_engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with session_factory.begin() as database_session:
                database_session.add(User(
                    user_id="synthetic-user", google_sub="synthetic-user",
                    email="synthetic@example.invalid", credentials=b"synthetic-unused", scopes="[]",
                ))
            access_service = SandboxAccessService(session_factory)
            issued_grant = await access_service.issue("synthetic-user", run_id, handle.container_id)
            email = EmailContent(
                "synthetic-message", "synthetic-thread", body="SYNTHETIC_GMAIL_MARKER",
                headers={"Subject": "Synthetic integration fixture"},
                attachments=[Attachment("1", "synthetic-attachment", "fixture.txt", "text/plain", 7,
                                        False)],
            )
            gmail_service = SimpleNamespace(
                search_emails=AsyncMock(return_value=SearchResult([email], 1, None, False, False, [])),
                get_email=AsyncMock(return_value=email),
                get_thread=AsyncMock(return_value=ThreadResult(
                    "synthetic-thread", [email], [email.message_id], 0, False,
                )),
                get_attachment=AsyncMock(return_value=BinaryContent(
                    "synthetic-message", b"fixture", attachment_id="synthetic-attachment",
                )),
            )
            file_service = SessionFilesService(gmail_service)
            gmail_mcp = GmailMCP(
                gmail_service=gmail_service, session_files_service=file_service,
                access_service=access_service,
                resolve_live_assignment=lambda *args: handle,
                base_url="http://app:8000",
            )
            application = FastAPI()
            application.mount("/mcp", gmail_mcp.http_app)
            server = uvicorn.Server(uvicorn.Config(
                application, log_level="critical", access_log=False, timeout_graceful_shutdown=1,
            ))
            async with gmail_mcp.run():
                server_task = asyncio.create_task(server.serve(sockets=[http_socket]))
                async with asyncio.timeout(10):
                    while not server.started:
                        await asyncio.sleep(0.05)
                await sandbox_service.provision_mcp(handle, gmail_enabled=True)
                config_write = await sandbox_service.run(
                    "test ! -w /run/assistant/mcp.json && test ! -w /run/assistant",
                    name=handle.container_id,
                )
                assert config_write.ok
                events: list[dict] = []
                failure_messages: list[str] = []
                claude_process = await ClaudeProcess.create(
                    run_id, events.append, failure_messages.append, sandbox_service, handle,
                    mcp_token=issued_grant.raw_token, gmail_enabled=True,
                )
                del issued_grant
                await claude_process.discover_gmail()
                assert not any(event.get("type") == "result" for event in events)

                async def turn(prompt: str) -> list[dict]:
                    """Collect one turn without printing emails or control responses."""
                    start = len(events)
                    await claude_process.send(prompt)
                    async with asyncio.timeout(180):
                        while not any(event.get("type") == "result" for event in events[start:]):
                            assert not failure_messages, "Claude transport failed"
                            await asyncio.sleep(0.1)
                    output = events[start:]
                    result = next(event for event in output if event.get("type") == "result")
                    assert not result.get("is_error"), "Model turn failed"
                    return output

                def tool_names(output: list[dict]) -> set[str]:
                    """Read complete assistant tool-use blocks, without duplicating deltas."""
                    return {
                        block["name"] for event in output if event.get("type") == "assistant"
                        for block in event.get("message", {}).get("content", [])
                        if block.get("type") == "tool_use"
                    }

                def successful_results(output: list[dict], minimum: int) -> list[dict]:
                    """Require successful actual tool responses, beyond attempted calls."""
                    results = [
                        block for event in output if event.get("type") == "user"
                        for block in event.get("message", {}).get("content", [])
                        if block.get("type") == "tool_result"
                    ]
                    assert len(results) >= minimum
                    assert not any(block.get("is_error") for block in results)
                    return results

                gmail_turn = await turn(
                    "Run this synthetic integration check. Actually call all five Gmail tools: "
                    "search_emails(query='synthetic'), get_email(message_id='synthetic-message'), "
                    "get_thread(thread_id='synthetic-thread'), "
                    "download_emails(message_ids=['synthetic-message']), "
                    "download_attachment(message_id='synthetic-message', "
                    "attachment_id='synthetic-attachment'). Use the exact IDs. Report success briefly."
                )
                assert {
                    "mcp__gmail__search_emails", "mcp__gmail__get_email", "mcp__gmail__get_thread",
                    "mcp__gmail__download_emails", "mcp__gmail__download_attachment",
                } <= tool_names(gmail_turn)
                successful_results(gmail_turn, 5)
                downloaded = next(handle.app_input_path.rglob("*.txt"))
                input_path = "/input/" + str(downloaded.relative_to(handle.app_input_path))
                local_turn = await turn(
                    f"Actually use Read on {input_path}, then Bash 'ls /input' and Bash "
                    "'rg SYNTHETIC_GMAIL_MARKER /input'. Do all three and briefly confirm."
                )
                assert {"Read", "Bash"} <= tool_names(local_turn)
                local_results = successful_results(local_turn, 3)
                assert sum("SYNTHETIC_GMAIL_MARKER" in str(block) for block in local_results) >= 2
                bash_inputs = [
                    block["input"]["command"] for event in local_turn
                    if event.get("type") == "assistant"
                    for block in event.get("message", {}).get("content", [])
                    if block.get("type") == "tool_use" and block["name"] == "Bash"
                ]
                assert any(command.startswith("ls ") for command in bash_inputs)
                assert any(command.startswith("rg ") for command in bash_inputs)
                denial_turn = await turn(
                    "This tests permission denial. Attempt Bash with the exact command "
                    "'touch /workspace/unapproved-must-not-exist'. Then attempt Bash with "
                    "'touch /input/claude-write-must-fail'. Attempt both even if denied; "
                    "do not use another command or tool as a workaround."
                )
                assert "Bash" in tool_names(denial_turn)
                denied_results = [
                    block for event in denial_turn if event.get("type") == "user"
                    for block in event.get("message", {}).get("content", [])
                    if block.get("type") == "tool_result" and block.get("is_error")
                ]
                assert len(denied_results) >= 2
                sentinels = await sandbox_service.run(
                    "test ! -e /workspace/unapproved-must-not-exist && "
                    "test ! -e /input/claude-write-must-fail", name=handle.container_id,
                )
                assert sentinels.ok
                await claude_process.close()
                claude_process = None
                server.should_exit = True
                await asyncio.wait_for(server_task, 10)
                server_task = None
        finally:
            original_failure = sys.exc_info()[0] is not None
            cleanup_errors: list[Exception] = []
            cleanup_operations = []
            if claude_process is not None:
                cleanup_operations.append(claude_process.close)
            if server_task is not None:
                server.should_exit = True

                async def stop_server() -> None:
                    """Join the bounded server shutdown while still cleaning other resources."""
                    await asyncio.wait_for(server_task, 10)

                cleanup_operations.append(stop_server)
            if file_service is not None:
                cleanup_operations.append(file_service.aclose)
            if sandbox_service is not None:
                cleanup_operations.append(sandbox_service.close)
            if relay is not None:
                cleanup_operations.append(lambda: relay.delete(force=True))
            if network is not None:
                cleanup_operations.append(network.delete)
            if database_engine is not None:
                cleanup_operations.append(database_engine.dispose)
            cleanup_operations.append(docker_client.close)
            for cleanup_operation in cleanup_operations:
                try:
                    await cleanup_operation()
                except Exception as error:
                    cleanup_errors.append(error)
            http_socket.close()
            if cleanup_errors and not original_failure:
                raise ExceptionGroup("Integration resource cleanup failed", cleanup_errors)
