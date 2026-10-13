"""Standalone, standard-library-only inspection of allowlisted sandbox files."""

from __future__ import annotations

import codecs
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import datetime, timezone
import errno
import heapq
import json
import os
import signal
import stat
import sys
from typing import Any

ROOTS = {"input": "/input", "workspace": "/workspace", "daily-logs": "/daily-logs"}
PREVIEW_LIMIT = 1024 * 1024
PAGE_SIZE = 200


class InspectionError(Exception):
    """A safe, user-facing file inspection failure."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def validate_request(operation: str, root: str, path: str, offset: int) -> list[str]:
    """Validate the operation and a strictly relative, canonical path."""
    if operation not in {"list", "preview"} or root not in ROOTS:
        raise InspectionError("Invalid file operation or root.")
    if not isinstance(path, str) or "\x00" in path or len(path) > 4096:
        raise InspectionError("Invalid file path.")
    components = path.split("/") if path else []
    if any(component in {"", ".", ".."} for component in components):
        raise InspectionError("File paths must stay within the selected folder.")
    if type(offset) is not int or offset < 0:
        raise InspectionError("Invalid directory offset.")
    if operation == "preview" and not components:
        raise InspectionError("Select a file to preview.")
    return components


def _metadata(name: str, path: str, file_stat: os.stat_result) -> dict[str, Any]:
    """Build metadata without following links."""
    kind = "unavailable"
    if stat.S_ISDIR(file_stat.st_mode):
        kind = "directory"
    elif stat.S_ISREG(file_stat.st_mode):
        kind = "file"
    return {
        "name": name,
        "path": path,
        "kind": kind,
        "size": file_stat.st_size,
        "modified": datetime.fromtimestamp(file_stat.st_mtime, timezone.utc).isoformat(),
    }


def inspect_request(
    operation: str,
    root: str,
    path: str = "",
    offset: int = 0,
    *,
    roots: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Inspect files through pinned descriptors without traversing symbolic links."""
    components = validate_request(operation, root, path, offset)
    root_path = (ROOTS if roots is None else roots)[root]
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        with ExitStack() as stack:
            descriptor = os.open(root_path, directory_flags)
            stack.callback(os.close, descriptor)
            for component in components if operation == "list" else components[:-1]:
                descriptor = os.open(component, directory_flags, dir_fd=descriptor)
                stack.callback(os.close, descriptor)
            if operation == "list":
                with os.scandir(descriptor) as entries:
                    def metadata_entries() -> Iterator[dict[str, Any]]:
                        """Skip entries removed while scanning the directory."""
                        for entry in entries:
                            try:
                                entry_stat = entry.stat(follow_symlinks=False)
                            except FileNotFoundError:
                                continue
                            entry_path = f"{path}/{entry.name}" if path else entry.name
                            yield _metadata(entry.name, entry_path, entry_stat)

                    # Retain only the requested prefix rather than the entire directory.
                    selected = heapq.nsmallest(
                        offset + PAGE_SIZE + 1,
                        metadata_entries(),
                        key=lambda entry: (
                            entry["kind"] != "directory", entry["name"].casefold(), entry["name"]
                        ),
                    )
                return {
                    "entries": selected[offset:offset + PAGE_SIZE],
                    "next_offset": offset + PAGE_SIZE if len(selected) > offset + PAGE_SIZE else None,
                }
            name = components[-1]
            initial_stat = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(initial_stat.st_mode):
                raise InspectionError("Only regular files can be previewed.", 415)
            # O_NONBLOCK also prevents a concurrent replacement with a FIFO from blocking.
            file_descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=descriptor,
            )
            stack.callback(os.close, file_descriptor)
            file_stat = os.fstat(file_descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise InspectionError("Only regular files can be previewed.", 415)
            metadata = _metadata(name, path, file_stat)
            metadata.pop("kind")
            with os.fdopen(os.dup(file_descriptor), "rb") as file_stream:
                raw_content = file_stream.read(PREVIEW_LIMIT + 1)
            truncated = len(raw_content) > PREVIEW_LIMIT
            raw_content = raw_content[:PREVIEW_LIMIT]
            content = None
            preview_format = "binary"
            if b"\x00" not in raw_content:
                try:
                    content = codecs.getincrementaldecoder("utf-8")().decode(
                        raw_content, final=not truncated
                    )
                    preview_format = "markdown" if name.lower().endswith((".md", ".markdown")) else "text"
                except UnicodeDecodeError:
                    pass
            return {
                **metadata, "format": preview_format, "content": content, "truncated": truncated,
            }
    except FileNotFoundError as error:
        raise InspectionError("The file or folder no longer exists.", 404) from error
    except PermissionError as error:
        raise InspectionError("The agent cannot read this file or folder.", 403) from error
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise InspectionError("Links cannot be followed and paths must name folders.", 400) from error
        raise InspectionError("The file or folder could not be read.", 503) from error


def main() -> None:
    """Return a bounded JSON result and stop long-running filesystem operations."""
    def timed_out(_signum: int, _frame: Any) -> None:
        raise InspectionError("File inspection timed out. Please retry.", 504)

    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(10)
    try:
        request = json.loads(sys.argv[1])
        result = inspect_request(**request)
        print(json.dumps({"result": result}))
    except InspectionError as error:
        print(json.dumps({"error": str(error), "status": error.status}))
    except Exception:
        print(json.dumps({"error": "File inspection failed. Please retry.", "status": 503}))
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    main()
