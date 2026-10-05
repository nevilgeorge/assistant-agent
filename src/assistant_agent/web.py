"""Public Google connection app with per-user PostgreSQL sessions."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import logging
import secrets
import time
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import text

from assistant_agent.chat import ChatError, SessionManager
from assistant_agent.config import ConfigError, SCOPES, TEMPLATES_DIR, get_settings
from assistant_agent.google_oauth import account_identity, authorization_url, exchange_code, revoke
from assistant_agent.web_store import WebStore

logger = logging.getLogger(__name__)
settings = get_settings()
if not settings.database_url or not settings.credential_encryption_key:
    raise ConfigError("Web app requires DATABASE_URL and CREDENTIAL_ENCRYPTION_KEY.")
store = WebStore()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
chat = SessionManager()


@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(chat.start)
    try:
        yield
    finally:
        await asyncio.to_thread(chat.close)


app = FastAPI(title="assistant-agent", lifespan=lifespan)
COOKIE = "assistant_agent_session"


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None


def _cookie(response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=14 * 86400, secure=settings.is_production, httponly=True, samesite="lax", path="/")


def _render(request: Request, name: str, **context):
    return templates.TemplateResponse(request=request, name=name, context=context)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    session = store.session(request.cookies.get(COOKIE))
    user = store.user(session.user_id) if session else None
    if user is None:
        return _render(request, "index.html", scopes=SCOPES)
    account = {"email": user.email, "connected_at": user.connected_at.isoformat(), "scopes": json.loads(user.scopes), "healthy": bool(store.load_refreshed(user.user_id))}
    return _render(request, "connected.html", accounts=[account], csrf_token=session.csrf_token)


@app.get("/auth/google/start")
def start(request: Request):
    token, session = store.new_session()
    nonce = secrets.token_urlsafe(32)
    url, state, verifier = authorization_url(nonce=nonce)
    session.oauth_state, session.oauth_nonce, session.oauth_verifier = state, nonce, verifier
    store.save_session(session)
    response = RedirectResponse(url, status_code=302)
    _cookie(response, token)
    return response


@app.get("/auth/google/callback")
def callback(request: Request):
    token = request.cookies.get(COOKIE)
    session = store.session(token)
    if session is None or not session.oauth_state or not session.oauth_nonce or not session.oauth_verifier:
        return _bad_request("OAuth session missing or expired.")
    state, nonce, verifier = session.oauth_state, session.oauth_nonce, session.oauth_verifier
    session.oauth_state = session.oauth_nonce = session.oauth_verifier = None
    store.save_session(session)
    received = request.query_params.get("state")
    if not received or not hmac.compare_digest(state, received):
        return _bad_request("OAuth state mismatch.")
    if request.query_params.get("error"):
        return _bad_request("Google authorization was cancelled.")
    if not request.query_params.get("code"):
        return _bad_request("Authorization code missing.")
    try:
        # Use the configured public HTTPS URL, even behind Caddy's internal HTTP proxy.
        response_url = settings.redirect_uri + "?" + urlencode(list(request.query_params.multi_items()))
        creds = exchange_code(response_url, state=state, code_verifier=verifier)
        email, sub = account_identity(creds, expected_nonce=nonce)
        if not sub or not creds.refresh_token or not set(SCOPES).issubset(set(creds.scopes or [])):
            return _bad_request("Google did not grant the requested read-only access.")
        user_id = store.save_credentials(sub, email, creds)
        new_token, _ = store.rotate(token, user_id)
    except Exception:
        logger.exception("OAuth callback failed")
        return _bad_request("Could not complete Google sign-in.")
    response = RedirectResponse("/", status_code=303)
    _cookie(response, new_token)
    return response


@app.post("/disconnect")
async def disconnect(request: Request):
    token = request.cookies.get(COOKIE)
    session = store.session(token)
    if session is None or not session.user_id:
        return JSONResponse({"error": "Authentication required."}, status_code=401)
    form = await request.form()
    supplied = form.get("csrf_token")
    if not isinstance(supplied, str) or not hmac.compare_digest(session.csrf_token, supplied):
        return _bad_request("CSRF token mismatch.")
    creds = store.load_credentials(session.user_id)
    if creds:
        revoke(creds)
    await asyncio.to_thread(chat.reset, session.user_id)
    store.disconnect(session.user_id)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


def chat_user(request: Request, *, csrf=False):
    session = store.session(request.cookies.get(COOKIE))
    if session is None or not session.user_id or store.user(session.user_id) is None:
        return JSONResponse({"error": "Authentication required."}, status_code=401)
    if csrf and not hmac.compare_digest(session.csrf_token, request.headers.get("x-csrf-token", "")):
        return _bad_request("CSRF token mismatch.")
    return session.user_id


def chat_error(exc):
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.post("/api/message")
def message(request: Request, body: MessageRequest):
    user = chat_user(request, csrf=True)
    if isinstance(user, JSONResponse):
        return user
    if not body.message.strip():
        return _bad_request("Message cannot be blank.")
    try:
        return JSONResponse(chat.submit(user, body.message, body.conversation_id), status_code=202)
    except ChatError as exc:
        return chat_error(exc)


@app.get("/api/conversation")
def conversation(request: Request):
    user = chat_user(request)
    if isinstance(user, JSONResponse):
        return user
    try:
        return chat.get(user).snapshot()
    except ChatError as exc:
        return chat_error(exc)


@app.post("/api/conversation/reset")
def reset_conversation(request: Request):
    user = chat_user(request, csrf=True)
    if isinstance(user, JSONResponse):
        return user
    chat.reset(user)
    return {"ok": True}


@app.get("/api/conversation/stream")
async def conversation_stream(request: Request, conversation_id: str, after: int = 0):
    user = chat_user(request)
    if isinstance(user, JSONResponse):
        return user
    try:
        session = chat.get(user)
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
            if time.monotonic() - heartbeat >= 15:
                if isinstance(chat_user(request), JSONResponse):
                    return
                yield ': heartbeat\n\n'
                heartbeat = time.monotonic()
            with session.condition:
                oldest = session.events[0]['sequence'] if session.events else session.sequence + 1
                missing = conversation_id != session.id or cursor < oldest - 1 or cursor > session.sequence
                pending = [dict(e) for e in session.events if e['sequence'] > cursor]
            if missing:
                yield 'data: ' + json.dumps({"type": "reload", "message": "Conversation changed. Reloading."}) + '\n\n'
                return
            for event in pending:
                cursor = event['sequence']
                yield f'id: {cursor}\ndata: {json.dumps(event)}\n\n'
                if event['type'] == 'conversation_reset':
                    return
            await asyncio.sleep(0.1)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/healthz")
def healthz():
    try:
        with store.factory() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"ok": False}, status_code=503)
    return {"ok": True}


def _bad_request(message: str):
    return JSONResponse({"error": message}, status_code=400)
