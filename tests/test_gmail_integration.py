"""Opt-in full application/PostgreSQL/Docker/Claude tests with synthetic Google data.

Run GMAIL_MCP_INTEGRATION=1 .venv/bin/pytest -q -s tests/test_gmail_integration.py.
Uses existing images only and makes billable model calls. All infrastructure is disposable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from assistant_agent.database import SandboxAccessToken
from gmail_phase6_harness import IntegrationHarness, tool_data

pytestmark = pytest.mark.skipif(
    os.getenv("GMAIL_MCP_INTEGRATION") != "1",
    reason="Set GMAIL_MCP_INTEGRATION=1 for full-app Docker/model checks",
)


@pytest.fixture
async def integration_harness(monkeypatch):
    """Construct real application services, replacing only Google boundaries."""
    assert os.getenv("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY is required"
    harness = IntegrationHarness(monkeypatch)
    try:
        await harness.prepare()
        print("Gmail phase 6 built image: " + harness.image_id)
        yield harness
    finally:
        await harness.close()


async def replay(user, snapshot: dict, after: int = 0) -> list[dict]:
    """Read actual SSE until the known snapshot sequence is reached."""
    events: list[dict] = []
    async with asyncio.timeout(15):
        async with user.client.stream("GET", "/api/conversation/stream", params={
            "conversation_id": snapshot["conversation_id"], "after": after,
        }) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    events.append(event)
                    if event.get("sequence", 0) >= snapshot["sequence"]:
                        break
    return events


async def assert_retired(harness, handle, grant) -> None:
    """Check container deletion, file retirement, and actual endpoint grant rejection."""
    assert not handle.app_input_path.exists()
    with pytest.raises(Exception) as caught:
        await (await harness.docker.containers.get(handle.container_id)).show()
    assert getattr(caught.value, "status", None) == 404
    async with httpx.AsyncClient(base_url=harness.base_url) as client:
        assert (await client.post("/mcp/gmail", headers={
            "Authorization": f"Bearer {grant.raw_token}"})).status_code == 401


async def test_full_app_workflows_two_users_and_revocation(integration_harness):
    """Exercise real model tools, MIME processing, files, HTTP sessions, and isolation."""
    harness = integration_harness
    alice, bob = await harness.connect("alice"), await harness.connect("bob")
    assert (await alice.client.post("/api/message", json={"message": "hi"})).status_code == 400
    async with httpx.AsyncClient(base_url=harness.base_url) as anonymous:
        assert (await anonymous.get("/api/conversation")).status_code == 401
        assert (await anonymous.post("/mcp/gmail")).status_code == 401
        assert (await anonymous.post("/mcp/gmail", headers={
            "Authorization": "Bearer deliberately-invalid"})).status_code == 401
    assert (await alice.client.post("/mcp/gmail")).status_code == 401
    snapshots = await asyncio.gather(*(
        harness.submit(user, f"Actually use all five Gmail tools: search_emails(query='fixture'), "
                       f"get_email(message_id='{user.name}-1'), get_thread(thread_id='{user.name}-thread'), "
                       f"download_emails(message_ids=['{user.name}-1']), download_attachment("
                       f"message_id='{user.name}-1', attachment_id='fixture-attachment'). "
                       "Read the downloaded email using Read and run Bash 'ls /input' and "
                       "Bash 'rg SYNTHETIC_GMAIL /input'. Briefly report the marker found.")
        for user in (alice, bob)))
    handles = {user.name: harness.app.state.conversation_manager.get(user.user_id).sandbox_handle
               for user in (alice, bob)}
    assert handles["alice"].container_id != handles["bob"].container_id
    for user, snapshot in zip((alice, bob), snapshots, strict=True):
        assert snapshot["gmail_status"] == "ready"
        assert {method for name, method in harness.calls if name == user.name} >= {
            "list", "message", "thread", "attachment"}
        files = list(handles[user.name].app_input_path.rglob("*.txt"))
        assert files
        all_bytes = b"".join(path.read_bytes() for path in files)
        assert f"SYNTHETIC_GMAIL_{user.name}_1".encode() in all_bytes
        other = "bob" if user.name == "alice" else "alice"
        assert f"SYNTHETIC_GMAIL_{other}".encode() not in all_bytes
        trace = harness.traces[user.user_id]
        tools = [block for event in trace if event.get("type") == "assistant"
                 for block in event.get("message", {}).get("content", [])
                 if block.get("type") == "tool_use"]
        assert {block["name"] for block in tools} >= {
            "mcp__gmail__search_emails", "mcp__gmail__get_email", "mcp__gmail__get_thread",
            "mcp__gmail__download_emails", "mcp__gmail__download_attachment", "Read", "Bash"}
        commands = [block["input"]["command"] for block in tools if block["name"] == "Bash"]
        assert any(command.startswith("ls ") for command in commands)
        assert any(command.startswith("rg ") for command in commands)
        results = [block for event in trace if event.get("type") == "user"
                   for block in event.get("message", {}).get("content", [])
                   if block.get("type") == "tool_result"]
        assert len(results) >= 8 and not any(block.get("is_error") for block in results)
        events = await replay(user, snapshot)
        assert any(event["type"] == "gmail_status" and event["gmail_status"] == "ready"
                   for event in events)
        assert events[-1]["type"] == "turn_completion"
        resumed = await replay(user, snapshot, events[-2]["sequence"])
        assert resumed == [events[-1]]
    async with bob.client.stream("GET", "/api/conversation/stream", params={
        "conversation_id": snapshots[0]["conversation_id"]}) as response:
        lines = response.aiter_lines()
        assert "reload" in await anext(lines)
    async with harness.mcp(alice) as session:
        first = tool_data(await session.call_tool("search_emails", {"query": "fixture", "page_size": 1}))
        assert first["messages"][0]["message_id"] == "alice-1"
        second = tool_data(await session.call_tool("search_emails", {
            "query": "fixture", "page_size": 1, "cursor": first["cursor"]}))
        assert second["messages"][0]["message_id"] == "alice-2"
        mismatch = await session.call_tool("search_emails", {
            "query": "different-query", "cursor": first["cursor"]})
        assert mismatch.is_error
        previews = tool_data(await session.call_tool("search_emails", {"query": "partial-preview"}))
        assert previews["metadata_incomplete"] and previews["errors"]
        forbidden = await session.call_tool("get_email", {"message_id": "bob-1"})
        assert forbidden.is_error
        partial = tool_data(await session.call_tool("download_emails", {
            "message_ids": ["alice-2", "missing-message"]}))
        assert partial["items"][0]["success"] and not partial["items"][1]["success"]
        for format_name in ("json", "eml"):
            downloaded = tool_data(await session.call_tool("download_emails", {
                "message_ids": ["alice-1"], "format": format_name}))
            item = downloaded["items"][0]
            assert item["success"]
            content = (handles["alice"].app_input_path / item["path"].removeprefix("/input/")).read_bytes()
            if format_name == "json":
                assert json.loads(content)["body"] == "SYNTHETIC_GMAIL_alice_1"
            else:
                assert content == b"Subject: Fixture alice\r\n\r\nSYNTHETIC_GMAIL_alice-1\r\n"
        attachment = tool_data(await session.call_tool("download_attachment", {
            "message_id": "alice-1", "attachment_id": "fixture-attachment"}))
        item = attachment
        content = (handles["alice"].app_input_path / item["path"].removeprefix("/input/")).read_bytes()
        assert hashlib.sha256(content).digest() == hashlib.sha256(b"ATTACHMENT_alice").digest()
    async with harness.mcp(bob) as session:
        stolen_cursor = await session.call_tool("search_emails", {
            "query": "fixture", "cursor": first["cursor"]})
        assert stolen_cursor.is_error
    alice_grant = harness.grants[alice.user_id]
    await harness.reset(alice)
    await assert_retired(harness, handles["alice"], alice_grant)
    assert (await bob.client.get("/api/conversation")).json()["gmail_status"] == "ready"
    await harness.submit(bob, "Reply briefly: still chatting.")
    bob_grant = harness.grants[bob.user_id]
    response = await bob.client.post("/disconnect", data={"csrf_token": bob.csrf})
    assert response.status_code == 303
    await assert_retired(harness, handles["bob"], bob_grant)
    assert (await bob.client.get("/api/conversation")).status_code == 401


async def test_full_app_fallback_reset_and_expiry(integration_harness):
    """Discovery failure retains chat and replay status; reset retries, expiry retires."""
    harness = integration_harness
    user = await harness.connect("fallback")
    harness.mcp_available = False
    snapshot = await harness.submit(user, "Reply briefly: hello.")
    conversation = harness.app.state.conversation_manager.get(user.user_id)
    process = conversation.claude_process
    assert snapshot["gmail_status"] == "unavailable"
    assert [enabled for conversation_id, enabled in harness.provisions
            if conversation_id == conversation.conversation_id] == [True, False]
    assert sum(message["role"] == "user" for message in snapshot["transcript"]) == 1
    events = await replay(user, snapshot)
    assert any(event.get("gmail_status") == "unavailable" for event in events)
    snapshot = await harness.submit(user, "Reply briefly: second turn.")
    assert snapshot["gmail_status"] == "unavailable"
    assert len(snapshot["transcript"]) == 4
    assert harness.app.state.conversation_manager.get(user.user_id).claude_process is process
    config = json.loads((await harness.service.run(
        "cat /run/assistant/mcp.json", name=conversation.sandbox_handle.container_id)).output)
    assert config == {"mcpServers": {}}
    harness.mcp_available = True
    grant = harness.grants[user.user_id]
    handle = conversation.sandbox_handle
    await harness.reset(user)
    await assert_retired(harness, handle, grant)
    snapshot = await harness.submit(user, "Reply briefly: reset complete.")
    assert snapshot["gmail_status"] == "ready"
    conversation = harness.app.state.conversation_manager.get(user.user_id)
    handle, grant = conversation.sandbox_handle, harness.grants[user.user_id]
    conversation.access_token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    async with harness.app.state.web_store.factory.begin() as database:
        await database.execute(update(SandboxAccessToken).where(
            SandboxAccessToken.id == grant.token_id).values(expires_at=conversation.access_token_expires_at))
    assert (await user.client.post("/mcp/gmail", headers={
        "Authorization": f"Bearer {grant.raw_token}"})).status_code == 401
    await harness.app.state.conversation_manager.sweep_once()
    await assert_retired(harness, handle, grant)


async def test_full_app_restart_recovery_and_oauth_failure(integration_harness):
    """Shutdown releases assignments; startup removes seeded orphans and revokes grants."""
    harness = integration_harness
    user = await harness.connect("restart")
    await harness.submit(user, "Reply briefly: hello.")
    manager = harness.app.state.conversation_manager
    handle = manager.get(user.user_id).sandbox_handle
    grant = harness.grants[user.user_id]
    await harness.stop()
    assert not handle.app_input_path.exists()
    # Seed genuinely abandoned resources using a separate real service after shutdown.
    from assistant_agent.sandbox import Sandbox
    orphan_service = Sandbox()
    from assistant_agent.database import make_async_engine, make_async_session_factory
    from assistant_agent.sandbox_access import SandboxAccessService
    engine = make_async_engine()
    try:
        orphan = await orphan_service.allocate(user.user_id, uuid.uuid4().hex)
        (orphan.app_input_path / "abandoned.txt").write_text("synthetic orphan")
        access = SandboxAccessService(make_async_session_factory(engine))
        await access.invalidate_outstanding()
        orphan_grant = await access.issue(user.user_id, orphan.conversation_id, orphan.container_id)
        await harness.start()
        await assert_retired(harness, orphan, orphan_grant)
        await assert_retired(harness, handle, grant)
        assert (await user.client.get("/")).status_code == 200
        fresh = (await user.client.get("/api/conversation")).json()
        assert fresh["conversation_id"] != handle.conversation_id
        assert fresh["transcript"] == [] and fresh["gmail_status"] == "pending"
        async with harness.app.state.web_store.factory() as database:
            record = await database.scalar(select(SandboxAccessToken).where(
                SandboxAccessToken.id == orphan_grant.token_id))
            assert record.revoked_at is not None
    finally:
        await orphan_service.close()
        await engine.dispose()
    await harness.submit(user, "Reply briefly: restarted.")
    harness.oauth_failing.add(user.name)
    async with harness.mcp(user) as session:
        failure = await session.call_tool("search_emails", {"query": "fixture"})
        assert failure.is_error
        assert "reconnect_required" in failure.content[0].text
    conversation = harness.app.state.conversation_manager.get(user.user_id)
    handle, grant = conversation.sandbox_handle, harness.grants[user.user_id]
    await harness.stop()
    assert not handle.app_input_path.exists()
    with pytest.raises(Exception) as caught:
        await (await harness.docker.containers.get(handle.container_id)).show()
    assert getattr(caught.value, "status", None) == 404


async def test_full_app_baseline(integration_harness):
    """Record bounded cold-start/search/resource measurements without message bodies."""
    from gmail_phase6_checks import benchmark_harness
    report = await benchmark_harness(integration_harness)
    print("Gmail phase 6 baseline: " + json.dumps(report, sort_keys=True))
    for level in report["levels"]:
        assert level["errors"] == level["sampling_errors"] == 0
        assert level["startup"]["samples"] == 3 * level["concurrency"]
        assert level["search"]["samples"] == 15 * level["concurrency"]


async def test_full_app_reachability(integration_harness):
    """Record observed local network exposure and sandbox mount/environment boundaries."""
    from gmail_phase6_checks import audit_harness
    report = await audit_harness(integration_harness)
    print("Gmail phase 6 reachability: " + json.dumps(report, sort_keys=True))
