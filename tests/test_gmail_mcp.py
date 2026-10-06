"""HTTP transport, grant, and tool contract checks for the Gmail MCP endpoint."""

import asyncio
import base64
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update

from assistant_agent.database import (
    Base,
    SandboxAccessToken,
    User,
    make_async_engine,
    make_async_session_factory,
    utcnow,
)
from assistant_agent.gmail_mcp import GmailMCP, MCPLimits
from assistant_agent.gmail_service import (
    BinaryContent,
    EmailContent,
    GmailError,
    SearchResult,
    ThreadResult,
)
from assistant_agent.sandbox import SandboxHandle
from assistant_agent.sandbox_access import (
    AuthorizedSandboxContext,
    SandboxAccessService,
    SandboxAuthorizationError,
)
from assistant_agent.session_files import DownloadItem, DownloadResult, SessionFilesService


INITIALIZE = {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": {"name": "test-client", "version": "1"},
}
HEADERS = {
    "Authorization": "Bearer test-grant",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-11-25",
}


@asynccontextmanager
async def endpoint(limits=None, access_service=None, resolver=None, base_url="http://localhost:8000"):
    application = FastAPI()
    context = AuthorizedSandboxContext(
        "alice", "conversation-alice", "container-alice", Path("/host/input"), Path("/app/input")
    )
    email = EmailContent("message-one", "thread-one", body="body")
    gmail_service = SimpleNamespace(
        search_emails=AsyncMock(return_value=SearchResult([email], 1, None, False, False, [])),
        get_email=AsyncMock(return_value=email),
        get_thread=AsyncMock(
            return_value=ThreadResult("thread-one", [email], [email.message_id], 0, False)
        ),
    )
    download = DownloadItem(0, "message-one", success=True, path="/input/call/001.txt", bytes=4)
    file_service = SimpleNamespace(
        download_emails=AsyncMock(return_value=DownloadResult([download], 4)),
        download_attachment=AsyncMock(return_value=download),
    )
    if access_service is None:
        access_service = SimpleNamespace(
            authenticate=AsyncMock(return_value=context),
            authorize=AsyncMock(return_value=context),
        )
    gmail_mcp = GmailMCP(
        gmail_service=gmail_service,
        session_files_service=file_service,
        access_service=access_service,
        resolve_live_assignment=resolver or (lambda *args: None),
        base_url=base_url,
        limits=limits,
    )
    application.mount("/mcp", gmail_mcp.http_app)
    async with gmail_mcp.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url=base_url,
            headers=HEADERS,
        ) as client:
            yield SimpleNamespace(
                client=client,
                access=access_service,
                gmail=gmail_service,
                files=file_service,
                context=context,
                application=application,
                mcp=gmail_mcp,
            )


async def rpc(client, method, params=None, **kwargs):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    return await client.post("/mcp/gmail", json=payload, **kwargs)


async def call(client, name, arguments):
    return await rpc(client, "tools/call", {"name": name, "arguments": arguments})


def lifecycle_mcp():
    return GmailMCP(
        gmail_service=SimpleNamespace(), session_files_service=SimpleNamespace(),
        access_service=SimpleNamespace(), resolve_live_assignment=lambda *args: None,
        base_url="http://localhost:8000",
    )


@pytest.mark.parametrize("method,params", [("initialize", INITIALIZE), ("tools/list", {})])
@pytest.mark.parametrize("authorization", [None, "Basic token", "Bearer", "Bearer unknown"])
async def test_every_transport_method_requires_bearer(method, params, authorization):
    async with endpoint() as fixture:
        fixture.client.headers.pop("Authorization")
        if authorization is not None:
            fixture.client.headers["Authorization"] = authorization
        fixture.access.authenticate.side_effect = SandboxAuthorizationError()
        response = await rpc(fixture.client, method, params)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].lower().startswith("bearer")
        fixture.gmail.search_emails.assert_not_awaited()
        fixture.files.download_emails.assert_not_awaited()


async def test_initialization_discovery_and_default_search_contract():
    async with endpoint() as fixture:
        initialized = await rpc(fixture.client, "initialize", INITIALIZE)
        assert initialized.status_code == 200
        assert initialized.headers["content-type"].startswith("application/json")
        assert "mcp-session-id" not in initialized.headers
        listed = await rpc(fixture.client, "tools/list")
        assert listed.status_code == 200
        schemas = {tool["name"]: tool["inputSchema"] for tool in listed.json()["result"]["tools"]}
        assert set(schemas) == {
            "search_emails",
            "get_email",
            "get_thread",
            "download_emails",
            "download_attachment",
        }
        assert schemas["search_emails"]["properties"]["page_size"]["default"] == 50
        response = await call(fixture.client, "search_emails", {"query": "from:friend"})
        assert response.status_code == 200
        assert not response.json()["result"].get("isError", False)
        assert (
            response.json()["result"]["structuredContent"]["messages"][0]["message_id"]
            == "message-one"
        )
        assert fixture.gmail.search_emails.await_args.args[:2] == ("alice", "from:friend")
        assert (
            50 in fixture.gmail.search_emails.await_args.args
            or fixture.gmail.search_emails.await_args.kwargs.get("page_size") == 50
        )
        assert fixture.access.authenticate.await_count == 3
        assert fixture.access.authorize.await_args.args[:2] == ("test-grant", "search_emails")
        fixture.files.download_emails.assert_not_awaited()


async def test_revocation_after_initialize_denies_discovery_and_call():
    async with endpoint() as fixture:
        assert (await rpc(fixture.client, "initialize", INITIALIZE)).status_code == 200
        fixture.access.authenticate.side_effect = SandboxAuthorizationError()
        assert (await rpc(fixture.client, "tools/list")).status_code == 401
        assert (
            await call(fixture.client, "get_email", {"message_id": "message-one"})
        ).status_code == 401
        fixture.gmail.get_email.assert_not_awaited()


async def test_tool_reauthorizes_after_transport_authentication():
    async with endpoint() as fixture:
        fixture.access.authorize.side_effect = SandboxAuthorizationError()
        response = await call(fixture.client, "get_email", {"message_id": "message-one"})
        assert response.status_code == 200
        assert response.json()["result"]["isError"]
        fixture.access.authenticate.assert_awaited_once()
        fixture.access.authorize.assert_awaited_once()
        fixture.gmail.get_email.assert_not_awaited()


async def test_run_owns_sdk_lifetime_in_one_task_across_fixture_tasks(monkeypatch):
    gmail_mcp = lifecycle_mcp()
    owner_tasks = []
    started = asyncio.Event()
    stopped = asyncio.Event()

    @asynccontextmanager
    async def managed_sdk():
        owner_tasks.append(asyncio.current_task())
        started.set()
        try:
            yield
        finally:
            owner_tasks.append(asyncio.current_task())
            stopped.set()

    monkeypatch.setattr(gmail_mcp.server.session_manager, "run", managed_sdk)
    lifespan = gmail_mcp.run()
    await asyncio.create_task(lifespan.__aenter__())
    assert started.is_set() and not stopped.is_set()
    await asyncio.create_task(lifespan.__aexit__(None, None, None))
    assert stopped.is_set()
    assert owner_tasks[0] is owner_tasks[1]
    assert owner_tasks[0].done()


async def test_run_startup_failure_propagates_without_waiting_forever(monkeypatch):
    gmail_mcp = lifecycle_mcp()

    @asynccontextmanager
    async def failed_sdk():
        raise RuntimeError("startup failed")
        yield

    monkeypatch.setattr(gmail_mcp.server.session_manager, "run", failed_sdk)
    with pytest.raises(RuntimeError, match="startup failed"):
        async with gmail_mcp.run():
            pytest.fail("Startup failure yielded a running server")


@pytest.fixture
async def real_grant(tmp_path):
    database_engine = make_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mcp.db'}")
    session_factory = make_async_session_factory(database_engine)
    async with database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory.begin() as database_session:
        database_session.add(
            User(
                user_id="alice",
                google_sub="alice",
                email="alice@example.com",
                credentials=b"encrypted",
                scopes="[]",
            )
        )
    access_service = SandboxAccessService(session_factory)
    grant = await access_service.issue("alice", "conversation-alice", "container-alice")
    handle = SandboxHandle(
        "container-alice", "alice", "conversation-alice", Path("/host/input"), Path("/app/input")
    )
    try:
        yield SimpleNamespace(
            access=access_service,
            grant=grant,
            handle=handle,
            session_factory=session_factory,
        )
    finally:
        await database_engine.dispose()


@pytest.mark.parametrize("invalidated", ["expired", "revoked", "inactive", "cross_user"])
async def test_database_grant_revalidated_after_initialization(real_grant, invalidated):
    live_handle = real_grant.handle
    async with endpoint(
        access_service=real_grant.access, resolver=lambda *args: live_handle
    ) as fixture:
        fixture.client.headers["Authorization"] = f"Bearer {real_grant.grant.raw_token}"
        assert (await rpc(fixture.client, "initialize", INITIALIZE)).status_code == 200
        if invalidated == "expired":
            async with real_grant.session_factory.begin() as database_session:
                await database_session.execute(
                    update(SandboxAccessToken).values(expires_at=utcnow())
                )
        elif invalidated == "revoked":
            await real_grant.access.revoke_conversation(real_grant.handle.conversation_id)
        elif invalidated == "inactive":
            live_handle = None
        else:
            live_handle = SandboxHandle(
                "container-alice", "bob", "conversation-alice", Path("/host/bob"), Path("/app/bob")
            )
        assert (await rpc(fixture.client, "tools/list")).status_code == 401
        assert (
            await call(fixture.client, "get_email", {"message_id": "message-one"})
        ).status_code == 401
        fixture.gmail.get_email.assert_not_awaited()


@pytest.mark.parametrize("http_method", ["GET", "DELETE", "POST"])
async def test_browser_cookie_without_bearer_does_not_authorize(real_grant, http_method):
    async with endpoint(
        access_service=real_grant.access, resolver=lambda *args: real_grant.handle
    ) as fixture:
        fixture.client.headers.pop("Authorization")
        fixture.client.cookies.set("assistant_agent_session", "browser-cookie")
        response = await fixture.client.request(http_method, "/mcp/gmail")
        assert response.status_code == 401
        assert response.json() == {"error": "Sandbox access denied"}


@pytest.mark.parametrize(
    "name,arguments,service_method",
    [
        ("get_email", {"message_id": "message-one"}, "get_email"),
        ("get_thread", {"thread_id": "thread-one"}, "get_thread"),
        ("download_emails", {"message_ids": ["message-one"]}, "download_emails"),
        (
            "download_attachment",
            {"message_id": "message-one", "attachment_id": "attachment-one"},
            "download_attachment",
        ),
    ],
)
async def test_tool_handlers_use_trusted_context(name, arguments, service_method):
    async with endpoint() as fixture:
        response = await call(fixture.client, name, arguments)
        assert response.status_code == 200
        result = response.json()["result"]
        assert not result.get("isError", False)
        assert result["structuredContent"]
        service = fixture.files if name.startswith("download_") else fixture.gmail
        invoked = getattr(service, service_method)
        assert invoked.await_args.args[0] == (
            fixture.context if name.startswith("download_") else "alice"
        )
        fixture.access.authorize.assert_awaited()
        if name.startswith("download_"):
            reauthorize = invoked.await_args.kwargs["reauthorize"]
            assert await reauthorize() == fixture.context


@pytest.mark.parametrize("extra", ["user_id", "output_path", "sandbox_id"])
async def test_identity_and_destination_overrides_rejected(extra):
    async with endpoint() as fixture:
        response = await call(
            fixture.client, "get_email", {"message_id": "message-one", extra: "other"}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["error"]["code"] == -32602
        fixture.gmail.get_email.assert_not_awaited()


@pytest.mark.parametrize(
    "parameters",
    [
        {"name": "send_email", "arguments": {}},
        {"name": ["get_email"], "arguments": {}},
        {"name": "get_email", "arguments": ["message-one"]},
        {"name": "get_email", "arguments": None},
    ],
)
async def test_unknown_tool_and_malformed_arguments_safe_protocol_errors(parameters):
    async with endpoint() as fixture:
        response = await rpc(fixture.client, "tools/call", parameters)
        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32602
        fixture.gmail.get_email.assert_not_awaited()
        fixture.access.authorize.assert_not_awaited()


async def test_raw_exact_bytes_and_oversize_guidance():
    async with endpoint(MCPLimits(response_bytes=500)) as fixture:
        fixture.gmail.get_email.return_value = BinaryContent(
            "message-one", b"Subject: Hi\r\n\r\nBody", thread_id="thread-one"
        )
        response = await call(
            fixture.client, "get_email", {"message_id": "message-one", "format": "raw"}
        )
        result = response.json()["result"]["structuredContent"]
        encoded = result.get("raw", result.get("data"))
        assert (
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            == b"Subject: Hi\r\n\r\nBody"
        )
        fixture.gmail.get_email.return_value = BinaryContent("message-one", b"x" * 1000)
        response = await call(
            fixture.client, "get_email", {"message_id": "message-one", "format": "raw"}
        )
        result = response.json()["result"]
        assert result["isError"]
        assert "download_emails" in str(result)


@pytest.mark.parametrize("headers", [{"Host": "evil.example"}, {"Origin": "https://evil.example"}])
async def test_host_and_origin_validation(headers):
    async with endpoint() as fixture:
        response = await rpc(fixture.client, "initialize", INITIALIZE, headers=headers)
        assert response.status_code in {400, 403, 421}


async def test_explicit_base_url_configures_transport_without_global_settings(monkeypatch):
    import assistant_agent.config as config

    def unexpected_settings():
        pytest.fail("MCP transport read global settings despite explicit base_url")

    monkeypatch.setattr(config, "get_settings", unexpected_settings)
    async with endpoint(base_url="https://assistant.example:8443") as fixture:
        response = await rpc(
            fixture.client, "initialize", INITIALIZE,
            headers={"Origin": "https://assistant.example:8443"},
        )
        assert response.status_code == 200
        rejected = await rpc(
            fixture.client, "tools/list", headers={"Origin": "https://other.example"},
        )
        assert rejected.status_code in {400, 403, 421}


async def test_body_and_request_rate_limits():
    async with endpoint(MCPLimits(request_bytes=128)) as fixture:
        response = await call(fixture.client, "search_emails", {"query": "x" * 200})
        assert response.status_code == 413
        fixture.gmail.search_emails.assert_not_awaited()
    async with endpoint(MCPLimits(requests_per_minute=1)) as fixture:
        assert (await rpc(fixture.client, "tools/list")).status_code == 200
        assert (await rpc(fixture.client, "tools/list")).status_code == 429


async def test_concurrent_calls_reject_excess_and_release_capacity():
    async with endpoint(MCPLimits(conversation_calls=1)) as fixture:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_email(*args, **kwargs):
            started.set()
            await release.wait()
            return EmailContent("message-one", "thread-one")

        fixture.gmail.get_email.side_effect = slow_email
        first_call = asyncio.create_task(
            call(fixture.client, "get_email", {"message_id": "message-one"})
        )
        await started.wait()
        excess = await call(fixture.client, "get_email", {"message_id": "message-two"})
        assert excess.json()["result"]["isError"]
        release.set()
        assert not (await first_call).json()["result"].get("isError", False)
        assert (
            not (await call(fixture.client, "get_email", {"message_id": "message-three"}))
            .json()["result"]
            .get("isError", False)
        )


async def test_cancelled_http_request_drains_operation_and_releases_capacity():
    async with endpoint(MCPLimits(conversation_calls=1)) as fixture:
        started = asyncio.Event()
        drained = asyncio.Event()

        async def slow_email(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        fixture.gmail.get_email.side_effect = slow_email
        request_task = asyncio.create_task(
            call(fixture.client, "get_email", {"message_id": "message-one"})
        )
        await started.wait()
        request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)
        await asyncio.wait_for(drained.wait(), timeout=2)
        assert fixture.mcp.active_calls == 0
        assert fixture.mcp.conversation_calls == {}
        fixture.gmail.get_email.side_effect = None
        assert (
            not (await call(fixture.client, "get_email", {"message_id": "message-one"}))
            .json()["result"]
            .get("isError", False)
        )


async def test_small_page_size_and_long_cursor_pass_through():
    async with endpoint() as fixture:
        cursor = "opaque-cursor-" + "x" * 5000
        response = await call(
            fixture.client,
            "search_emails",
            {
                "query": "label:inbox",
                "page_size": 3,
                "cursor": cursor,
            },
        )
        assert not response.json()["result"].get("isError", False)
        fixture.gmail.search_emails.assert_awaited_once_with("alice", "label:inbox", 3, cursor)


async def test_global_concurrency_limit_across_conversations():
    async with endpoint(MCPLimits(app_calls=1)) as fixture:
        bob_context = AuthorizedSandboxContext(
            "bob", "conversation-bob", "container-bob", Path("/host/bob"), Path("/app/bob")
        )

        async def authenticate(token, *args):
            return bob_context if token == "bob-grant" else fixture.context

        async def authorize(token, *args):
            return await authenticate(token)

        fixture.access.authenticate.side_effect = authenticate
        fixture.access.authorize.side_effect = authorize
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_email(*args, **kwargs):
            started.set()
            await release.wait()
            return EmailContent("message-one", "thread-one")

        fixture.gmail.get_email.side_effect = slow_email
        first_call = asyncio.create_task(call(fixture.client, "get_email", {"message_id": "one"}))
        await started.wait()
        denied = await rpc(
            fixture.client,
            "tools/call",
            {
                "name": "get_email",
                "arguments": {"message_id": "two"},
            },
            headers={"Authorization": "Bearer bob-grant"},
        )
        assert denied.json()["result"]["isError"]
        assert fixture.mcp.conversation_calls == {"conversation-alice": 1}
        release.set()
        await first_call
        allowed = await rpc(
            fixture.client,
            "tools/call",
            {
                "name": "get_email",
                "arguments": {"message_id": "three"},
            },
            headers={"Authorization": "Bearer bob-grant"},
        )
        assert not allowed.json()["result"].get("isError", False)


async def test_http_downloads_publish_real_files_without_id_traversal(tmp_path):
    input_mount = tmp_path.resolve() / "input"
    input_mount.mkdir()
    async with endpoint() as fixture:
        context = AuthorizedSandboxContext(
            "alice",
            "conversation-alice",
            "container-alice",
            input_mount,
            input_mount,
        )
        fixture.access.authenticate.return_value = context
        fixture.access.authorize.return_value = context

        async def email(user_id, message_id, format):
            assert user_id == "alice"
            if message_id == "missing":
                raise GmailError("not_found")
            if format == "raw":
                return BinaryContent(message_id, b"Subject: exact\r\n\r\nMIME", thread_id="thread")
            return EmailContent(message_id, "thread", body="fresh body", truncated=True)

        fixture.gmail.get_email.side_effect = email
        fixture.gmail.get_attachment = AsyncMock(
            return_value=BinaryContent(
                "../../outside",
                b"\x00attachment\xff",
                attachment_id="../../attachment",
            )
        )
        file_service = SessionFilesService(fixture.gmail)
        fixture.mcp.session_files_service = file_service
        try:
            for tool, arguments in [
                ("search_emails", {"query": "from:friend"}),
                ("get_email", {"message_id": "message-one"}),
                ("get_thread", {"thread_id": "thread-one"}),
            ]:
                result = (await call(fixture.client, tool, arguments)).json()["result"]
                assert not result.get("isError", False)
            assert list(input_mount.iterdir()) == []

            first = (
                await call(
                    fixture.client,
                    "download_emails",
                    {
                        "message_ids": ["../../outside", "missing", "../../outside"],
                        "format": "text",
                    },
                )
            ).json()["result"]["structuredContent"]
            assert [item["success"] for item in first["items"]] == [True, False, True]
            assert first["items"][1]["error_code"] == "not_found"
            assert first["items"][0]["warnings"] == ["truncated"]
            generated_paths = []
            for item in (first["items"][0], first["items"][2]):
                sandbox_path = Path(item["path"])
                assert sandbox_path.parts[1] == "input"
                published_path = input_mount.joinpath(*sandbox_path.parts[2:])
                assert published_path.resolve().is_relative_to(input_mount)
                assert "WARNING: truncated" in published_path.read_text()
                assert published_path.stat().st_mode & 0o777 == 0o644
                generated_paths.append(published_path)
            assert generated_paths[0] != generated_paths[1]

            second = (
                await call(
                    fixture.client,
                    "download_emails",
                    {
                        "message_ids": ["../../outside"],
                        "format": "eml",
                    },
                )
            ).json()["result"]["structuredContent"]
            eml_path = input_mount.joinpath(*Path(second["items"][0]["path"]).parts[2:])
            assert eml_path.read_bytes() == b"Subject: exact\r\n\r\nMIME"
            assert eml_path.parent != generated_paths[0].parent
            assert all(path.exists() for path in generated_paths)

            attachment = (
                await call(
                    fixture.client,
                    "download_attachment",
                    {
                        "message_id": "../../outside",
                        "attachment_id": "../../attachment",
                    },
                )
            ).json()["result"]["structuredContent"]
            attachment_path = input_mount.joinpath(*Path(attachment["path"]).parts[2:])
            assert attachment_path.suffix == ".bin"
            assert attachment_path.read_bytes() == b"\x00attachment\xff"
            assert attachment["mime_type"] == "application/octet-stream"
            fixture.gmail.get_attachment.assert_awaited_once_with(
                "alice",
                "../../outside",
                "../../attachment",
            )
            assert not list(input_mount.glob(".gmail-stage-*"))
            assert not (tmp_path / "outside").exists()
        finally:
            await file_service.aclose()
