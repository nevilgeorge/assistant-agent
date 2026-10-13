"""Security, pagination, and preview coverage for sandbox file inspection."""

import json
import os
from pathlib import Path
import shlex
from unittest.mock import AsyncMock

import pytest

from assistant_agent.file_inspector import InspectionError, PREVIEW_LIMIT, inspect_request
from assistant_agent.file_viewer import FileViewerError, FileViewerService
from assistant_agent.sandbox import ExecResult, SandboxError, SandboxHandle


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, str]:
    """Point all test roots at a temporary directory."""
    return {"input": str(tmp_path), "workspace": str(tmp_path), "daily-logs": str(tmp_path)}


def test_directory_order_hidden_files_and_pagination(tmp_path: Path, roots: dict) -> None:
    (tmp_path / "z-folder").mkdir()
    (tmp_path / ".hidden").write_text("hidden")
    for index in range(202):
        (tmp_path / f"file-{index:03}").write_text("hello")
    first = inspect_request("list", "input", roots=roots)
    assert len(first["entries"]) == 200
    assert first["entries"][0]["name"] == "z-folder"
    assert first["entries"][1]["name"] == ".hidden"
    assert first["next_offset"] == 200
    last = inspect_request("list", "input", offset=200, roots=roots)
    assert len(last["entries"]) == 4
    assert last["next_offset"] is None
    assert set(entry["name"] for entry in first["entries"]).isdisjoint(
        entry["name"] for entry in last["entries"]
    )


@pytest.mark.parametrize("path", ["/etc/passwd", "../escape", "a/../escape", "a//b", "a/", ".", "a\x00"])
def test_reject_invalid_paths(path: str, roots: dict) -> None:
    with pytest.raises(InspectionError) as error:
        inspect_request("list", "input", path, roots=roots)
    assert error.value.status == 400


def test_links_and_special_files_are_unavailable(tmp_path: Path, roots: dict) -> None:
    (tmp_path / "file").write_text("safe")
    (tmp_path / "link").symlink_to(tmp_path / "file")
    (tmp_path / "directory-link").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(tmp_path / "fifo")
    entries = {entry["name"]: entry for entry in inspect_request("list", "input", roots=roots)["entries"]}
    for name in ["link", "directory-link", "fifo"]:
        assert entries[name]["kind"] == "unavailable"
        with pytest.raises(InspectionError) as error:
            inspect_request("preview", "input", name, roots=roots)
        assert error.value.status == 415
    with pytest.raises(InspectionError):
        inspect_request("preview", "input", "directory-link/file", roots=roots)
    with pytest.raises(InspectionError):
        inspect_request("list", "input", "directory-link", roots=roots)


def test_root_symlink_rejected(tmp_path: Path, roots: dict) -> None:
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(InspectionError):
        inspect_request("list", "input", roots={**roots, "input": str(tmp_path / "link")})


@pytest.mark.parametrize(
    ("name", "data", "format", "content"),
    [("note.md", b"# hello", "markdown", "# hello"), ("empty", b"", "text", ""),
     ("file.txt", b"hello", "text", "hello"), ("nul", b"ab\x00cd", "binary", None),
     ("bad-encoding", b"\xff", "binary", None)],
)
def test_preview_formats(
    tmp_path: Path, roots: dict, name: str, data: bytes, format: str, content: str | None,
) -> None:
    (tmp_path / name).write_bytes(data)
    result = inspect_request("preview", "workspace", name, roots=roots)
    assert result["format"] == format
    assert result["content"] == content
    assert result["truncated"] is False
    assert result["size"] == len(data)
    assert result["modified"].endswith("+00:00")


def test_preview_truncation_preserves_utf8_boundary(tmp_path: Path, roots: dict) -> None:
    prefix = b"a" * (PREVIEW_LIMIT - 1)
    (tmp_path / "large").write_bytes(prefix + "€".encode())
    result = inspect_request("preview", "input", "large", roots=roots)
    assert result["truncated"] is True
    assert result["format"] == "text"
    assert result["content"] == prefix.decode()
    (tmp_path / "exact").write_bytes(b"a" * PREVIEW_LIMIT)
    assert inspect_request("preview", "input", "exact", roots=roots)["truncated"] is False


def test_missing_file_and_nested_path(tmp_path: Path, roots: dict) -> None:
    with pytest.raises(InspectionError) as error:
        inspect_request("preview", "daily-logs", "missing", roots=roots)
    assert error.value.status == 404
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "note").write_text("content")
    result = inspect_request("list", "daily-logs", "nested", roots=roots)
    assert result["entries"][0]["path"] == "nested/note"
    assert inspect_request("preview", "daily-logs", "nested/note", roots=roots)["content"] == "content"


@pytest.fixture
def handle(tmp_path: Path) -> SandboxHandle:
    """Create a stable sandbox identity for mocked execution."""
    return SandboxHandle("container-id", "user-id", "conversation-id", tmp_path, tmp_path)


@pytest.mark.parametrize("operation", ["list", "preview"])
async def test_service_runs_quoted_inspector_as_system_python(
    handle: SandboxHandle, operation: str,
) -> None:
    service = AsyncMock()
    service.run.return_value = ExecResult(0, json.dumps({"result": {"content": "hello"}}))
    path = "a ' $(touch injected).txt"
    file_viewer_service = FileViewerService(service)
    if operation == "list":
        result = await file_viewer_service.list_directory(handle, root="input", path=path, offset=200)
    else:
        result = await file_viewer_service.preview_file(handle, root="input", path=path)
    assert result == {"content": "hello"}
    arguments = shlex.split(service.run.call_args.args[0])
    assert arguments[:3] == ["/usr/bin/python3", "-I", "-c"]
    assert json.loads(arguments[4]) == {
        "operation": operation, "root": "input", "path": path,
        "offset": 200 if operation == "list" else 0,
    }
    assert service.run.call_args.kwargs == {"name": "container-id", "workdir": "/", "timeout": 15}


async def test_service_rejects_bad_path_before_exec(handle: SandboxHandle) -> None:
    service = AsyncMock()
    with pytest.raises(FileViewerError) as error:
        await FileViewerService(service).preview_file(handle, root="input", path="../secret")
    assert error.value.status == 400
    service.run.assert_not_called()


@pytest.mark.parametrize("failure", [SandboxError("private daemon details"), TimeoutError()])
async def test_service_reports_safe_execution_failure(handle: SandboxHandle, failure: Exception) -> None:
    service = AsyncMock()
    service.run.side_effect = failure
    with pytest.raises(FileViewerError) as error:
        await FileViewerService(service).list_directory(handle, root="input")
    assert error.value.status in {503, 504}
    assert "private" not in str(error.value)


@pytest.mark.parametrize("response", ["not json", "[]", '{}', '{"result":null}', '{"error":"bad","status":200}'])
async def test_service_rejects_invalid_result(handle: SandboxHandle, response: str) -> None:
    service = AsyncMock()
    service.run.return_value = ExecResult(0, response)
    with pytest.raises(FileViewerError) as error:
        await FileViewerService(service).list_directory(handle, root="input")
    assert error.value.status == 503


async def test_service_preserves_inspector_status(handle: SandboxHandle) -> None:
    service = AsyncMock()
    service.run.return_value = ExecResult(0, '{"error":"Missing file","status":404}')
    with pytest.raises(FileViewerError) as error:
        await FileViewerService(service).preview_file(handle, root="input", path="missing")
    assert error.value.status == 404


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_preview_replacement_between_stat_and_open_is_rejected(
    tmp_path: Path, roots: dict, monkeypatch: pytest.MonkeyPatch, replacement: str,
) -> None:
    """Replacing a checked file cannot escape via a link or block on a FIFO."""
    target = tmp_path / "target"
    target.write_text("safe")
    original_open = os.open

    def replacing_open(path, flags, *args, **kwargs):
        if path == "target":
            target.unlink()
            if replacement == "symlink":
                target.symlink_to("/etc/passwd")
            else:
                os.mkfifo(target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replacing_open)
    with pytest.raises(InspectionError):
        inspect_request("preview", "input", "target", roots=roots)
