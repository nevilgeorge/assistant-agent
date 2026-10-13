"""Authentication and lifecycle boundaries for sandbox file browsing."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import test_web_auth

from assistant_agent.sandbox import SandboxHandle
from assistant_agent.file_viewer import FileViewerError, FileViewerService
from assistant_agent.web_dependencies import get_file_viewer_service
from test_web_auth import client_for, connect

web = test_web_auth.web


async def ready_conversation(web, monkeypatch):
    """Create an authenticated chat-only sandbox without launching Docker."""
    client = await connect(web, monkeypatch, "file-user", "files@example.com")
    user = await web.web_store.user_by_google_sub("file-user")
    conversation = web.conversation_manager.get(user.user_id)
    conversation.sandbox_handle = SandboxHandle(
        "container", user.user_id, conversation.conversation_id, Path("/input"), Path("/input"),
    )
    return client, conversation


@pytest.mark.parametrize("endpoint", ["/api/files", "/api/files/preview"])
async def test_file_routes_require_auth_and_current_owned_sandbox(web, monkeypatch, endpoint):
    file_viewer_service = AsyncMock(spec=FileViewerService)
    inspector = (
        file_viewer_service.preview_file if endpoint.endswith("preview")
        else file_viewer_service.list_directory
    )
    inspector.return_value = {"entries": [], "next_offset": None}
    web.app.dependency_overrides[get_file_viewer_service] = lambda: file_viewer_service
    client, conversation = await ready_conversation(web, monkeypatch)
    params = {"conversation_id": conversation.conversation_id, "root": "workspace"}
    assert (await client_for(web).get(endpoint, params=params)).status_code == 401
    other = await connect(web, monkeypatch, "other-user", "other@example.com")
    assert (await other.get(endpoint, params=params)).status_code == 409
    assert (await client.get(endpoint, params={**params, "conversation_id": "old"})).status_code == 409
    inspector.assert_not_awaited()
    before = conversation.last_activity_monotonic
    response = await client.get(endpoint, params=params)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert conversation.last_activity_monotonic == before
    assert conversation.access_token_id is None
    expected_arguments = {"root": "workspace", "path": ""}
    if not endpoint.endswith("preview"):
        expected_arguments["offset"] = 0
    inspector.assert_awaited_once_with(conversation.sandbox_handle, **expected_arguments)
    assert (await client.get("/api/conversation")).json()["files_available"] is True


async def test_file_browsing_does_not_create_conversation(web, monkeypatch):
    client = await connect(web, monkeypatch, "idle-user", "idle@example.com")
    assert not web.conversation_manager.conversations
    response = await client.get("/api/files", params={"conversation_id": "missing", "root": "input"})
    assert response.status_code == 409
    assert not web.conversation_manager.conversations


async def test_reset_during_read_discards_content(web, monkeypatch):
    client, conversation = await ready_conversation(web, monkeypatch)

    async def inspect(*args, **kwargs):
        conversation.retire()
        return {"content": "must not reach the browser"}

    file_viewer_service = AsyncMock(spec=FileViewerService)
    file_viewer_service.preview_file.side_effect = inspect
    web.app.dependency_overrides[get_file_viewer_service] = lambda: file_viewer_service
    response = await client.get("/api/files/preview", params={
        "conversation_id": conversation.conversation_id, "root": "workspace", "path": "note.md",
    })
    assert response.status_code == 409
    assert "must not reach" not in response.text


async def test_lifespan_shares_sandbox_with_file_viewer(web):
    assert isinstance(web.app.state.file_viewer_service, FileViewerService)
    assert web.app.state.file_viewer_service.sandbox_service is web.app.state.sandbox_service


async def test_file_route_preserves_service_errors(web, monkeypatch):
    client, conversation = await ready_conversation(web, monkeypatch)
    file_viewer_service = AsyncMock(spec=FileViewerService)
    file_viewer_service.preview_file.side_effect = FileViewerError("Missing file", 404)
    web.app.dependency_overrides[get_file_viewer_service] = lambda: file_viewer_service
    response = await client.get("/api/files/preview", params={
        "conversation_id": conversation.conversation_id, "root": "input", "path": "missing",
    })
    assert response.status_code == 404
    assert response.json() == {"error": "Missing file"}
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("state", ["is_starting", "is_retired", "has_failed"])
async def test_unavailable_conversation_denies_files(web, monkeypatch, state):
    client, conversation = await ready_conversation(web, monkeypatch)
    setattr(conversation, state, True)
    response = await client.get("/api/files", params={
        "conversation_id": conversation.conversation_id, "root": "workspace",
    })
    assert response.status_code == 409
