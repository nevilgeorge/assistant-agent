"""Environment-backed settings for the OAuth connect app."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Read-only access to mail and calendars, plus just enough to learn which account
# said yes. gmail.readonly is a "restricted" scope and calendar.readonly is
# "sensitive" in Google's verification tiers -- see README for what that means.
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
DATA_DIR = Path.cwd() / "data"
TOKENS_PATH = DATA_DIR / "tokens.json"

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
CERTS_URI = "https://www.googleapis.com/oauth2/v1/certs"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"

CALLBACK_PATH = "/auth/google/callback"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Settings:
    google_client_id: str
    google_client_secret: str
    session_secret: str
    app_env: str
    host: str
    port: int
    base_url: str

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}{CALLBACK_PATH}"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill in your "
            "Google OAuth client credentials (see README.md)."
        )
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    session_secret = os.getenv("SESSION_SECRET", "").strip()
    app_env = os.getenv("APP_ENV", "development").strip() or "development"

    if not session_secret:
        if app_env == "production":
            raise ConfigError("SESSION_SECRET must be set when APP_ENV=production.")
        session_secret = secrets.token_urlsafe(32)
        logger.warning(
            "SESSION_SECRET is unset; using a random one. Sessions will not "
            "survive a restart. Set it in .env to silence this."
        )

    settings = Settings(
        google_client_id=_required("GOOGLE_CLIENT_ID"),
        google_client_secret=_required("GOOGLE_CLIENT_SECRET"),
        session_secret=session_secret,
        app_env=app_env,
        host=os.getenv("HOST", "localhost"),
        port=int(os.getenv("PORT", "8000")),
        base_url=os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
    )

    _apply_oauthlib_flags(settings)
    return settings


def _apply_oauthlib_flags(settings: Settings) -> None:
    """Two oauthlib behaviours that otherwise break a localhost Google flow."""
    # oauthlib refuses any non-https redirect URI. Google itself permits plain
    # HTTP for loopback redirects, so the check is spurious here -- but keep it
    # gated on the dev flag so it can never be relaxed in a real deployment.
    if not settings.is_production:
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

    # Requesting `openid` makes Google echo the granted scopes reordered (and
    # occasionally expanded), which oauthlib treats as a scope-change warning
    # and raises on. Relax it, or essentially every callback fails.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def client_config() -> dict:
    """The dict shape `Flow.from_client_config` expects, built from env vars."""
    settings = get_settings()
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "auth_provider_x509_cert_url": CERTS_URI,
            "redirect_uris": [settings.redirect_uri],
        }
    }
