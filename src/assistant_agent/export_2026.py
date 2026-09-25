"""Resumable, read-only export of 2026 Gmail messages and Calendar events."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from assistant_agent.config import DATA_DIR
from assistant_agent.store import TokenStore

START = "2026-01-01T00:00:00Z"
END = "2027-01-01T00:00:00Z"
START_MS = 1767225600000
END_MS = 1798761600000
QUERY = f"after:{START_MS // 1000} before:{END_MS // 1000}"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}
logger = logging.getLogger(__name__)


class RequestGate:
    """Bound both in-flight requests and request starts across all services."""

    def __init__(self, *, interval: float = 0.5, concurrent: int = 4) -> None:
        self.interval = interval
        self.semaphore = threading.BoundedSemaphore(concurrent)
        self.lock = threading.Lock()
        self.next_start = 0.0

    def execute(self, request):
        with self.semaphore:
            with self.lock:
                delay = max(0.0, self.next_start - time.monotonic())
                if delay:
                    time.sleep(delay)
                self.next_start = time.monotonic() + self.interval
            return request.execute(num_retries=0)

    def defer(self, seconds: float) -> None:
        """Pause starts from every worker after a shared quota response."""
        with self.lock:
            self.next_start = max(self.next_start, time.monotonic() + seconds)


def _retry_delay(exc: HttpError) -> float:
    headers = exc.resp
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                pass
    try:
        body = json.loads(exc.content)
        details = body.get("error", {}).get("details", [])
        for detail in details:
            duration = detail.get("retryDelay", "")
            match = re.fullmatch(r"(\d+(?:\.\d+)?)s", duration)
            if match:
                return float(match.group(1))
    except (ValueError, TypeError, AttributeError):
        pass
    return 0.0


def _retryable(exc: HttpError) -> bool:
    if exc.resp.status in RETRYABLE_STATUS:
        return True
    if exc.resp.status != 403:
        return False
    try:
        errors = json.loads(exc.content).get("error", {}).get("errors", [])
        return any(error.get("reason") in RETRYABLE_REASONS for error in errors)
    except (ValueError, TypeError, AttributeError):
        return False


def call(gate: RequestGate, request_factory, *, attempts: int = 8):
    for attempt in range(attempts):
        try:
            return gate.execute(request_factory())
        except HttpError as exc:
            if not _retryable(exc) or attempt == attempts - 1:
                raise
            backoff = min(64.0, 2**attempt) * random.uniform(0.5, 1.5)
            delay = max(backoff, _retry_delay(exc))
            if exc.resp.status in (403, 429):
                gate.defer(delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _safe_name(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Exporter:
    def __init__(self, credentials: Credentials, output: Path = DATA_DIR) -> None:
        self.credentials_json = credentials.to_json()
        self.output = output
        self.gate = RequestGate()
        self.local = threading.local()
        self.count_lock = threading.Lock()
        self.counts = {
            "gmail_listed": 0, "gmail_saved": 0, "gmail_skipped": 0,
            "gmail_outside_window": 0, "calendar_count": 0,
            "calendar_saved": 0, "calendar_skipped": 0,
        }
        self.incomplete: list[str] = []

    def _service(self, name: str):
        if not hasattr(self.local, "services"):
            self.local.services = {}
        if name not in self.local.services:
            creds = Credentials.from_authorized_user_info(json.loads(self.credentials_json))
            version = "v1" if name == "gmail" else "v3"
            self.local.services[name] = build(name, version, credentials=creds, cache_discovery=False)
        return self.local.services[name]

    def _message(self, message_id: str) -> str:
        path = self.output / "emails" / "2026" / f"{message_id}.json"
        if path.is_file():
            return "skipped"
        gmail = self._service("gmail")
        message = call(self.gate, lambda: gmail.users().messages().get(
            userId="me", id=message_id, format="full"
        ))
        stamp = int(message["internalDate"])
        if not START_MS <= stamp < END_MS or {"SPAM", "TRASH"} & set(message.get("labelIds", [])):
            return "outside_window"
        _atomic_json(path, message)
        return "saved"

    def export_gmail(self, pool: ThreadPoolExecutor) -> None:
        page_token = None
        seen_pages: set[str] = set()
        seen_ids: set[str] = set()
        pending = {}

        def collect(done):
            for future in done:
                message_id = pending.pop(future)
                try:
                    result = future.result()
                    self.counts[f"gmail_{result}"] += 1
                except Exception as exc:
                    self.incomplete.append(f"Gmail message {message_id}: {exc}")

        try:
            gmail = self._service("gmail")
            while True:
                page = call(self.gate, lambda: gmail.users().messages().list(
                    userId="me", q=QUERY, maxResults=500,
                    includeSpamTrash=False, pageToken=page_token
                ))
                if not seen_pages:
                    logger.info("Gmail estimates %s matching messages", page.get("resultSizeEstimate", "an unknown number of"))
                for item in page.get("messages", []):
                    message_id = item["id"]
                    if message_id in seen_ids:
                        continue
                    seen_ids.add(message_id)
                    self.counts["gmail_listed"] += 1
                    if self.counts["gmail_listed"] % 500 == 0:
                        logger.info("Gmail listed %s unique messages", self.counts["gmail_listed"])
                    if len(pending) >= 16:
                        done, _ = wait(pending, return_when=FIRST_COMPLETED)
                        collect(done)
                    pending[pool.submit(self._message, message_id)] = message_id
                next_token = page.get("nextPageToken")
                if not next_token:
                    break
                if next_token in seen_pages:
                    raise RuntimeError(f"repeated Gmail page token {next_token}")
                seen_pages.add(next_token)
                page_token = next_token
        except Exception as exc:
            self.incomplete.append(f"Gmail pagination: {exc}")
        finally:
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                collect(done)

    def _calendar(self, calendar_id: str) -> None:
        calendar = self._service("calendar")
        directory = self.output / "calendar" / "2026" / _safe_name(calendar_id)
        page_token = None
        seen_pages: set[str] = set()
        while True:
            page = call(self.gate, lambda: calendar.events().list(
                calendarId=calendar_id, timeMin=START, timeMax=END,
                singleEvents=True, showDeleted=False, maxResults=2500,
                pageToken=page_token
            ))
            for event in page.get("items", []):
                if event.get("status") == "cancelled":
                    continue
                event_id = event["id"]
                path = directory / f"{_safe_name(event_id)}.json"
                if path.is_file():
                    with self.count_lock:
                        self.counts["calendar_skipped"] += 1
                else:
                    _atomic_json(path, {"calendarId": calendar_id, "event": event})
                    with self.count_lock:
                        self.counts["calendar_saved"] += 1
            next_token = page.get("nextPageToken")
            if not next_token:
                return
            if next_token in seen_pages:
                raise RuntimeError(f"repeated Calendar page token {next_token}")
            seen_pages.add(next_token)
            page_token = next_token

    def export_calendar(self, pool: ThreadPoolExecutor) -> None:
        page_token = None
        seen_pages: set[str] = set()
        ids: set[str] = set()
        try:
            calendar = self._service("calendar")
            while True:
                page = call(self.gate, lambda: calendar.calendarList().list(
                    maxResults=250, showDeleted=False, showHidden=False,
                    pageToken=page_token
                ))
                for item in page.get("items", []):
                    if not item.get("hidden") and not item.get("deleted"):
                        ids.add(item["id"])
                next_token = page.get("nextPageToken")
                if not next_token:
                    break
                if next_token in seen_pages:
                    raise RuntimeError(f"repeated calendar-list page token {next_token}")
                seen_pages.add(next_token)
                page_token = next_token
        except Exception as exc:
            self.incomplete.append(f"Calendar list pagination: {exc}")
        self.counts["calendar_count"] = len(ids)
        pending = {pool.submit(self._calendar, calendar_id): calendar_id for calendar_id in ids}
        for future, calendar_id in pending.items():
            try:
                future.result()
            except Exception as exc:
                self.incomplete.append(f"Calendar {calendar_id}: {exc}")

    def run(self, *, calendar_only: bool = False) -> dict:
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.output, 0o700)
        operations = (("Calendar", self.export_calendar),) if calendar_only else (
            ("Gmail", self.export_gmail), ("Calendar", self.export_calendar)
        )
        with ThreadPoolExecutor(max_workers=4) as pool:
            for name, operation in operations:
                try:
                    operation(pool)
                except Exception as exc:
                    self.incomplete.append(f"{name} export: {exc}")
        report = {
            "mode": "calendar-only" if calendar_only else "gmail-and-calendar",
            "window": {"start": START, "end_exclusive": END},
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "complete": not self.incomplete,
            "counts": (
                {key: value for key, value in self.counts.items() if key.startswith("calendar_")}
                if calendar_only else self.counts
            ),
            "incomplete": self.incomplete,
        }
        report_name = "export-2026-calendar-report.json" if calendar_only else "export-2026-report.json"
        _atomic_json(self.output / report_name, report)
        return report


def export_account(
    email: str | None = None, output: Path = DATA_DIR, *, calendar_only: bool = False
) -> dict:
    store = TokenStore()
    accounts = store.list_accounts()
    if not accounts:
        raise RuntimeError("No connected accounts. Connect one using `assistant-agent serve`.")
    if email is None:
        if len(accounts) != 1:
            raise RuntimeError("Multiple connected accounts; pass --email to choose one.")
        email = accounts[0].email
    credentials = store.load_refreshed(email)
    if credentials is None:
        raise RuntimeError(f"Credentials for {email} need reconnecting.")
    return Exporter(credentials, output).run(calendar_only=calendar_only)
