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

import os
import shlex
from dataclasses import dataclass

# Imported lazily inside _client() so that importing this module -- which the web app does
# at startup -- never fails on a machine with no Docker socket.

DEFAULT_CONTAINER = "assistant-agent-sandbox-1"
# The sandbox's WORKDIR, where the host's per-sandbox directory is bind-mounted.
WORKSPACE = "/workspace"


class SandboxError(RuntimeError):
    """Raised when the sandbox is unreachable or not running."""


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


def _client():
    try:
        import docker
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise SandboxError("The docker package is not installed.") from exc
    try:
        return docker.from_env()
    except Exception as exc:
        raise SandboxError(f"Cannot reach the Docker daemon: {exc}") from exc


def run(
    command: str,
    *,
    workdir: str = WORKSPACE,
    name: str | None = None,
) -> ExecResult:
    """Run `command` in the sandbox through a login shell and collect its output.

    A login shell, not a bare exec: the image puts the agent CLIs on PATH through
    /etc/profile.d/10-npm-global.sh, which only a login shell sources. Without `-l`,
    `claude` is not found.
    """
    target = name or container_name()
    client = _client()
    try:
        container = client.containers.get(target)
    except Exception as exc:
        raise SandboxError(f"Sandbox container {target!r} not found: {exc}") from exc

    if container.status != "running":
        raise SandboxError(f"Sandbox container {target!r} is {container.status}, not running.")

    result = container.exec_run(
        ["bash", "-lc", command],
        workdir=workdir,
        # The image's own user. Running as root would break Claude Code, which refuses
        # --dangerously-skip-permissions as root, and would leave root-owned files in the
        # bind-mounted workspace.
        user="agent",
        demux=False,
    )
    output = result.output.decode("utf-8", errors="replace") if result.output else ""
    return ExecResult(exit_code=result.exit_code, output=output)


def run_argv(argv: list[str], **kwargs) -> ExecResult:
    """`run` for an argument list, quoted so the login shell cannot reinterpret it."""
    return run(shlex.join(argv), **kwargs)


def health() -> dict:
    """A small status dict for diagnostics: is the sandbox up and is the agent CLI there?"""
    target = container_name()
    try:
        result = run_argv(["claude", "--version"])
    except SandboxError as exc:
        return {"container": target, "ok": False, "error": str(exc)}
    return {
        "container": target,
        "ok": result.ok,
        "claude_version": result.output.strip() if result.ok else None,
        "error": None if result.ok else result.output.strip(),
    }
