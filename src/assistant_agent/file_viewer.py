"""Read-only file inspection through the current user's agent sandbox."""

from __future__ import annotations

import asyncio
from importlib import resources
import json
import shlex
from typing import Any

from .file_inspector import InspectionError, validate_request
from .sandbox import Sandbox, SandboxError, SandboxHandle


class FileViewerError(Exception):
    """A safe HTTP-facing file viewer error."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class FileViewerService:
    """Inspect agent-visible files using the application's shared sandbox service."""

    def __init__(self, sandbox_service: Sandbox) -> None:
        """Borrow the sandbox service without owning its lifecycle."""
        self.sandbox_service = sandbox_service

    async def list_directory(
        self, handle: SandboxHandle, *, root: str, path: str = "", offset: int = 0,
    ) -> dict[str, Any]:
        """List a page of entries in an agent-visible directory."""
        return await self._inspect(handle, operation="list", root=root, path=path, offset=offset)

    async def preview_file(
        self, handle: SandboxHandle, *, root: str, path: str,
    ) -> dict[str, Any]:
        """Return metadata and a bounded text preview of an agent-visible file."""
        return await self._inspect(handle, operation="preview", root=root, path=path)

    async def _inspect(
        self, handle: SandboxHandle, *, operation: str, root: str, path: str, offset: int = 0,
    ) -> dict[str, Any]:
        """Run the packaged inspector as the agent without allocating a sandbox."""
        try:
            validate_request(operation, root, path, offset)
        except InspectionError as error:
            raise FileViewerError(str(error), error.status) from error
        inspector_source = resources.files("assistant_agent").joinpath("file_inspector.py").read_text()
        request = json.dumps({"operation": operation, "root": root, "path": path, "offset": offset})
        # Debian's system Python is independent of agent-modifiable PATH and startup files.
        command = shlex.join(["/usr/bin/python3", "-I", "-c", inspector_source, request])
        try:
            async with asyncio.timeout(15):
                execution = await self.sandbox_service.run(
                    command, name=handle.container_id, workdir="/", timeout=15,
                )
        except TimeoutError as error:
            raise FileViewerError("File inspection timed out. Please retry.", 504) from error
        except SandboxError as error:
            raise FileViewerError("The sandbox is unavailable. Please retry.", 503) from error
        try:
            if not execution.ok:
                raise ValueError("Inspector did not complete")
            response = json.loads(execution.output)
            if not isinstance(response, dict):
                raise ValueError("Invalid inspector response")
            if "error" in response:
                if response.get("status") not in {400, 403, 404, 415, 503, 504}:
                    raise ValueError("Invalid inspector error")
                raise FileViewerError(str(response["error"]), response["status"])
            if not isinstance(response.get("result"), dict):
                raise ValueError("Invalid inspector result")
            return response["result"]
        except (ValueError, TypeError) as error:
            raise FileViewerError("File inspection failed. Please retry.", 503) from error
