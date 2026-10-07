"""Read-only live Gmail fixture validation with content-free JSON evidence.

Run only with an explicitly selected connected user and fixture query. Credentials
stay in the existing encrypted store; normal refresh may update that store.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Sequence

from assistant_agent.config import get_settings
from assistant_agent.database import make_async_engine, make_async_session_factory
from assistant_agent.gmail_service import EmailContent, GmailService, SearchResult
from assistant_agent.web_store import WebStore


class FixtureValidationError(Exception):
    """A fixture check failed, without retaining mailbox data in the exception."""


class SafeArgumentParser(argparse.ArgumentParser):
    """Avoid echoing sensitive fixture arguments when command syntax is invalid."""

    def error(self, message: str) -> None:
        """Print static guidance rather than argparse's potentially sensitive message."""
        self.exit(2, "Invalid validation arguments. Use --help for required options.\n")


@dataclass
class ValidationReport:
    """Only check outcomes and live request durations are suitable for retention."""

    data_source: str = "live_google"
    recorded_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    validation_mode: Literal["controlled_fixture", "live_smoke"] = "controlled_fixture"
    attachments_covered: bool = False
    success: bool = False
    checks: dict[str, bool] = field(default_factory=dict)
    search_seconds: list[float] = field(default_factory=list)
    failure: str | None = None


def _require(condition: bool) -> None:
    """Raise a generic fixture failure rather than including source values."""
    if not condition:
        raise FixtureValidationError("Fixture validation failed")


def _complete_page(page: SearchResult) -> None:
    """Require an intact one-message page, permitting further pagination."""
    _require(len(page.messages) == 1)
    _require(not page.errors and not page.metadata_incomplete and not page.truncated)
    _require(not page.messages[0].truncated)
    _require(bool(page.messages[0].message_id and page.messages[0].thread_id))


async def validate_fixture(
    gmail_service: GmailService,
    user_id: str,
    query: str,
    expected_marker: str,
    attachment_sha256: str,
) -> ValidationReport:
    """Check three paginated fixture messages, content, attachment, and search latency.

    Pagination is deliberately bounded to three pages; this never claims the
    result set is exhaustive. At most ten fixture attachments are retrieved.
    No IDs, headers, content, query, or credential values enter the report.
    """
    report = ValidationReport()
    stage = "invalid_fixture"
    try:
        _require(all(value.strip() for value in (user_id, query, expected_marker)))
        _require(re.fullmatch(r"[a-fA-F0-9]{64}", attachment_sha256) is not None)
        stage = "pagination_failed"
        cursor = None
        seen_cursors: set[str] = set()
        messages: list[EmailContent] = []
        seen_ids: set[str] = set()
        for page_index in range(3):
            page = await gmail_service.search_emails(user_id, query, page_size=1, cursor=cursor)
            _complete_page(page)
            message_id = page.messages[0].message_id
            _require(message_id not in seen_ids)
            seen_ids.add(message_id)
            stage = "message_failed"
            message = await gmail_service.get_email(user_id, message_id)
            _require(isinstance(message, EmailContent))
            _require(not message.errors and not message.truncated)
            _require(message.message_id == message_id and bool(message.thread_id))
            _require(expected_marker in message.body)
            messages.append(message)
            stage = "pagination_failed"
            if page_index < 2:
                _require(page.has_more and isinstance(page.cursor, str) and bool(page.cursor))
                _require(page.cursor not in seen_cursors)
                seen_cursors.add(page.cursor)
                cursor = page.cursor
        report.checks["three_distinct_paginated_messages"] = True
        report.checks["message_marker"] = True

        stage = "thread_failed"
        thread = await gmail_service.get_thread(user_id, messages[0].thread_id)
        _require(not thread.errors and not thread.truncated and not thread.ids_truncated)
        _require(thread.omitted_messages == 0 and bool(thread.messages))
        _require(thread.thread_id == messages[0].thread_id)
        _require(messages[0].message_id in thread.message_ids)
        _require(all(not message.errors and not message.truncated for message in thread.messages))
        _require(any(expected_marker in message.body for message in thread.messages))
        report.checks["thread_marker"] = True

        stage = "attachment_failed"
        attachments = [
            (message.message_id, attachment.attachment_id)
            for message in messages for attachment in message.attachments
            if attachment.attachment_id
        ]
        _require(0 < len(attachments) <= 10)
        matched_attachment = False
        for message_id, attachment_id in attachments:
            attachment = await gmail_service.get_attachment(user_id, message_id, attachment_id)
            _require(attachment.message_id == message_id)
            _require(attachment.attachment_id == attachment_id)
            if hashlib.sha256(attachment.data).hexdigest() == attachment_sha256.lower():
                matched_attachment = True
                break
        _require(matched_attachment)
        report.checks["attachment_sha256"] = True
        report.attachments_covered = True

        stage = "timing_search_failed"
        for _ in range(5):
            started = time.perf_counter()
            page = await gmail_service.search_emails(user_id, query, page_size=1)
            elapsed = time.perf_counter() - started
            _complete_page(page)
            report.search_seconds.append(round(elapsed, 6))
        report.checks["five_sequential_live_searches"] = True
        report.success = True
    except Exception:
        # Never echo SDK exceptions; they may include URLs, identifiers, or body.
        report.failure = stage
    return report


async def validate_smoke(
    gmail_service: GmailService, user_id: str, query: str,
) -> ValidationReport:
    """Check bounded live retrieval consistency without independent fixture claims.

    Read at most three one-message search pages, one thread, and one explicitly
    inventoried attachment. Absence of an attachment is reported as uncovered.
    """
    report = ValidationReport(validation_mode="live_smoke")
    stage = "invalid_smoke_arguments"
    try:
        _require(bool(user_id.strip() and query.strip()))
        stage = "pagination_failed"
        cursor = None
        seen_cursors: set[str] = set()
        seen_ids: set[str] = set()
        messages: list[EmailContent] = []
        for page_index in range(3):
            page = await gmail_service.search_emails(user_id, query, page_size=1, cursor=cursor)
            _complete_page(page)
            preview = page.messages[0]
            _require(preview.message_id not in seen_ids)
            seen_ids.add(preview.message_id)
            stage = "message_failed"
            message = await gmail_service.get_email(user_id, preview.message_id)
            _require(isinstance(message, EmailContent))
            _require(not message.errors and not message.truncated)
            _require(message.message_id == preview.message_id)
            _require(message.thread_id == preview.thread_id)
            messages.append(message)
            stage = "pagination_failed"
            if not page.has_more or page_index == 2:
                break
            _require(isinstance(page.cursor, str) and bool(page.cursor))
            _require(page.cursor not in seen_cursors)
            seen_cursors.add(page.cursor)
            cursor = page.cursor
        report.checks["bounded_distinct_message_consistency"] = True

        stage = "thread_failed"
        selected_message = messages[0]
        thread = await gmail_service.get_thread(user_id, selected_message.thread_id)
        _require(not thread.errors and not thread.truncated and not thread.ids_truncated)
        _require(thread.omitted_messages == 0 and bool(thread.messages))
        _require(thread.thread_id == selected_message.thread_id)
        _require(selected_message.message_id in thread.message_ids)
        _require(any(message.message_id == selected_message.message_id for message in thread.messages))
        _require(all(
            not message.errors and not message.truncated
            and message.thread_id == selected_message.thread_id for message in thread.messages
        ))
        report.checks["thread_identity_consistency"] = True

        stage = "attachment_failed"
        selected_attachment = next((
            (message.message_id, attachment) for message in messages
            for attachment in message.attachments if attachment.attachment_id
        ), None)
        if selected_attachment is not None:
            message_id, inventory = selected_attachment
            attachment = await gmail_service.get_attachment(
                user_id, message_id, inventory.attachment_id,
            )
            _require(attachment.message_id == message_id)
            _require(attachment.attachment_id == inventory.attachment_id)
            _require(isinstance(attachment.data, bytes) and len(attachment.data) == inventory.size)
            report.checks["attachment_identity_and_byte_count_consistency"] = True
            report.attachments_covered = True

        stage = "timing_search_failed"
        for _ in range(5):
            started = time.perf_counter()
            page = await gmail_service.search_emails(user_id, query, page_size=1)
            elapsed = time.perf_counter() - started
            _complete_page(page)
            report.search_seconds.append(round(elapsed, 6))
        report.checks["five_sequential_live_searches"] = True
        report.success = True
    except Exception:
        report.failure = stage
    return report


async def run_validation(
    user_id: str, query: str, expected_marker: str | None = None,
    attachment_sha256: str | None = None, *, smoke: bool = False,
) -> ValidationReport:
    """Open existing app services, validate the selected user, and close resources."""
    database_engine = None
    gmail_service = None
    report = ValidationReport(
        validation_mode="live_smoke" if smoke else "controlled_fixture",
        failure="configuration_failed",
    )
    try:
        settings = get_settings()
        database_engine = make_async_engine(settings.database_url)
        store = WebStore(make_async_session_factory(database_engine),
                         key=settings.credential_encryption_key)
        report.failure = "selected_user_unavailable"
        if await store.user(user_id) is None:
            return report
        report.failure = "service_failed"
        gmail_service = GmailService(store)
        report.failure = "validation_failed"
        if smoke:
            report = await validate_smoke(gmail_service, user_id, query)
        else:
            report = await validate_fixture(
                gmail_service, user_id, query, expected_marker or "", attachment_sha256 or "",
            )
    except Exception:
        pass
    finally:
        cleanup_failed = False
        if gmail_service is not None:
            try:
                await gmail_service.aclose()
            except Exception:
                cleanup_failed = True
        if database_engine is not None:
            try:
                await database_engine.dispose()
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            report.success = False
            report.failure = "cleanup_failed"
    return report


def write_report(path: Path, serialized_report: str) -> None:
    """Atomically write safe evidence with private modes, leaving no staging file."""
    temporary_path = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".gmail-validation-", dir=path.parent)
        temporary_path = Path(name)
        with os.fdopen(descriptor, "w") as output_file:
            output_file.write(serialized_report + "\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main(arguments: Sequence[str] | None = None) -> int:
    """Print only safe JSON evidence, without SDK logging or raw failure strings."""
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--expected-marker")
    parser.add_argument("--attachment-sha256")
    parser.add_argument("--smoke", action="store_true",
                        help="Check live sample consistency without controlled fixture claims")
    parser.add_argument("--report", type=Path)
    options = parser.parse_args(arguments)
    if options.smoke:
        if options.expected_marker is not None or options.attachment_sha256 is not None:
            parser.error("Smoke mode excludes fixture arguments")
    elif options.expected_marker is None or options.attachment_sha256 is None:
        parser.error("Controlled fixture mode requires marker and hash")
    previous_logging_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if options.smoke:
            report = asyncio.run(run_validation(options.user_id, options.query, smoke=True))
        else:
            report = asyncio.run(run_validation(
                options.user_id, options.query, options.expected_marker, options.attachment_sha256,
            ))
        serialized_report = json.dumps(asdict(report), sort_keys=True)
        if options.report is not None:
            try:
                write_report(options.report, serialized_report)
            except Exception:
                report.success = False
                report.failure = "report_write_failed"
                serialized_report = json.dumps(asdict(report), sort_keys=True)
        print(serialized_report)
        return 0 if report.success else 1
    finally:
        logging.disable(previous_logging_level)


if __name__ == "__main__":
    raise SystemExit(main())
