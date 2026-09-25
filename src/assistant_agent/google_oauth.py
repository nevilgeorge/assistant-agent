"""Google OAuth 2.0 authorization-code flow helpers."""

from __future__ import annotations

import logging

import requests
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from assistant_agent.config import REVOKE_URI, SCOPES, client_config, get_settings

logger = logging.getLogger(__name__)


def build_flow(state: str | None = None, code_verifier: str | None = None) -> Flow:
    flow = Flow.from_client_config(
        client_config(),
        scopes=SCOPES,
        state=state,
        code_verifier=code_verifier,
    )
    flow.redirect_uri = get_settings().redirect_uri
    return flow


def authorization_url() -> tuple[str, str, str]:
    """Return (url, state, code_verifier) for the consent screen.

    `access_type=offline` is what makes Google issue a refresh token at all;
    `prompt=consent` forces a *new* one on every connect. Without the latter,
    reconnecting after deleting tokens.json yields an access token that dies in
    an hour with no way to renew it.

    The Flow uses PKCE: `authorization_url()` generates a `code_verifier` on the
    Flow object and sends Google only its SHA-256 challenge. The token exchange
    happens in a *different* request with a *different* Flow instance, so the
    verifier has to be carried across the redirect alongside `state` -- drop it
    and Google rejects the exchange with "Missing code verifier".
    """
    flow = build_flow()
    url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return url, state, flow.code_verifier


def exchange_code(
    authorization_response_url: str, state: str, code_verifier: str
) -> Credentials:
    flow = build_flow(state=state, code_verifier=code_verifier)
    flow.fetch_token(authorization_response=authorization_response_url)
    return flow.credentials


def account_identity(creds: Credentials) -> tuple[str, str | None]:
    """Return (email, google_account_id) from the ID token in the token response.

    Cheaper and stronger than calling the userinfo endpoint: the ID token is
    already in hand, and verifying it confirms the token was minted for *our*
    client. `sub` is the stable identifier -- emails can change.
    """
    claims = id_token.verify_oauth2_token(
        creds.id_token,
        google_requests.Request(),
        get_settings().google_client_id,
        clock_skew_in_seconds=10,
    )
    return claims["email"], claims.get("sub")


def revoke(creds: Credentials) -> bool:
    """Best-effort revocation so the grant also disappears from the user's
    Google account permissions page. Never raises -- disconnect must always
    succeed locally."""
    token = creds.refresh_token or creds.token
    if not token:
        return False
    try:
        response = requests.post(
            REVOKE_URI,
            data={"token": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        return response.ok
    except requests.RequestException:
        logger.warning("Token revocation request failed.", exc_info=True)
        return False
