"""On-disk storage for connected Google accounts.

The client id/secret are deliberately *not* written here: they belong to the
app, already live in .env, and duplicating the secret into a second file only
widens the leak surface. They are stripped on save and merged back in on load.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from assistant_agent.config import DATA_DIR, TOKENS_PATH, get_settings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Account:
    email: str
    sub: str | None
    connected_at: str
    scopes: list[str]


class TokenStore:
    def __init__(self, path: Path = TOKENS_PATH) -> None:
        self.path = path

    # --- reading -------------------------------------------------------

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "accounts": {}}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.exception("Could not read %s; treating as empty.", self.path)
            return {"version": SCHEMA_VERSION, "accounts": {}}
        data.setdefault("accounts", {})
        return data

    def list_accounts(self) -> list[Account]:
        return [
            Account(
                email=entry.get("email", email),
                sub=entry.get("sub"),
                connected_at=entry.get("connected_at", ""),
                scopes=entry.get("scopes", []),
            )
            for email, entry in sorted(self._read()["accounts"].items())
        ]

    def load(self, email: str) -> Credentials | None:
        entry = self._read()["accounts"].get(email)
        if not entry:
            return None
        settings = get_settings()
        info = {
            **entry["credentials"],
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
        }
        return Credentials.from_authorized_user_info(info, scopes=info.get("scopes"))

    def load_refreshed(self, email: str) -> Credentials | None:
        """Load credentials, refreshing (and re-saving) them if expired.

        Returns None when the refresh token is dead -- most often because the
        OAuth app is still in "Testing", where Google expires refresh tokens
        after 7 days. Callers should render a "reconnect" prompt, not a 500.
        """
        creds = self.load(email)
        if creds is None:
            return None
        if creds.valid:
            return creds
        if not creds.refresh_token:
            logger.warning("No refresh token stored for %s.", email)
            return None
        try:
            creds.refresh(Request())
        except RefreshError:
            logger.warning("Refresh failed for %s; reconnect required.", email)
            return None
        self.save(email, creds)
        return creds

    # --- writing -------------------------------------------------------

    def save(self, email: str, creds: Credentials, *, sub: str | None = None) -> None:
        data = self._read()
        existing = data["accounts"].get(email, {})

        blob = json.loads(creds.to_json())
        blob.pop("client_id", None)
        blob.pop("client_secret", None)

        data["accounts"][email] = {
            "email": email,
            "sub": sub or existing.get("sub"),
            "connected_at": existing.get("connected_at")
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scopes": list(creds.scopes or []),
            "credentials": blob,
        }
        self._write(data)

    def delete(self, email: str) -> bool:
        data = self._read()
        if data["accounts"].pop(email, None) is None:
            return False
        self._write(data)
        return True

    def _write(self, data: dict) -> None:
        """Atomic, owner-only write -- a truncated file loses the refresh token."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(DATA_DIR, 0o700)

        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
