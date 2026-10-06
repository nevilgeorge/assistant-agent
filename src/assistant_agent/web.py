"""Public Google connection app with per-user PostgreSQL sessions."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
import hmac
import json
import logging
import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, FastAPI, Request
from anyio import CancelScope
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

from assistant_agent.sandbox import Sandbox
from assistant_agent.async_workers import AsyncWorker
from assistant_agent.database import make_async_engine, make_async_session_factory
from assistant_agent.chat import ChatError, Conversation, ConversationManager
from assistant_agent.config import ConfigError, SCOPES, TEMPLATES_DIR, get_settings
from assistant_agent.google_oauth import account_identity, authorization_url, exchange_code, revoke
from assistant_agent.web_store import WebStore
from assistant_agent.gmail_service import GmailService

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if not settings.database_url or not settings.credential_encryption_key:
        raise ConfigError("Web app requires DATABASE_URL and CREDENTIAL_ENCRYPTION_KEY.")
    engine = make_async_engine(settings.database_url)
    chat = None
    gmail = None
    sandbox = Sandbox()
    try:
        app.state.settings = settings
        app.state.google_worker = AsyncWorker()
        app.state.store = WebStore(
            make_async_session_factory(engine),
            settings.credential_encryption_key,
            app.state.google_worker,
        )
        gmail = GmailService(app.state.store)
        app.state.gmail = gmail
        app.state.sandbox = sandbox
        chat = ConversationManager(service=sandbox)
        app.state.chat = chat
        await chat.start()
        yield
    finally:
        # Cleanup also runs if startup fails or the lifespan is cancelled.
        with CancelScope(shield=True):
            try:
                if chat is not None:
                    await chat.close()
            finally:
                try:
                    await sandbox.close()
                finally:
                    try:
                        if gmail is not None:
                            await gmail.aclose()
                    finally:
                        await engine.dispose()


def create_app() -> FastAPI:
    application = FastAPI(title="assistant-agent", lifespan=lifespan)
    application.include_router(router)
    application.mount("/static", StaticFiles(directory=TEMPLATES_DIR.parent / "static"), name="static")
    return application


COOKIE = "assistant_agent_session"
HEARTBEAT_SECONDS = 15


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None


def _cookie(request: Request, response, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=14 * 86400,
        secure=request.app.state.settings.is_production,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _render(request: Request, name: str, **context):
    return templates.TemplateResponse(request=request, name=name, context=context)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    store = request.app.state.store
    session = await store.session(request.cookies.get(COOKIE))
    user = await store.user(session.user_id) if session else None
    if user is None:
        return _render(request, "index.html", scopes=SCOPES)
    account = {
        "email": user.email,
        "connected_at": user.connected_at.isoformat(),
        "scopes": json.loads(user.scopes),
        "healthy": bool(await store.load_refreshed(user.user_id)),
    }
    return _render(request, "connected.html", accounts=[account], csrf_token=session.csrf_token)


@router.get("/auth/google/start")
async def start(request: Request):
    store = request.app.state.store
    token, session = await store.new_session()
    nonce = secrets.token_urlsafe(32)
    url, state, verifier = authorization_url(nonce=nonce)
    session.oauth_state, session.oauth_nonce, session.oauth_verifier = state, nonce, verifier
    await store.save_session(session)
    response = RedirectResponse(url, status_code=302)
    _cookie(request, response, token)
    return response


@router.get("/auth/google/callback")
async def callback(request: Request):
    store = request.app.state.store
    token = request.cookies.get(COOKIE)
    session = await store.session(token)
    if (
        session is None
        or not session.oauth_state
        or not session.oauth_nonce
        or not session.oauth_verifier
    ):
        return _bad_request("OAuth session missing or expired.")
    state, nonce, verifier = session.oauth_state, session.oauth_nonce, session.oauth_verifier
    session.oauth_state = session.oauth_nonce = session.oauth_verifier = None
    await store.save_session(session)
    received = request.query_params.get("state")
    if not received or not hmac.compare_digest(state, received):
        return _bad_request("OAuth state mismatch.")
    if request.query_params.get("error"):
        return _bad_request("Google authorization was cancelled.")
    if not request.query_params.get("code"):
        return _bad_request("Authorization code missing.")
    try:
        # Use the configured public HTTPS URL, even behind Caddy's internal HTTP proxy.
        response_url = (
            request.app.state.settings.redirect_uri
            + "?"
            + urlencode(list(request.query_params.multi_items()))
        )
        creds = await request.app.state.google_worker.run(
            exchange_code, response_url, state=state, code_verifier=verifier
        )
        email, sub = await request.app.state.google_worker.run(
            account_identity, creds, expected_nonce=nonce
        )
        if not sub or not creds.refresh_token or not set(SCOPES).issubset(set(creds.scopes or [])):
            return _bad_request("Google did not grant the requested read-only access.")
        user_id = await store.save_credentials(sub, email, creds)
        new_token, _ = await store.rotate(token, user_id)
    except Exception:
        logger.exception("OAuth callback failed")
        return _bad_request("Could not complete Google sign-in.")
    response = RedirectResponse("/", status_code=303)
    _cookie(request, response, new_token)
    return response


@router.post("/disconnect")
async def disconnect(request: Request):
    store = request.app.state.store
    token = request.cookies.get(COOKIE)
    session = await store.session(token)
    if session is None or not session.user_id:
        return JSONResponse({"error": "Authentication required."}, status_code=401)
    form = await request.form()
    supplied = form.get("csrf_token")
    if not isinstance(supplied, str) or not hmac.compare_digest(session.csrf_token, supplied):
        return _bad_request("CSRF token mismatch.")
    creds = await store.load_credentials(session.user_id)
    if creds:
        await request.app.state.google_worker.run(revoke, creds)
    await request.app.state.chat.reset(session.user_id)
    await store.disconnect(session.user_id)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


async def chat_user(request: Request, *, csrf: bool = False) -> str | JSONResponse:
    store = request.app.state.store
    session = await store.session(request.cookies.get(COOKIE))
    if session is None or not session.user_id or await store.user(session.user_id) is None:
        return JSONResponse({"error": "Authentication required."}, status_code=401)
    if csrf and not hmac.compare_digest(
        session.csrf_token, request.headers.get("x-csrf-token", "")
    ):
        return _bad_request("CSRF token mismatch.")
    return session.user_id


def chat_error(exc: ChatError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


@router.post("/api/message")
async def message(request: Request, body: MessageRequest):
    user = await chat_user(request, csrf=True)
    if isinstance(user, JSONResponse):
        return user
    if not body.message.strip():
        return _bad_request("Message cannot be blank.")
    try:
        return JSONResponse(
            await request.app.state.chat.submit(user, body.message, body.conversation_id),
            status_code=202,
        )
    except ChatError as exc:
        return chat_error(exc)


@router.get("/api/conversation")
async def conversation(request: Request):
    user = await chat_user(request)
    if isinstance(user, JSONResponse):
        return user
    try:
        return request.app.state.chat.get(user).snapshot()
    except ChatError as exc:
        return chat_error(exc)


@router.post("/api/conversation/reset")
async def reset_conversation(request: Request):
    user = await chat_user(request, csrf=True)
    if isinstance(user, JSONResponse):
        return user
    await request.app.state.chat.reset(user)
    return {"ok": True}


@router.get("/api/conversation/stream")
async def conversation_stream(request: Request, conversation_id: str, after: int = 0):
    user = await chat_user(request)
    if isinstance(user, JSONResponse):
        return user
    try:
        conversation = request.app.state.chat.get(user)
    except ChatError as exc:
        return chat_error(exc)
    try:
        cursor = int(request.headers.get("last-event-id", after))
    except ValueError:
        return _bad_request("Invalid event sequence.")

    async def events():
        nonlocal cursor
        heartbeat = time.monotonic()
        while not await request.is_disconnected():
            if time.monotonic() - heartbeat >= HEARTBEAT_SECONDS:
                if isinstance(await chat_user(request), JSONResponse):
                    return
                yield ": heartbeat\n\n"
                heartbeat = time.monotonic()
            missing, pending = _event_batch(conversation, conversation_id, cursor)
            if missing:
                yield (
                    "data: "
                    + json.dumps({"type": "reload", "message": "Conversation changed. Reloading."})
                    + "\n\n"
                )
                return
            for event in pending:
                cursor = event["sequence"]
                yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
                if event["type"] == "conversation_reset":
                    return
            await asyncio.sleep(0.1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/healthz")
async def healthz(request: Request):
    store = request.app.state.store
    try:
        async with store.factory() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"ok": False}, status_code=503)
    return {"ok": True}


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)


def _event_batch(
    conversation: Conversation, conversation_id: str, cursor: int
) -> tuple[bool, list[dict]]:
    """Copy replay state synchronously on the owning event loop."""
    oldest = conversation.events[0]["sequence"] if conversation.events else conversation.sequence + 1
    missing = conversation_id != conversation.id or cursor < oldest - 1 or cursor > conversation.sequence
    pending = [dict(e) for e in conversation.events if e["sequence"] > cursor]
    return missing, pending


app = create_app()
