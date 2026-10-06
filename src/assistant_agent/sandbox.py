"""Dedicated, disposable Docker containers and loop-owned exec connections."""

from __future__ import annotations

import asyncio
import os
import io
import json
import tarfile
import shlex
import re
import shutil
from pathlib import Path
from collections.abc import Mapping

from .config import SandboxSettings, get_sandbox_settings
from dataclasses import dataclass
from enum import Enum

import aiodocker
from aiohttp import ClientTimeout
from aiodocker.execs import Exec

OWNER_LABEL = "assistant-agent.owner"
DEPLOYMENT_LABEL = "assistant-agent.deployment"
CONVERSATION_LABEL = "assistant-agent.conversation"
ALLOCATION_TIMEOUT_SECONDS = 30
# The writable, agent-owned workspace lives in the disposable container layer.
WORKSPACE = "/workspace"
REQUEST_TIMEOUT_SECONDS = 120


class SandboxError(RuntimeError):
    """Raised when the sandbox is unreachable or not running."""


class DockerClientRole(Enum):
    # Short commands for health, readiness, process termination, and orphan cleanup.
    CONTROL = "control"
    # Long-lived conversation exec streams with stdin kept open across turns.
    CHAT = "chat"


@dataclass(frozen=True)
class ExecResult:
    """The outcome of one command run inside the sandbox."""

    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class SandboxHandle:
    container_id: str
    user_id: str
    conversation_id: str
    host_input_path: Path
    app_input_path: Path


class Sandbox:
    """Loop-owned Docker connections, separate for control and chat attachments."""

    def __init__(
        self, settings: SandboxSettings | None = None, max_sessions: int | None = None
    ) -> None:
        self.settings = settings or get_sandbox_settings()
        self.max_sessions = (
            int(os.getenv("CHAT_MAX_SESSIONS", "4")) if max_sessions is None else max_sessions
        )
        if self.max_sessions < 1:
            raise ValueError("Sandbox capacity must be positive.")
        self._lock = asyncio.Lock()
        self._assignments: dict[str, SandboxHandle] = {}
        self._cleanup: dict[str, SandboxHandle] = {}
        self._tasks: set[asyncio.Task] = set()
        self._reconciled = False
        self._clients: dict[DockerClientRole, aiodocker.Docker] = {}
        self.closed = False
        self._closing = False

    def _client(self, role: DockerClientRole) -> aiodocker.Docker:
        if self.closed:
            raise SandboxError("Sandbox service is closed.")
        if role not in self._clients:
            try:
                self._clients[role] = aiodocker.Docker(
                    timeout=ClientTimeout(total=REQUEST_TIMEOUT_SECONDS + 15, connect=10)
                )
            except Exception as exc:
                raise SandboxError(f"Cannot reach the Docker daemon: {exc}") from exc
        return self._clients[role]

    def _track(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # Retrieve errors even when the requesting HTTP task is cancelled.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return task

    @property
    def capacity_used(self) -> int:
        return len(self._assignments)

    def _name(self, conversation_id: str) -> str:
        return f"assistant-agent-{self.settings.deployment_id}-{conversation_id}"

    def _session_path(self, conversation_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", conversation_id):
            raise SandboxError("Invalid generated conversation ID.")
        root = self.settings.app_input_root
        # Refuse symlinks at every existing level, including the configured root.
        for path in (root, *root.parents):
            if path.is_symlink():
                raise SandboxError("Input root cannot contain symlinks.")
        session = root / conversation_id
        if session.is_symlink():
            raise SandboxError("Input paths cannot be symlinks.")
        return session

    def _prepare_input(self, conversation_id: str) -> Path:
        session = self._session_path(conversation_id)
        root = self.settings.app_input_root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        session.mkdir(mode=0o755, exist_ok=False)
        session.chmod(0o755)
        return session

    def _remove_input(self, conversation_id: str) -> None:
        session = self._session_path(conversation_id)
        if session.exists():
            # No symlink anywhere in a generated directory is accepted for cleanup.
            if any(path.is_symlink() for path in session.rglob("*")):
                raise SandboxError("Refusing symlink in generated input directory.")
            shutil.rmtree(session)

    def _container_config(self, handle: SandboxHandle) -> dict:
        return {
            "Image": self.settings.image,
            "Cmd": ["sleep", "infinity"],
            "User": "agent",
            "WorkingDir": WORKSPACE,
            "Labels": {
                OWNER_LABEL: "assistant-agent",
                DEPLOYMENT_LABEL: self.settings.deployment_id,
                CONVERSATION_LABEL: handle.conversation_id,
            },
            "HostConfig": {
                "Binds": [f"{handle.host_input_path}:/input:ro"],
                "Init": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "PidsLimit": 2048,
                "Memory": 2 * 1024**3,
                "NanoCpus": 1_000_000_000,
                "RestartPolicy": {"Name": "no"},
                "NetworkMode": self.settings.network,
                "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}},
            },
            "NetworkingConfig": {"EndpointsConfig": {self.settings.network: {}}},
        }

    async def _dependencies(self) -> None:
        client = self._client(DockerClientRole.CONTROL)
        await client.version()
        await client.images.inspect(self.settings.image)
        await client.networks.get(self.settings.network)

    async def readiness(self, container_id: str) -> None:
        result = await self.run("true", name=container_id, timeout=10)
        if not result.ok:
            raise SandboxError("Sandbox readiness command failed.")

    async def allocate(self, user_id: str, conversation_id: str) -> SandboxHandle:
        return await asyncio.shield(self._track(self._allocate_with_timeout(user_id, conversation_id)))

    async def _allocate_with_timeout(self, user_id: str, conversation_id: str) -> SandboxHandle:
        async with asyncio.timeout(ALLOCATION_TIMEOUT_SECONDS):
            return await self._allocate(user_id, conversation_id)

    async def _allocate(self, user_id: str, conversation_id: str) -> SandboxHandle:
        async with self._lock:
            if self.closed or self._closing:
                raise SandboxError("Sandbox service is closing.")
            if not self._reconciled:
                await self._reconcile()
            await self._retry_cleanup()
            if self._cleanup:
                raise SandboxError("Sandbox cleanup is incomplete; try again shortly.")
            if self.capacity_used >= self.max_sessions:
                raise SandboxError("All sandbox slots are in use.")
            if conversation_id in self._assignments:
                raise SandboxError("Conversation already has a sandbox assignment.")
            name = self._name(conversation_id)
            handle = SandboxHandle(
                name, user_id, conversation_id,
                self.settings.host_input_root / conversation_id,
                self._session_path(conversation_id),
            )
            # Reserve before any create call, including an ambiguous daemon response.
            self._assignments[conversation_id] = handle
            try:
                async with asyncio.timeout(ALLOCATION_TIMEOUT_SECONDS):
                    await self._dependencies()
                    self._prepare_input(conversation_id)
                    container = await self._client(DockerClientRole.CONTROL).containers.create(
                        self._container_config(handle), name=name
                    )
                    handle = SandboxHandle(
                        container.id, user_id, conversation_id,
                        handle.host_input_path, handle.app_input_path,
                    )
                    self._assignments[conversation_id] = handle
                    await container.start()
                    await self.readiness(handle.container_id)
                return handle
            except BaseException as exc:
                self._cleanup[conversation_id] = handle
                try:
                    await self._destroy(handle)
                except Exception:
                    pass
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise SandboxError("Sandbox allocation failed.") from exc

    async def destroy(self, handle: SandboxHandle) -> None:
        await asyncio.shield(self._track(self._destroy_locked(handle)))

    async def _destroy_locked(self, handle: SandboxHandle) -> None:
        async with self._lock:
            await self._destroy(handle)

    async def _destroy(self, handle: SandboxHandle) -> None:
        self._cleanup[handle.conversation_id] = handle
        try:
            client = self._client(DockerClientRole.CONTROL)
            try:
                container = await client.containers.get(handle.container_id)
                labels = container["Config"]["Labels"] or {}
                if (labels.get(OWNER_LABEL) != "assistant-agent"
                        or labels.get(DEPLOYMENT_LABEL) != self.settings.deployment_id
                        or labels.get(CONVERSATION_LABEL) != handle.conversation_id):
                    raise SandboxError("Refusing to destroy a container outside this assignment.")
                await container.delete(force=True)
            except aiodocker.DockerError as exc:
                if exc.status != 404:
                    raise
            self._remove_input(handle.conversation_id)
        except Exception as exc:
            raise SandboxError("Sandbox destruction incomplete; retained for retry.") from exc
        self._cleanup.pop(handle.conversation_id, None)
        self._assignments.pop(handle.conversation_id, None)

    async def retry_cleanup(self) -> None:
        await asyncio.shield(self._track(self._retry_locked()))

    async def _retry_locked(self) -> None:
        async with self._lock:
            if not self._reconciled:
                await self._reconcile()
            await self._retry_cleanup()

    async def _retry_cleanup(self) -> None:
        for handle in list(self._cleanup.values()):
            try:
                await self._destroy(handle)
            except SandboxError:
                continue

    async def reconcile(self) -> None:
        await asyncio.shield(self._track(self._reconcile_locked()))

    async def _reconcile_locked(self) -> None:
        async with self._lock:
            await self._reconcile()

    async def _reconcile(self) -> None:
        self._reconciled = False
        try:
            client = self._client(DockerClientRole.CONTROL)
            containers = await client.containers.list(all=True, filters={"label": [
                f"{OWNER_LABEL}=assistant-agent",
            ]})
            protected: set[str] = set()
            for container in containers:
                # Check labels ourselves as defense against an overly broad daemon response.
                labels = container["Labels"]
                if (labels.get(OWNER_LABEL) == "assistant-agent"
                        and labels.get(DEPLOYMENT_LABEL) != self.settings.deployment_id):
                    protected.add(labels.get(CONVERSATION_LABEL, ""))
                if (labels.get(OWNER_LABEL) != "assistant-agent"
                        or labels.get(DEPLOYMENT_LABEL) != self.settings.deployment_id):
                    continue
                conversation_id = labels.get(CONVERSATION_LABEL, "")
                self._session_path(conversation_id)
                handle = SandboxHandle(
                    container.id, "", conversation_id,
                    self.settings.host_input_root / conversation_id,
                    self.settings.app_input_root / conversation_id,
                )
                self._assignments[conversation_id] = handle
                await self._destroy(handle)
            await self._retry_cleanup()
            if self._cleanup:
                raise SandboxError("Sandbox cleanup remains incomplete.")
            root = self.settings.app_input_root
            self._session_path("0" * 32)
            if root.exists():
                for path in root.iterdir():
                    if re.fullmatch(r"[0-9a-f]{32}", path.name) and path.name not in protected:
                        self._remove_input(path.name)
            self._reconciled = True
        except Exception as exc:
            raise SandboxError("Docker reconciliation incomplete; allocation is blocked.") from exc

    async def health(self, container_id: str | None = None) -> dict:
        try:
            await self._dependencies()
            if container_id:
                result = await self.run("claude --version", name=container_id)
                return {"container": container_id, "ok": result.ok,
                        "claude_version": result.output.strip()}
            return {"ok": True, "image": self.settings.image, "network": self.settings.network}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def provision_mcp(self, handle: SandboxHandle, gmail_enabled: bool) -> None:
        """Publish root-owned, secret-free MCP configuration through Docker archive."""
        config = {"mcpServers": {}}
        if gmail_enabled:
            config["mcpServers"]["gmail"] = {
                "type": "http",
                "url": "http://app:8000/mcp/gmail",
                "headers": {"Authorization": "Bearer ${ASSISTANT_MCP_TOKEN}"},
            }
        config_bytes = json.dumps(config).encode("utf-8")
        # Write json config to a tar archive and upload it into the container.
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
            # Create a directory named "assistant"
            directory = tarfile.TarInfo("assistant")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            directory.uid = directory.gid = 0
            archive.addfile(directory)
            # Create a file named "mcp.json"
            config_file = tarfile.TarInfo("assistant/mcp.json")
            config_file.mode = 0o644
            config_file.uid = config_file.gid = 0
            config_file.size = len(config_bytes)
            archive.addfile(config_file, io.BytesIO(config_bytes))
        try:
            container = await self._client(DockerClientRole.CONTROL).containers.get(
                handle.container_id
            )
            await container.put_archive("/run", archive_buffer.getvalue())
        except Exception as exc:
            raise SandboxError("MCP configuration provisioning failed.") from exc

    async def exec(
        self,
        command: str,
        *,
        stdin: bool = False,
        workdir: str = WORKSPACE,
        name: str,
        environment: Mapping[str, str] | None = None,
    ) -> Exec:
        target = name
        if not target:
            raise SandboxError("An explicit sandbox container ID is required.")
        role = DockerClientRole.CHAT if stdin else DockerClientRole.CONTROL
        try:
            container = await self._client(role).containers.get(target)
        except Exception as exc:
            raise SandboxError(f"Sandbox container {target!r} not found: {exc}") from exc
        status = container["State"]["Status"]
        if status != "running":
            raise SandboxError(f"Sandbox container {target!r} is {status}, not running.")
        try:
            return await container.exec(
                ["bash", "-lc", command],
                stdin=stdin,
                stdout=True,
                stderr=True,
                tty=False,
                user="agent",
                workdir=workdir,
                environment=environment,
            )
        except Exception as exc:
            raise SandboxError(f"Sandbox exec failed: {exc}") from exc

    async def run(
        self,
        command: str,
        *,
        workdir: str = WORKSPACE,
        name: str,
        environment: Mapping[str, str] | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS + 15,
    ) -> ExecResult:
        """Use a login shell so the image's CLI PATH is available."""
        try:
            async with asyncio.timeout(timeout):
                execution = await self.exec(command, workdir=workdir, name=name, environment=environment)
                chunks = []
                stream = execution.start()
                try:
                    await stream.__aenter__()
                    while (message := await stream.read_out()) is not None:
                        if message.stream not in (1, 2):
                            raise SandboxError("Invalid Docker stream ID")
                        chunks.append(message.data)
                finally:
                    await stream.close()
                status = await execution.inspect()
                code = status.get("ExitCode")
                if code is None or status.get("Running"):
                    raise SandboxError("Sandbox exec has no exit status.")
                return ExecResult(code, b"".join(chunks).decode("utf-8", errors="replace"))
        except Exception as exc:
            raise SandboxError(f"Sandbox exec failed: {exc}") from exc

    async def close(self) -> None:
        self._closing = True
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        for handle in list(self._assignments.values()):
            try:
                await self.destroy(handle)
            except SandboxError:
                pass
        self.closed = True
        clients, self._clients = list(self._clients.values()), {}
        if clients:
            results = await asyncio.gather(
                *(client.close() for client in clients), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result

    async def __aenter__(self) -> Sandbox:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


async def run(command: str, **kwargs) -> ExecResult:
    """Execute with a temporary service; chat uses its lifespan-owned service."""
    async with Sandbox() as service:
        return await service.run(command, **kwargs)


async def run_argv(argv: list[str], **kwargs) -> ExecResult:
    """Quote arguments so the login shell cannot reinterpret them."""
    return await run(shlex.join(argv), **kwargs)


async def ask(
    message: str, *, name: str, environment: Mapping[str, str] | None = None
) -> ExecResult:
    """Send one message to Claude and collect its plain-text response.

    Legacy explicit-container commands expose no tools and save no transcript.
    """
    if environment is None and (key := os.getenv("ANTHROPIC_API_KEY")):
        environment = {"ANTHROPIC_API_KEY": key}
    return await run_argv(
        [
            "timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{REQUEST_TIMEOUT_SECONDS}s",
            "claude",
            "-p",
            "--output-format",
            "text",
            "--no-session-persistence",
            "--restricted",
            "--tools",
            "",
            "--disallowedTools",
            "mcp__*",
            "--",
            message,
        ],
        name=name,
        environment=environment,
    )


async def health(name: str | None = None) -> dict:
    """Check dependencies without allocating a container."""
    async with Sandbox() as service:
        return await service.health(name)
