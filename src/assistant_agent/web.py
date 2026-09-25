"""FastAPI app serving the connect page and the OAuth callback."""

from __future__ import annotations

import hmac
import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from assistant_agent.config import SCOPES, TEMPLATES_DIR, get_settings
from assistant_agent.google_oauth import (
    account_identity,
    authorization_url,
    exchange_code,
    revoke,
)
from assistant_agent.store import TokenStore

logger = logging.getLogger(__name__)

settings = get_settings()
store = TokenStore()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="assistant-agent")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="assistant_agent_session",
    # Must be "lax", not "strict": the OAuth callback is a cross-site top-level
    # navigation from accounts.google.com, and "strict" would withhold the
    # cookie, leaving us with no stored state to check.
    same_site="lax",
    https_only=settings.is_production,
    max_age=60 * 60 * 24 * 14,
)


def _render(request: Request, name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={"flash": request.session.pop("flash", None), **context},
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    accounts = store.list_accounts()
    if not accounts:
        return _render(request, "index.html", scopes=SCOPES)

    rows = [
        {
            "email": account.email,
            "connected_at": account.connected_at,
            "scopes": account.scopes,
            "healthy": store.load_refreshed(account.email) is not None,
        }
        for account in accounts
    ]
    return _render(request, "connected.html", accounts=rows)


@app.get("/auth/google/start")
def start(request: Request) -> RedirectResponse:
    url, state, code_verifier = authorization_url()
    request.session["oauth_state"] = state
    # PKCE: the verifier is generated with the authorization URL but is only
    # needed at token exchange, one request later. It must survive the redirect.
    request.session["oauth_code_verifier"] = code_verifier
    return RedirectResponse(url, status_code=302)


@app.get("/auth/google/callback")
def callback(request: Request):
    expected_state = request.session.pop("oauth_state", None)
    code_verifier = request.session.pop("oauth_code_verifier", None)

    error = request.query_params.get("error")
    if error:
        request.session["flash"] = f"Connection cancelled by Google: {error}"
        return RedirectResponse("/", status_code=303)

    received_state = request.query_params.get("state")
    if not expected_state or not received_state:
        return _bad_request(
            "Missing OAuth state: the browser sent no session cookie with the "
            f"callback. Start the flow from {settings.base_url} rather than "
            "another hostname -- cookies set on one host are not sent to another."
        )
    if not hmac.compare_digest(expected_state, received_state):
        return _bad_request("OAuth state mismatch; the request was rejected.")

    try:
        creds = exchange_code(
            str(request.url), state=expected_state, code_verifier=code_verifier
        )
        email, sub = account_identity(creds)
    except Exception:
        logger.exception("OAuth code exchange failed.")
        return _bad_request("Could not complete the Google sign-in. Check server logs.")

    store.save(email, creds, sub=sub)
    request.session["active_email"] = email

    if creds.refresh_token:
        request.session["flash"] = f"Connected {email}."
    else:
        # Should not happen given prompt=consent, but it would leave us with a
        # credential that silently dies in an hour -- say so rather than hide it.
        logger.warning("Google returned no refresh token for %s.", email)
        request.session["flash"] = (
            f"Connected {email}, but Google returned no refresh token. "
            "Access will expire in about an hour; try disconnecting and "
            "reconnecting."
        )
    return RedirectResponse("/", status_code=303)


@app.post("/disconnect/{email}")
def disconnect(request: Request, email: str) -> RedirectResponse:
    creds = store.load(email)
    if creds is not None:
        revoke(creds)

    if store.delete(email):
        request.session["flash"] = f"Disconnected {email}."
    else:
        request.session["flash"] = f"No connected account named {email}."

    if request.session.get("active_email") == email:
        request.session.pop("active_email", None)
    return RedirectResponse("/", status_code=303)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)
