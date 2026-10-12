"""Resolve and cache each user's primary Google Calendar timezone."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httplib2
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from assistant_agent.async_workers import AsyncWorker
from assistant_agent.web_store import WebStore

logger = logging.getLogger(__name__)
CALENDAR_TIMEOUT_SECONDS = 5


def valid_timezone(name: str | None) -> str | None:
    """Return a supported timezone key that fits the persisted column."""
    if not isinstance(name, str) or not name or len(name) > 64:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def fetch_primary_calendar_timezone(credentials: Credentials) -> str | None:
    """Read the primary calendar using one bounded, token-only HTTP transport."""
    transport = httplib2.Http(timeout=CALENDAR_TIMEOUT_SECONDS)
    try:
        authorized_transport = AuthorizedHttp(
            Credentials(token=credentials.token, scopes=credentials.scopes),
            http=transport,
            max_refresh_attempts=0,
        )
        calendar_service = build(
            "calendar", "v3", http=authorized_transport,
            cache_discovery=False, static_discovery=True, num_retries=0,
        )
        calendar = calendar_service.calendars().get(calendarId="primary").execute(num_retries=0)
        return valid_timezone(calendar.get("timeZone")) if isinstance(calendar, dict) else None
    finally:
        transport.close()


class UserTimezoneService:
    """Use cached timezone metadata, retrying failed lazy lookups on later calls."""

    def __init__(self, web_store: WebStore, google_worker: AsyncWorker | None = None) -> None:
        self.web_store = web_store
        self.google_worker = google_worker if google_worker is not None else web_store.google_worker

    async def resolve(self, user_id: str) -> str:
        """Return the cached or fetched timezone, falling back to unpersisted UTC."""
        try:
            user = await self.web_store.user(user_id)
            if user is None:
                return "UTC"
            stored_timezone = valid_timezone(user.timezone)
            if stored_timezone is not None:
                return stored_timezone
            credentials = await self.web_store.load_refreshed(user_id)
            if credentials is None:
                return "UTC"
            timezone_name = await self.google_worker.run(fetch_primary_calendar_timezone, credentials)
            if timezone_name is not None:
                await self.web_store.save_timezone(user_id, timezone_name)
                return timezone_name
        except Exception:
            # Google exceptions can include raw responses and tokens; omit exception details.
            logger.warning("User timezone resolution failed")
        return "UTC"
