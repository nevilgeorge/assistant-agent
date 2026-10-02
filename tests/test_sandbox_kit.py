"""Guard the vendored agent-sandbox build context and the sandbox exec wrapper.

The kit is not installed anywhere -- deploy/deploy.sh builds it straight from the working
tree -- so these tests stand in for the manifest that test_agent_kit.py relies on.
"""

from __future__ import annotations

import stat

import pytest

from assistant_agent import sandbox
from assistant_agent.config import SANDBOX_KIT_DIR

# Exactly what `docker buildx build -f .../Dockerfile .../sandbox_kit` needs. The upstream
# .dockerignore reduces the context to entrypoint.sh, so nothing else may be required.
KIT_FILES = ("Dockerfile", ".dockerignore", "docker/entrypoint.sh", "UPSTREAM.md")


@pytest.mark.parametrize("relpath", KIT_FILES)
def test_kit_file_is_present(relpath: str) -> None:
    assert (SANDBOX_KIT_DIR / relpath).is_file(), f"{relpath} missing from the vendored context"


def test_syntax_directive_is_the_first_line() -> None:
    """A provenance banner above this line silently disables the BuildKit frontend.

    The Dockerfile needs BuildKit for `--mount=type=cache` and `COPY --chmod`, so losing
    the directive would turn a working build into a confusing failure.
    """
    first = (SANDBOX_KIT_DIR / "Dockerfile").read_text(encoding="utf-8").splitlines()[0]
    assert first == "# syntax=docker/dockerfile:1"


def test_dockerignore_reduces_the_context_to_the_entrypoint() -> None:
    body = (SANDBOX_KIT_DIR / ".dockerignore").read_text(encoding="utf-8").split()
    assert body == ["*", "!docker/entrypoint.sh"]


def test_entrypoint_is_executable_and_copied_verbatim() -> None:
    entrypoint = SANDBOX_KIT_DIR / "docker" / "entrypoint.sh"
    assert entrypoint.stat().st_mode & stat.S_IXUSR
    # `exec "$@"` is what hands control to the compose `command:`; without it the
    # container would run the entrypoint's setup and exit.
    assert entrypoint.read_text(encoding="utf-8").rstrip().endswith('exec "$@"')


def test_dockerfile_still_copies_only_the_entrypoint_from_the_context() -> None:
    """If upstream adds a context COPY, the vendored file list above is incomplete."""
    lines = (SANDBOX_KIT_DIR / "Dockerfile").read_text(encoding="utf-8").splitlines()
    context_copies = [
        line for line in lines if line.startswith("COPY ") and "--from=" not in line
    ]
    assert len(context_copies) == 1
    assert "docker/entrypoint.sh" in context_copies[0]


def test_container_name_defaults_and_honours_the_environment(monkeypatch) -> None:
    monkeypatch.delenv("SANDBOX_CONTAINER", raising=False)
    assert sandbox.container_name() == sandbox.DEFAULT_CONTAINER
    # compose.prod.yaml sets this, so the default is only a fallback.
    monkeypatch.setenv("SANDBOX_CONTAINER", "other-sandbox")
    assert sandbox.container_name() == "other-sandbox"
    # A blank value is the same as unset: compose writes empty strings for unset vars.
    monkeypatch.setenv("SANDBOX_CONTAINER", "   ")
    assert sandbox.container_name() == sandbox.DEFAULT_CONTAINER


def test_run_reports_an_unreachable_daemon_as_sandbox_error(monkeypatch) -> None:
    """Importing the module must not require Docker; calling it must fail clearly."""
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    with pytest.raises(sandbox.SandboxError):
        sandbox.run("true")


def test_exec_result_ok_tracks_the_exit_code() -> None:
    assert sandbox.ExecResult(0, "").ok
    assert not sandbox.ExecResult(1, "boom").ok


def test_run_argv_quotes_its_arguments(monkeypatch) -> None:
    """Shell metacharacters in an argv entry must not reach the login shell unquoted."""
    seen: dict[str, str] = {}

    def fake_run(command: str, **kwargs):
        seen["command"] = command
        return sandbox.ExecResult(0, "")

    monkeypatch.setattr(sandbox, "run", fake_run)
    sandbox.run_argv(["echo", "a b; rm -rf /"])
    assert seen["command"] == "echo 'a b; rm -rf /'"


def test_workspace_matches_the_image_workdir() -> None:
    """The bind mount in compose.prod.yaml targets this path."""
    assert sandbox.WORKSPACE == "/workspace"
    assert "WORKDIR /workspace" in (SANDBOX_KIT_DIR / "Dockerfile").read_text(encoding="utf-8")


def test_vendored_copies_match_upstream_when_the_sibling_repo_is_present() -> None:
    """The real drift check, skipped on machines without agent-sandbox checked out."""
    upstream = SANDBOX_KIT_DIR.parents[2] / ".." / "agent-sandbox"
    upstream = upstream.resolve()
    if not upstream.is_dir():
        pytest.skip(f"{upstream} is not checked out")
    for relpath in ("Dockerfile", ".dockerignore", "docker/entrypoint.sh"):
        expected = (upstream / relpath).read_bytes()
        actual = (SANDBOX_KIT_DIR / relpath).read_bytes()
        assert actual == expected, (
            f"{relpath} has drifted from upstream; see sandbox_kit/UPSTREAM.md to re-sync"
        )


def test_compose_wires_the_sandbox_to_the_vendored_image() -> None:
    """The three settings the plan depends on, read straight from the deployed file."""
    compose = (SANDBOX_KIT_DIR.parents[2] / "deploy" / "compose.prod.yaml").read_text(
        encoding="utf-8"
    )
    # The container the app execs into must be the one compose names.
    assert f"container_name: {sandbox.DEFAULT_CONTAINER}" in compose
    assert f"SANDBOX_CONTAINER: {sandbox.DEFAULT_CONTAINER}" in compose
    # Without --rm's absence and a blocking command, the container would exit at once.
    assert "exec sleep infinity" in compose
    # The socket, not the network, is what makes docker exec possible.
    assert "/var/run/docker.sock:/var/run/docker.sock" in compose
