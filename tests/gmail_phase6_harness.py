"""Disposable full-app integration environment; only Google boundaries are synthetic."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import socket
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import aiodocker
import httpx
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from httplib2 import Response
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.responses import JSONResponse

from assistant_agent import config, web
from assistant_agent.gmail_service import GmailService
from assistant_agent.sandbox_access import IssuedSandboxAccess


@dataclass
class BrowserUser:
    """An independently authenticated browser session."""

    client: httpx.AsyncClient
    user_id: str
    csrf: str
    name: str


class IntegrationHarness:
    """Own the real HTTP app, migrated database, and disposable Docker resources."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.run_id = uuid.uuid4().hex
        self.network_name = f"gmail-phase6-{self.run_id}"
        self.backend_network_name = f"gmail-phase6-db-{self.run_id}"
        self.docker = aiodocker.Docker()
        self.directory = tempfile.TemporaryDirectory(prefix="gmail-phase6-", dir="/private/tmp")
        self.root = Path(self.directory.name)
        self.users: dict[str, BrowserUser] = {}
        self.grants: dict[str, IssuedSandboxAccess] = {}
        self.report: dict[str, Any] = {}
        self.calls: list[tuple[str, str]] = []
        self.traces: dict[str, list[dict]] = {}
        self.provisions: list[tuple[str, bool]] = []
        self.oauth_failing: set[str] = set()
        self.mcp_available = True
        self.server_task: asyncio.Task | None = None
        self.resources: list[Any] = []
        self.postgres_id = self.relay_id = ""
        self.image_id = ""
        self.app: Any = None
        self.http_socket: socket.socket | None = None
        self.port = 0
        self.base_url = ""

    @property
    def service(self):
        """Return the lifespan-owned real sandbox service."""
        return self.app.state.sandbox_service

    def google_request(
        self, credentials: Credentials, method: str, params: dict, timeout: float,
    ) -> dict:
        """Return Gmail wire objects, keeping real service parsing and authorization."""
        name = credentials.token.removeprefix("synthetic-")
        self.calls.append((name, method))
        if name in self.oauth_failing:
            raise HttpError(Response({"status": "401"}), b'{"error":{"message":"expired"}}')
        message_id = params.get("id", "")
        if method == "list":
            page = 2 if params.get("pageToken") else 1
            result = {"messages": [{"id": f"{name}-{page}", "threadId": f"{name}-thread"}],
                      "resultSizeEstimate": 2}
            if params.get("q") == "partial-preview":
                result["messages"].append({"id": "missing-message", "threadId": "missing"})
            if page == 1:
                result["nextPageToken"] = "second-page"
            return result
        if method == "attachment":
            if params.get("messageId") != f"{name}-1" or message_id != "fixture-attachment":
                raise HttpError(Response({"status": "404"}), b'{"error":{"message":"missing"}}')
            return {"data": base64.urlsafe_b64encode(f"ATTACHMENT_{name}".encode()).decode(),
                    "size": len(f"ATTACHMENT_{name}")}
        messages = [self.message(name, index) for index in (1, 2)]
        if method == "thread" and message_id == f"{name}-thread":
            return {"id": message_id, "messages": messages}
        for message in messages:
            if message["id"] == message_id:
                if params.get("format") == "raw":
                    raw = f"Subject: Fixture {name}\r\n\r\nSYNTHETIC_GMAIL_{message_id}\r\n".encode()
                    return {"id": message_id, "threadId": f"{name}-thread",
                            "raw": base64.urlsafe_b64encode(raw).decode()}
                return copy.deepcopy(message)
        raise HttpError(Response({"status": "404"}), b'{"error":{"message":"missing"}}')

    @staticmethod
    def message(name: str, index: int) -> dict:
        """Build a small multipart email with a downloadable attachment."""
        body = f"SYNTHETIC_GMAIL_{name}_{index}"
        return {"id": f"{name}-{index}", "threadId": f"{name}-thread", "snippet": body,
                "internalDate": "1700000000000", "payload": {
                    "mimeType": "multipart/mixed", "headers": [
                        {"name": "Subject", "value": f"Fixture {name} {index}"},
                        {"name": "From", "value": f"{name}@example.invalid"}],
                    "parts": [{"partId": "0", "mimeType": "text/plain", "body": {
                        "data": base64.urlsafe_b64encode(body.encode()).decode(), "size": len(body)}},
                        {"partId": "1", "mimeType": "text/plain", "filename": "fixture.txt",
                         "body": {"attachmentId": "fixture-attachment", "size": 12}}]}}

    async def prepare(self) -> None:
        """Create pinned disposable infrastructure and migrate its empty database."""
        self.image_id = (await self.docker.images.inspect(config.get_sandbox_settings().image))["Id"]
        postgres_image = (await self.docker.images.inspect("postgres:17-alpine"))["Id"]
        for name in (self.network_name, self.backend_network_name):
            self.resources.append(await self.docker.networks.create({"Name": name}))
        postgres = await self.docker.containers.create({
            "Image": postgres_image, "Env": ["POSTGRES_USER=integration", "POSTGRES_PASSWORD=synthetic",
                                            "POSTGRES_DB=integration"],
            "HostConfig": {"NetworkMode": self.backend_network_name,
                           "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]}},
            "ExposedPorts": {"5432/tcp": {}},
        }, name=f"gmail-phase6-db-{self.run_id}")
        self.resources.append(postgres)
        self.postgres_id = postgres.id
        await postgres.start()
        postgres_info = await postgres.show()
        db_port = postgres_info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
        database_url = f"postgresql+psycopg://integration:synthetic@127.0.0.1:{db_port}/integration"
        import psycopg
        async with asyncio.timeout(30):
            while True:
                try:
                    connection = await psycopg.AsyncConnection.connect(
                        database_url.replace("postgresql+psycopg", "postgresql"), connect_timeout=1,
                    )
                    await connection.close()
                    break
                except psycopg.OperationalError:
                    await asyncio.sleep(0.1)
        environment = {
            "DATABASE_URL": database_url, "CREDENTIAL_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "GOOGLE_CLIENT_ID": "synthetic-client", "GOOGLE_CLIENT_SECRET": "synthetic-client-secret",
            "APP_ENV": "development", "BASE_URL": "http://localhost:8000",
            "SANDBOX_IMAGE": self.image_id, "SANDBOX_NETWORK": self.network_name,
            "SANDBOX_DEPLOYMENT_ID": f"test-{self.run_id}",
            "SANDBOX_HOST_INPUT_ROOT": str(self.root / "inputs"),
            "SANDBOX_APP_INPUT_ROOT": str(self.root / "inputs"), "CHAT_MAX_SESSIONS": "4",
        }
        for name, value in environment.items():
            self.monkeypatch.setenv(name, value)
        config.get_settings.cache_clear()
        await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
        self.monkeypatch.setattr(web, "GmailService", partial(GmailService, request=self.google_request))
        self.monkeypatch.setattr(web, "authorization_url", lambda nonce: (
            f"https://google.invalid/?state={nonce}", nonce, "synthetic-verifier"))
        def exchange(response_url: str, **kwargs) -> Credentials:
            from urllib.parse import parse_qs, urlsplit
            name = parse_qs(urlsplit(response_url).query)["code"][0]
            return Credentials(token=f"synthetic-{name}", refresh_token=f"refresh-{name}",
                               token_uri="https://google.invalid/token", client_id="synthetic-client",
                               client_secret="synthetic-client-secret", scopes=config.SCOPES,
                               expiry=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))
        self.monkeypatch.setattr(web, "exchange_code", exchange)
        self.monkeypatch.setattr(web, "account_identity", lambda creds, expected_nonce: (
            creds.token.removeprefix("synthetic-") + "@example.invalid", creds.token))
        self.monkeypatch.setattr(web, "revoke", lambda creds: True)
        from assistant_agent.chat.conversation import Conversation
        original_receive = Conversation.receive
        def capture_receive(conversation: Conversation, message: dict) -> None:
            self.traces.setdefault(conversation.user_id, []).append(message)
            original_receive(conversation, message)
        self.monkeypatch.setattr(Conversation, "receive", capture_receive)
        self.http_socket = socket.socket()
        self.http_socket.bind(("0.0.0.0", 0))
        self.http_socket.listen(128)
        self.port = self.http_socket.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        relay_script = (
            "const http=require('http');http.createServer((req,res)=>{"
            f"const upstream=http.request({{hostname:'host.docker.internal',port:{self.port},"
            "path:req.url,method:req.method,headers:{...req.headers,host:'app:8000'}},r=>{"
            "res.writeHead(r.statusCode,r.headers);r.pipe(res)});"
            "upstream.on('error',()=>{res.writeHead(502);res.end()});req.pipe(upstream)"
            "}).listen(8000,'0.0.0.0')")
        relay = await self.docker.containers.create({
            "Image": self.image_id, "User": "agent", "Cmd": ["node", "-e", relay_script],
            "HostConfig": {"NetworkMode": self.network_name},
            "NetworkingConfig": {"EndpointsConfig": {self.network_name: {"Aliases": ["app"]}}},
        }, name=f"gmail-phase6-relay-{self.run_id}")
        self.resources.append(relay)
        self.relay_id = relay.id
        await relay.start()
        await self.start()

    async def start(self) -> None:
        """Start a fresh production lifespan against the same database and Docker scope."""
        self.app = web.create_app()
        @self.app.middleware("http")
        async def inject_transport_fault(request, call_next):
            if request.url.path.startswith("/mcp/") and not self.mcp_available:
                return JSONResponse({"error": "synthetic_unavailable"}, status_code=503)
            return await call_next(request)
        self.server = uvicorn.Server(uvicorn.Config(
            self.app, log_level="critical", access_log=False, timeout_graceful_shutdown=5))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.http_socket.dup()]))
        async with asyncio.timeout(30):
            while not self.server.started:
                assert not self.server_task.done(), "Full application startup failed"
                await asyncio.sleep(0.05)
        original_issue = self.app.state.sandbox_access_service.issue
        async def capture_issue(user_id: str, conversation_id: str, container_id: str):
            grant = await original_issue(user_id, conversation_id, container_id)
            self.grants[user_id] = grant
            return grant
        self.app.state.sandbox_access_service.issue = capture_issue
        original_provision = self.service.provision_mcp
        async def capture_provision(handle, *, gmail_enabled: bool):
            self.provisions.append((handle.conversation_id, gmail_enabled))
            return await original_provision(handle, gmail_enabled=gmail_enabled)
        self.service.provision_mcp = capture_provision

    async def stop(self) -> None:
        """Run bounded server and full lifespan shutdown."""
        if self.server_task is not None:
            self.server.should_exit = True
            await asyncio.wait_for(self.server_task, 30)
            self.server_task = None

    async def connect(self, name: str) -> BrowserUser:
        """Authenticate a separate browser through the public OAuth routes."""
        client = httpx.AsyncClient(base_url=self.base_url, timeout=240)
        start = await client.get("/auth/google/start")
        from urllib.parse import parse_qs, urlsplit
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        callback = await client.get("/auth/google/callback", params={"state": state, "code": name})
        assert callback.status_code == 303
        session = await self.app.state.web_store.session(client.cookies.get(web.COOKIE))
        user = BrowserUser(client, session.user_id, session.csrf_token, name)
        self.users[name] = user
        return user

    async def submit_start(self, user: BrowserUser, prompt: str) -> dict:
        """Submit through HTTP, returning after startup and first prompt acceptance."""
        response = await user.client.post("/api/message", json={"message": prompt},
                                          headers={"x-csrf-token": user.csrf})
        assert response.status_code == 202, "Public message submission failed"
        return response.json()

    async def wait_turn(self, user: BrowserUser) -> dict:
        """Wait for the HTTP snapshot to report a successful completed model turn."""
        async with asyncio.timeout(180):
            while True:
                response = await user.client.get("/api/conversation")
                assert response.status_code == 200
                snapshot = response.json()
                assert not snapshot["failed"], "Real model turn failed"
                if not snapshot["active_turn"]:
                    return snapshot
                await asyncio.sleep(0.1)

    async def submit(self, user: BrowserUser, prompt: str) -> dict:
        """Submit and await one completed real model turn."""
        await self.submit_start(user, prompt)
        return await self.wait_turn(user)

    async def reset(self, user: BrowserUser) -> None:
        """Reset the browser's conversation through the public CSRF-protected route."""
        response = await user.client.post("/api/conversation/reset", headers={"x-csrf-token": user.csrf})
        assert response.status_code == 200

    @asynccontextmanager
    async def mcp(self, user: BrowserUser) -> AsyncIterator[ClientSession]:
        """Connect a direct adversarial MCP client to the real assigned grant."""
        async with httpx.AsyncClient(headers={
            "Authorization": f"Bearer {self.grants[user.user_id].raw_token}"}) as client:
            async with streamable_http_client(self.base_url + "/mcp/gmail", http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session

    async def close(self) -> None:
        """Clean every owned resource even when an earlier cleanup fails."""
        errors: list[Exception] = []
        operations = [self.stop]
        operations.extend(user.client.aclose for user in self.users.values())
        operations.extend(partial(resource.delete, force=True) for resource in reversed(self.resources)
                          if hasattr(resource, "exec"))
        operations.extend(resource.delete for resource in reversed(self.resources)
                          if not hasattr(resource, "exec"))
        operations.append(self.docker.close)
        for operation in operations:
            try:
                await operation()
            except Exception as error:
                errors.append(error)
        if self.http_socket is not None:
            self.http_socket.close()
        self.directory.cleanup()
        config.get_settings.cache_clear()
        self.grants.clear()
        self.traces.clear()
        if errors:
            raise ExceptionGroup("Integration cleanup failed", errors)


def tool_data(result) -> dict:
    """Decode structured tool results without writing email bodies to diagnostics."""
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)
