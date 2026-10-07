"""Fixture integrity and safe evidence checks; these never access a live mailbox."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant_agent import gmail_validation
from assistant_agent.gmail_service import (
    Attachment, BinaryContent, EmailContent, ItemError, SearchResult, ThreadResult,
)


MARKER = "private-fixture-marker"
ATTACHMENT = b"private-fixture-attachment"
ATTACHMENT_HASH = hashlib.sha256(ATTACHMENT).hexdigest()


@pytest.fixture
def fixture_service() -> SimpleNamespace:
    """Supply three intact fixture pages and one exact attachment."""
    messages = [EmailContent(f"private-id-{index}", "private-thread", body=MARKER)
                for index in range(3)]
    messages[0].attachments = [Attachment("1", "private-attachment", "private-file", "text/plain",
                                          len(ATTACHMENT))]
    pages = [SearchResult([message], 3, f"private-cursor-{index}", True, False, [])
             for index, message in enumerate(messages)]
    service = SimpleNamespace(
        messages=messages, pages=pages,
        search_emails=AsyncMock(side_effect=pages + [pages[0]] * 5),
        get_email=AsyncMock(side_effect=messages),
        get_thread=AsyncMock(return_value=ThreadResult(
            "private-thread", messages, [message.message_id for message in messages], 0, False,
        )),
        get_attachment=AsyncMock(return_value=BinaryContent(
            messages[0].message_id, ATTACHMENT, "private-attachment",
        )),
    )
    return service


async def validate(service: SimpleNamespace) -> gmail_validation.ValidationReport:
    return await gmail_validation.validate_fixture(
        service, "private-user", "private-query", MARKER, ATTACHMENT_HASH,
    )


async def test_success_paginates_exactly_and_records_five_search_timings(fixture_service) -> None:
    report = await validate(fixture_service)
    assert report.success and report.failure is None
    assert len(report.search_seconds) == 5
    assert all(duration >= 0 for duration in report.search_seconds)
    assert all(report.checks.values()) and len(report.checks) == 5
    calls = fixture_service.search_emails.await_args_list
    assert len(calls) == 8
    assert [call.kwargs.get("cursor") for call in calls[:3]] == [
        None, "private-cursor-0", "private-cursor-1",
    ]
    assert all(call.args == ("private-user", "private-query") for call in calls)
    assert all(call.kwargs["page_size"] == 1 for call in calls)
    assert all("cursor" not in call.kwargs for call in calls[3:])
    serialized = json.dumps(asdict(report))
    assert "private-" not in serialized and ATTACHMENT_HASH not in serialized


@pytest.mark.parametrize("mutation", [
    "no_continuation", "repeated_cursor", "duplicate_id", "empty_page", "oversized_page",
    "metadata_error", "truncated_preview",
])
async def test_invalid_pagination_never_claims_success(fixture_service, mutation: str) -> None:
    first, second, _ = fixture_service.pages
    if mutation == "no_continuation":
        first.has_more = False
    elif mutation == "repeated_cursor":
        second.cursor = first.cursor
    elif mutation == "duplicate_id":
        second.messages[0].message_id = first.messages[0].message_id
    elif mutation == "empty_page":
        first.messages = []
    elif mutation == "oversized_page":
        first.messages = first.messages * 2
    elif mutation == "metadata_error":
        first.errors = [ItemError("private-id", "private-error")]
    else:
        first.messages[0].truncated = True
    report = await validate(fixture_service)
    assert not report.success and report.failure == "pagination_failed"
    assert not report.search_seconds


@pytest.mark.parametrize("mutation", ["marker", "body_error", "body_truncation", "wrong_id"])
async def test_invalid_message_content(fixture_service, mutation: str) -> None:
    message = EmailContent("private-id-0", "private-thread", body=MARKER)
    if mutation == "marker":
        message.body = "missing"
    elif mutation == "body_error":
        message.errors = [ItemError("private-id", "private-error")]
    elif mutation == "body_truncation":
        message.truncated = True
    else:
        message.message_id = "other"
    fixture_service.get_email.side_effect = None
    fixture_service.get_email.return_value = message
    report = await validate(fixture_service)
    assert not report.success and report.failure == "message_failed"


@pytest.mark.parametrize("mutation", ["marker", "errors", "truncated", "missing_message"])
async def test_invalid_thread_content(fixture_service, mutation: str) -> None:
    thread = fixture_service.get_thread.return_value
    if mutation == "marker":
        thread.messages = [EmailContent("private-id-0", "private-thread", body="missing")]
    elif mutation == "errors":
        thread.errors = [ItemError("private-id", "private-error")]
    elif mutation == "truncated":
        thread.truncated = True
    else:
        thread.message_ids = []
    report = await validate(fixture_service)
    assert not report.success and report.failure == "thread_failed"


@pytest.mark.parametrize("mutation", ["wrong_hash", "no_attachment", "too_many", "wrong_identity"])
async def test_attachment_must_be_exact_and_bounded(fixture_service, mutation: str) -> None:
    if mutation == "wrong_hash":
        fixture_service.get_attachment.return_value.data = b"wrong"
    elif mutation == "no_attachment":
        fixture_service.messages[0].attachments = []
    elif mutation == "too_many":
        fixture_service.messages[0].attachments *= 11
    else:
        fixture_service.get_attachment.return_value.message_id = "wrong"
    report = await validate(fixture_service)
    assert not report.success and report.failure == "attachment_failed"
    if mutation in {"no_attachment", "too_many"}:
        fixture_service.get_attachment.assert_not_awaited()


async def test_timing_search_partial_failure_keeps_only_completed_samples(fixture_service) -> None:
    fixture_service.search_emails.side_effect = (
        fixture_service.pages + [fixture_service.pages[0],
                                 RuntimeError("private-token private-email private-body")]
    )
    report = await validate(fixture_service)
    assert not report.success and report.failure == "timing_search_failed"
    assert len(report.search_seconds) == 1
    assert "private-" not in json.dumps(asdict(report))


@pytest.mark.parametrize("marker,hash_value", [(" ", ATTACHMENT_HASH), (MARKER, "not-a-hash")])
async def test_malformed_arguments_do_not_search(fixture_service, marker, hash_value) -> None:
    report = await gmail_validation.validate_fixture(
        fixture_service, "private-user", "private-query", marker, hash_value,
    )
    assert report.failure == "invalid_fixture"
    fixture_service.search_emails.assert_not_awaited()


@pytest.fixture
def dependencies(monkeypatch) -> SimpleNamespace:
    """Replace app services at their boundaries to check ownership and cleanup."""
    settings = SimpleNamespace(database_url="private-db", credential_encryption_key="private-key")
    engine = SimpleNamespace(dispose=AsyncMock())
    store = SimpleNamespace(user=AsyncMock(return_value=object()))
    service = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(gmail_validation, "get_settings", lambda: settings)
    monkeypatch.setattr(gmail_validation, "make_async_engine", lambda url: engine)
    monkeypatch.setattr(gmail_validation, "make_async_session_factory", lambda engine: object())
    monkeypatch.setattr(gmail_validation, "WebStore", lambda *args, **kwargs: store)
    monkeypatch.setattr(gmail_validation, "GmailService", lambda store: service)
    validator = AsyncMock(return_value=gmail_validation.ValidationReport(success=True))
    monkeypatch.setattr(gmail_validation, "validate_fixture", validator)
    return SimpleNamespace(engine=engine, store=store, service=service, validator=validator)


async def test_lifecycle_uses_selected_user_and_cleans_services(dependencies) -> None:
    report = await gmail_validation.run_validation(
        "selected-user", "query", MARKER, ATTACHMENT_HASH,
    )
    assert report.success
    dependencies.store.user.assert_awaited_once_with("selected-user")
    dependencies.validator.assert_awaited_once_with(
        dependencies.service, "selected-user", "query", MARKER, ATTACHMENT_HASH,
    )
    dependencies.service.aclose.assert_awaited_once()
    dependencies.engine.dispose.assert_awaited_once()


async def test_unknown_user_never_reads_mail(dependencies) -> None:
    dependencies.store.user.return_value = None
    report = await gmail_validation.run_validation("missing", "query", MARKER, ATTACHMENT_HASH)
    assert report.failure == "selected_user_unavailable"
    dependencies.validator.assert_not_awaited()
    dependencies.service.aclose.assert_not_awaited()
    dependencies.engine.dispose.assert_awaited_once()


async def test_cleanup_failure_still_disposes_database(dependencies) -> None:
    dependencies.service.aclose.side_effect = RuntimeError("private-credential")
    report = await gmail_validation.run_validation("selected", "query", MARKER, ATTACHMENT_HASH)
    assert not report.success and report.failure == "cleanup_failed"
    dependencies.engine.dispose.assert_awaited_once()


async def test_service_failure_still_closes_resources(dependencies) -> None:
    dependencies.validator.side_effect = RuntimeError("private-query private-token")
    report = await gmail_validation.run_validation("selected", "query", MARKER, ATTACHMENT_HASH)
    assert not report.success and "private-" not in json.dumps(asdict(report))
    dependencies.service.aclose.assert_awaited_once()
    dependencies.engine.dispose.assert_awaited_once()


def test_cli_prints_and_writes_only_safe_report(monkeypatch, tmp_path: Path, capsys) -> None:
    report = gmail_validation.ValidationReport(success=True, checks={"safe_check": True})
    monkeypatch.setattr(gmail_validation, "run_validation", AsyncMock(return_value=report))
    destination = tmp_path / "report.json"
    exit_code = gmail_validation.main([
        "--user-id", "private-user", "--query", "private-query",
        "--expected-marker", MARKER, "--attachment-sha256", ATTACHMENT_HASH,
        "--report", str(destination),
    ])
    assert exit_code == 0
    output = capsys.readouterr()
    assert not output.err and "private-" not in output.out
    assert json.loads(output.out) == json.loads(destination.read_text())
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [destination]


def test_failed_report_write_is_generic(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(gmail_validation, "run_validation", AsyncMock(
        return_value=gmail_validation.ValidationReport(success=True),
    ))
    monkeypatch.setattr(gmail_validation, "write_report", lambda *args: (_ for _ in ()).throw(
        OSError("private-user private-query"),
    ))
    exit_code = gmail_validation.main([
        "--user-id", "selected", "--query", "fixture", "--expected-marker", MARKER,
        "--attachment-sha256", ATTACHMENT_HASH, "--report", str(tmp_path / "report.json"),
    ])
    assert exit_code == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["failure"] == "report_write_failed"
    assert "private-" not in output.out and not output.err


def test_invalid_cli_arguments_do_not_echo_fixture_values(capsys) -> None:
    with pytest.raises(SystemExit) as failure:
        gmail_validation.main(["--unknown", "private-mailbox-query"])
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert "private-mailbox-query" not in output.err
    assert "Invalid validation arguments" in output.err


async def test_smoke_three_pages_preserves_safe_metadata(fixture_service) -> None:
    report = await gmail_validation.validate_smoke(fixture_service, "private-user", "private-query")
    assert report.success and report.validation_mode == "live_smoke"
    assert report.data_source == "live_google" and report.recorded_at
    assert report.attachments_covered and len(report.search_seconds) == 5
    assert set(report.checks) == {
        "bounded_distinct_message_consistency", "thread_identity_consistency",
        "attachment_identity_and_byte_count_consistency", "five_sequential_live_searches",
    }
    assert fixture_service.search_emails.await_count == 8
    fixture_service.get_attachment.assert_awaited_once_with(
        "private-user", "private-id-0", "private-attachment",
    )
    serialized = json.dumps(asdict(report))
    assert "private-" not in serialized and "sha256" not in serialized and "marker" not in serialized


async def test_smoke_single_page_without_attachments_is_valid_but_uncovered(fixture_service) -> None:
    page = fixture_service.pages[0]
    page.has_more = False
    fixture_service.search_emails.side_effect = [page] * 6
    fixture_service.messages[0].attachments = []
    report = await gmail_validation.validate_smoke(fixture_service, "selected", "query")
    assert report.success and not report.attachments_covered
    assert fixture_service.search_emails.await_count == 6
    fixture_service.get_email.assert_awaited_once()
    fixture_service.get_attachment.assert_not_awaited()
    assert "attachment_identity_and_byte_count_consistency" not in report.checks


async def test_smoke_downloads_at_most_one_inventoried_attachment(fixture_service) -> None:
    fixture_service.messages[0].attachments *= 12
    report = await gmail_validation.validate_smoke(fixture_service, "selected", "query")
    assert report.success
    fixture_service.get_attachment.assert_awaited_once()


@pytest.mark.parametrize("mutation,failure_category", [
    ("empty", "pagination_failed"), ("duplicate", "pagination_failed"),
    ("repeated_cursor", "pagination_failed"), ("missing_cursor", "pagination_failed"),
    ("partial", "pagination_failed"), ("wrong_thread", "message_failed"),
    ("missing_thread_message", "thread_failed"), ("wrong_attachment_size", "attachment_failed"),
])
async def test_smoke_rejects_inconsistent_results(
    fixture_service, mutation: str, failure_category: str,
) -> None:
    if mutation == "empty":
        fixture_service.pages[0].messages = []
    elif mutation == "duplicate":
        fixture_service.pages[1].messages = fixture_service.pages[0].messages
    elif mutation == "repeated_cursor":
        fixture_service.pages[1].cursor = fixture_service.pages[0].cursor
    elif mutation == "missing_cursor":
        fixture_service.pages[0].cursor = None
    elif mutation == "partial":
        fixture_service.pages[0].metadata_incomplete = True
    elif mutation == "wrong_thread":
        fixture_service.get_email.side_effect = None
        fixture_service.get_email.return_value = EmailContent("private-id-0", "wrong-thread")
    elif mutation == "missing_thread_message":
        fixture_service.get_thread.return_value.messages = [fixture_service.messages[1]]
    else:
        fixture_service.get_attachment.return_value.data = b"wrong-size"
    report = await gmail_validation.validate_smoke(fixture_service, "selected", "query")
    assert not report.success and report.failure == failure_category
    assert report.validation_mode == "live_smoke"


async def test_smoke_lifecycle_dispatches_selected_user(dependencies, monkeypatch) -> None:
    smoke_validator = AsyncMock(return_value=gmail_validation.ValidationReport(
        success=True, validation_mode="live_smoke",
    ))
    monkeypatch.setattr(gmail_validation, "validate_smoke", smoke_validator)
    report = await gmail_validation.run_validation("selected-user", "selected-query", smoke=True)
    assert report.success and report.validation_mode == "live_smoke"
    smoke_validator.assert_awaited_once_with(dependencies.service, "selected-user", "selected-query")
    dependencies.validator.assert_not_awaited()
    dependencies.service.aclose.assert_awaited_once()
    dependencies.engine.dispose.assert_awaited_once()


def test_smoke_cli_dispatch(monkeypatch, capsys) -> None:
    runner = AsyncMock(return_value=gmail_validation.ValidationReport(
        success=True, validation_mode="live_smoke",
    ))
    monkeypatch.setattr(gmail_validation, "run_validation", runner)
    assert gmail_validation.main([
        "--smoke", "--user-id", "selected", "--query", "private-query",
    ]) == 0
    runner.assert_awaited_once_with("selected", "private-query", smoke=True)
    output = capsys.readouterr()
    assert json.loads(output.out)["validation_mode"] == "live_smoke"
    assert "private-query" not in output.out


@pytest.mark.parametrize("options", [
    ["--smoke", "--expected-marker", "private-marker"],
    ["--smoke", "--attachment-sha256", "private-hash"],
    [], ["--expected-marker", "private-marker"],
])
def test_cli_requires_unambiguous_validation_mode(options: list[str], capsys) -> None:
    with pytest.raises(SystemExit) as failure:
        gmail_validation.main(["--user-id", "selected", "--query", "query", *options])
    assert failure.value.code == 2
    assert "private-" not in capsys.readouterr().err
