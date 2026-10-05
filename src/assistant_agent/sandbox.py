"""Run commands inside the agent sandbox container.

`docker exec` is a call to the Docker daemon socket, not a connection to the container,
so this works because deploy/compose.prod.yaml bind-mounts /var/run/docker.sock into the
app container and adds the host's docker group to its supplementary groups. Being on a
shared Compose network is neither necessary nor sufficient for it.

That socket is root-equivalent control of the host, so keep this module narrow: it runs a
command in one named, already-running container and returns what happened. It does not
create, configure, or destroy containers -- the sandbox's lifecycle belongs to Compose.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass
from enum import Enum

import aiodocker
from aiohttp import ClientTimeout
from aiodocker.execs import Exec

DEFAULT_CONTAINER = "assistant-agent-sandbox-1"
# The sandbox's WORKDIR, where the host's per-sandbox directory is bind-mounted.
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


def container_name() -> str:
    """The container to exec into, overridable per deployment."""
    return os.getenv("SANDBOX_CONTAINER", "").strip() or DEFAULT_CONTAINER


class Sandbox:
    """Loop-owned Docker connections, separate for control and chat attachments."""

    def __init__(self) -> None:
        self._clients: dict[DockerClientRole, aiodocker.Docker] = {}
        self.closed = False

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

    async def exec(
        self,
        command: str,
        *,
        stdin: bool = False,
        workdir: str = WORKSPACE,
        name: str | None = None,
    ) -> Exec:
        target = name or container_name()
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
            )
        except Exception as exc:
            raise SandboxError(f"Sandbox exec failed: {exc}") from exc

    async def run(
        self,
        command: str,
        *,
        workdir: str = WORKSPACE,
        name: str | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS + 15,
    ) -> ExecResult:
        """Use a login shell so the image's CLI PATH is available."""
        try:
            async with asyncio.timeout(timeout):
                execution = await self.exec(command, workdir=workdir, name=name)
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


async def ask(message: str) -> ExecResult:
    """Send one message to Claude and collect its plain-text response.

    The shared sandbox has no per-user workspace isolation yet, so this mode gives
    Claude no tools and saves no transcript in the container.
    """
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
        ]
    )


async def health() -> dict:
    """A small status dict for diagnostics: is the sandbox up and is the agent CLI there?"""
    target = container_name()
    try:
        result = await run_argv(["claude", "--version"])
    except SandboxError as exc:
        return {"container": target, "ok": False, "error": str(exc)}
    return {
        "container": target,
        "ok": result.ok,
        "claude_version": result.output.strip() if result.ok else None,
        "error": None if result.ok else result.output.strip(),
    }
