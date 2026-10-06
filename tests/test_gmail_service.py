import asyncio
import base64
import json
import threading
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httplib2
import pytest
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

from assistant_agent.async_workers import AsyncWorker
from assistant_agent.gmail_service import (
    BinaryContent,
    GmailError,
    GmailLimits,
    GmailService,
    _size,
)


def encoded(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def part(data=b"hello", mime="text/plain", **extra):
    return {
        "partId": "0",
        "mimeType": mime,
        "body": {"data": encoded(data), "size": len(data)},
        **extra,
    }


def message(message_id="m", payload=None):
    payload = payload or part()
    payload.setdefault("headers", [{"name": "Subject", "value": "Subject"}])
    return {
        "id": message_id,
        "threadId": "t",
        "snippet": "hello &amp; goodbye",
        "internalDate": "1",
        "payload": payload,
    }


def error(status, reason="backendError", retry_after=None):
    response = {"status": str(status)}
    if retry_after is not None:
        response["retry-after"] = str(retry_after)
    return HttpError(
        httplib2.Response(response),
        json.dumps({"error": {"errors": [{"reason": reason}], "message": "PRIVATE BODY"}}).encode(),
    )


class FakeClock:
    def __init__(self):
        self.now = 0
        self.delays = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def service(request, *, limits=None, clock=None, capacity=4):
    store = SimpleNamespace(
        google_worker=AsyncWorker(capacity),
        load_refreshed=AsyncMock(return_value=Credentials(token="private-token")),
    )
    options = {"clock": clock, "sleep": clock.sleep} if clock else {}
    return GmailService(store, request=request, limits=limits, jitter=lambda: 0, **options)


async def test_search_one_page_order_partial_failure_and_cursor():
    calls = []

    def request(creds, method, params, timeout):
        calls.append((method, params, timeout))
        if method == "list":
            if params.get("pageToken") == "next":
                return {}
            return {
                "messages": [{"id": "a", "threadId": "t"}, {"id": "b", "threadId": "t"}],
                "nextPageToken": "next",
                "resultSizeEstimate": 99,
            }
        if params["id"] == "a":
            raise error(404, "notFound")
        return message("b")

    gmail = service(request)
    result = await gmail.search_emails("user", "from:someone")
    assert [m.message_id for m in result.messages] == ["a", "b"]
    assert result.messages[1].headers == {"subject": "Subject"}
    assert result.messages[1].snippet == "hello & goodbye"
    assert result.has_more and result.metadata_incomplete and result.estimated_total == 99
    assert [(e.item_id, e.code) for e in result.errors] == [("a", "not_found")]
    assert sum(method == "list" for method, _, _ in calls) == 1
    assert all("PRIVATE" not in str(asdict(e)) for e in result.errors)
    page = await gmail.search_emails("user", "from:someone", cursor=result.cursor)
    assert page.messages == [] and not page.has_more
    for user, query, cursor in [
        ("other", "from:someone", result.cursor),
        ("user", "other", result.cursor),
        ("user", "from:someone", "invalid"),
        ("user", "from:someone", result.cursor + "x"),
    ]:
        with pytest.raises(GmailError, match="invalid_cursor"):
            await gmail.search_emails(user, query, cursor=cursor)
    assert [params for method, params, _ in calls if method == "message"][0]["metadataHeaders"] == [
        "From",
        "To",
        "Subject",
        "Date",
    ]
    await gmail.aclose()


@pytest.mark.parametrize("page_size", [0, 51, True, 1.5])
async def test_invalid_search_sizes(page_size):
    gmail = service(lambda *args: {})
    with pytest.raises(GmailError, match="invalid_arguments"):
        await gmail.search_emails("u", "", page_size)
    gmail.store.load_refreshed.assert_not_called()


async def test_nested_mime_external_body_charset_and_attachments():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    part(b"Stub"),
                    part(
                        b"<style>hidden</style><script>hidden</script><p>A much longer complete message.</p>",
                        "text/html",
                    ),
                ],
            },
            {
                "partId": "external",
                "mimeType": "text/plain",
                "headers": [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
                "body": {"attachmentId": "body", "size": 4},
            },
            {
                "partId": "file",
                "filename": "file.txt",
                "mimeType": "text/plain",
                "body": {"attachmentId": "file", "size": 999},
            },
            part(
                b"not body",
                "image/png",
                headers=[{"name": "Content-Disposition", "value": "inline"}],
            ),
        ],
    }
    calls = []

    def request(creds, method, params, timeout):
        calls.append((method, params))
        if method == "attachment":
            assert params == {"messageId": "m", "id": "body"}
            return {"data": encoded(b"caf\xe9")}
        return message(payload=payload)

    gmail = service(request)
    content = await gmail.get_email("u", "m")
    assert content.body_source == "html"
    assert content.body == "A much longer complete message."
    assert [a.attachment_id for a in content.attachments] == ["file", None]
    assert content.attachments[1].inline
    assert not content.truncated and content.errors == []
    assert len(calls) == 2
    # The external part is correctly decoded when it is the only body.
    payload["parts"] = [payload["parts"][1]]
    assert (await gmail.get_email("u", "m")).body == "café"
    await gmail.aclose()


@pytest.mark.parametrize(
    "payload,code",
    [
        (
            part(headers=[{"name": "Content-Type", "value": "text/plain; charset=nonexistent"}]),
            "lossy_decoding",
        ),
        (part(b"\xff"), "lossy_decoding"),
        ({"mimeType": "text/plain", "body": {"data": "%%%"}}, "malformed_content"),
    ],
)
async def test_decode_failures_are_explicit(payload, code):
    gmail = service(lambda *args: message(payload=payload))
    result = await gmail.get_email("u", "m")
    assert code in [e.code for e in result.errors]
    if code == "malformed_content":
        assert result.truncated and result.body == ""


async def test_body_external_failure_and_mime_limits():
    def request(creds, method, params, timeout):
        if method == "attachment":
            raise error(404)
        return message(
            payload={
                "mimeType": "multipart/mixed",
                "parts": [part(), {"mimeType": "text/plain", "body": {"attachmentId": "missing"}}],
            }
        )

    gmail = service(request)
    result = await gmail.get_email("u", "m")
    assert result.body == "hello" and result.truncated
    assert result.errors[0].code == "not_found"
    gmail.limits = replace(gmail.limits, mime_parts=1)
    assert "mime_limit" in [e.code for e in (await gmail.get_email("u", "m")).errors]
    gmail.limits = replace(gmail.limits, mime_parts=100, mime_depth=1)
    gmail._request = lambda *args: message(payload={"parts": [{"parts": [part()]}]})
    assert (await gmail.get_email("u", "m")).truncated


async def test_binary_exact_size_limits_and_no_caching(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []
    raw = b"From: sender\r\n\r\n\x00\xff\r\n"

    def request(creds, method, params, timeout):
        calls.append((method, params))
        return {"raw": encoded(raw), "data": encoded(raw), "threadId": "t", "size": len(raw)}

    gmail = service(request)
    for _ in range(2):
        result = await gmail.get_email("u", "m", format="raw")
        assert isinstance(result, BinaryContent) and result.data == raw and result.thread_id == "t"
    attachment = await gmail.get_attachment("u", "m", "a")
    assert attachment.data == raw and attachment.attachment_id == "a"
    assert len(calls) == 3 and list(tmp_path.iterdir()) == []
    gmail.limits = replace(gmail.limits, binary_bytes=2)
    with pytest.raises(GmailError, match="size_limit"):
        await gmail.get_email("u", "m", "raw")
    with pytest.raises(GmailError, match="size_limit"):
        await gmail.get_attachment("u", "m", "a")


async def test_thread_and_utf8_response_truncation():
    gmail = service(
        lambda *args: {"messages": [message(str(i), part("é".encode() * 20)) for i in range(4)]},
        limits=GmailLimits(thread_messages=2, body_bytes=5),
    )
    result = await gmail.get_thread("u", "t")
    assert [m.body for m in result.messages] == ["éé", "éé"]
    assert result.message_ids == ["0", "1", "2", "3"]
    assert result.omitted_messages == 2 and result.truncated
    gmail.limits = replace(gmail.limits, response_bytes=1024, thread_messages=50)
    gmail._request = lambda *args: {
        "messages": [message(str(i), part(b"x" * 500)) for i in range(20)]
    }
    result = await gmail.get_thread("u", "t")
    assert _size(result) <= 1024 and result.omitted_messages > 0
    gmail._request = lambda *args: {"messages": [message(str(i)) for i in range(1000)]}
    result = await gmail.get_thread("u", "t")
    assert result.ids_truncated and result.omitted_messages == 1000
    assert _size(result) <= 1024


@pytest.mark.parametrize(
    "status,reason,code",
    [
        (401, "authError", "reconnect_required"),
        (403, "forbidden", "permission_denied"),
        (404, "notFound", "not_found"),
    ],
)
async def test_permanent_errors_are_not_retried(status, reason, code):
    calls = []

    def request(*args):
        calls.append(1)
        raise error(status, reason)

    gmail = service(request)
    with pytest.raises(GmailError, match=code) as raised:
        await gmail.get_email("u", "m")
    assert not raised.value.retryable and calls == [1]
    assert "PRIVATE" not in str(raised.value)


async def test_missing_revoked_credentials_and_refresh_serialization():
    gmail = service(lambda *args: message())
    gmail.store.load_refreshed.return_value = None
    with pytest.raises(GmailError, match="reconnect_required"):
        await gmail.get_email("u", "m")
    active = maximum = 0

    async def load(user):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        await asyncio.sleep(0.01)
        active -= 1
        return Credentials(token=user)

    gmail.store.load_refreshed.side_effect = load
    await asyncio.gather(*(gmail.get_email("u", "m") for _ in range(4)))
    assert maximum == 1


@pytest.mark.parametrize(
    "status,reason",
    [(429, "userRateLimitExceeded"), (403, "rateLimitExceeded"), (503, "backendError")],
)
async def test_retries_cooldowns_and_retry_after(status, reason):
    clock = FakeClock()
    calls = []

    def request(*args):
        calls.append(clock())
        if len(calls) < 3:
            raise error(status, reason, 3)
        return message()

    gmail = service(request, clock=clock)
    assert (await gmail.get_email("u", "m")).body == "hello"
    assert calls == [0, 3, 6]
    assert clock.delays == [3, 3]
    bucket = gmail._app_bucket if reason == "rateLimitExceeded" else gmail._user("u").bucket
    if status != 503:
        assert bucket.cooldown == 6


async def test_retry_exhaustion_and_oversized_retry_after():
    clock = FakeClock()
    calls = []

    def request(*args):
        calls.append(1)
        raise error(503)

    gmail = service(request, clock=clock)
    with pytest.raises(GmailError, match="upstream_error"):
        await gmail.get_email("u", "m")
    assert len(calls) == 3 and clock.delays == [1, 2]
    gmail._request = lambda *args: (_ for _ in ()).throw(error(429, retry_after=60))
    with pytest.raises(GmailError, match="deadline_exceeded"):
        await gmail.get_email("u", "m")


async def test_weighted_budgets_are_shared_and_independent_of_concurrency():
    clock = FakeClock()
    calls = []

    def request(creds, method, params, timeout):
        calls.append(clock())
        return message()

    gmail = service(
        request, clock=clock, limits=GmailLimits(user_units_per_second=40, app_units_per_second=40)
    )
    for user in ["u", "u", "other"]:
        await gmail.get_email(user, "m")
    assert calls == [0, 0, 0.5]
    await gmail.get_email("u", "m")
    assert calls[-1] == 1


async def test_search_deadline_drains_calls_and_reports_unscheduled_ids():
    clock = FakeClock()
    calls = []

    def request(creds, method, params, timeout):
        calls.append((method, params, timeout))
        if method == "list":
            return {"messages": [{"id": str(i)} for i in range(10)]}
        clock.now = 16
        return message(params["id"])

    gmail = service(request, clock=clock, capacity=1, limits=GmailLimits(user_concurrency=1))
    result = await gmail.search_emails("u", "")
    assert len(result.messages) == 10 and len(result.errors) == 9
    assert all(e.code == "deadline_exceeded" for e in result.errors)
    assert len(calls) == 2 and gmail.worker.limiter.borrowed_tokens == 0
    assert not gmail._app_semaphore.locked()


async def test_concurrency_cancellation_and_shutdown_keep_capacity_until_drain():
    release = threading.Event()
    started = threading.Event()
    active = maximum = 0
    lock = threading.Lock()

    def request(*args):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        started.set()
        assert release.wait(3)
        with lock:
            active -= 1
        return message()

    gmail = service(request, capacity=4, limits=GmailLimits(user_concurrency=1, app_concurrency=2))
    first = asyncio.create_task(gmail.get_email("u", "m"))
    while not started.is_set():
        await asyncio.sleep(0.001)
    first.cancel()
    first.cancel()
    second = asyncio.create_task(gmail.get_email("u", "m"))
    other = asyncio.create_task(gmail.get_email("other", "m"))
    await asyncio.sleep(0.02)
    closing = asyncio.create_task(gmail.aclose())
    await asyncio.sleep(0.01)
    assert not first.done() and not closing.done()
    assert maximum == 2 and gmail.worker.limiter.borrowed_tokens == 2
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.gather(second, other, closing)
    assert gmail.worker.limiter.borrowed_tokens == 0
    with pytest.raises(GmailError, match="service_closed"):
        await gmail.get_email("u", "m")


async def test_sdk_uses_me_thread_owned_transports_and_no_auth_retry(monkeypatch):
    from assistant_agent import gmail_service as module

    records = []

    class Resource:
        def users(self):
            return self

        messages = threads = attachments = users

        def get(self, **params):
            records.append(params)
            return self

        list = get

        def execute(self, **params):
            assert params == {"num_retries": 0}
            return message()

    transports = []

    def build(*args, **kwargs):
        auth = kwargs["http"]
        assert auth._max_refresh_attempts == 0
        assert auth.credentials.refresh_token is None
        assert auth.credentials.expiry is None
        transports.append(auth.http)
        return Resource()

    monkeypatch.setattr(module, "build", build)
    store = SimpleNamespace(
        google_worker=AsyncWorker(1),
        load_refreshed=AsyncMock(
            return_value=Credentials(token="private-token", refresh_token="private-refresh")
        ),
    )
    gmail = GmailService(store)
    await gmail.get_email("u", "m")
    await gmail.get_email("u", "m")
    assert transports[0] is transports[1]
    assert all(p["userId"] == "me" for p in records)
    await gmail.aclose()


async def test_shared_cooldown_delays_other_calls_and_charges_retries():
    clock = FakeClock()
    calls = []

    def request(creds, method, params, timeout):
        calls.append((creds.token, clock()))
        if len(calls) == 1:
            raise error(403, "rateLimitExceeded", 60)
        return message()

    gmail = service(request, clock=clock)
    with pytest.raises(GmailError, match="deadline_exceeded"):
        await gmail.get_email("u", "m")
    assert gmail._app_bucket.tokens == 380
    assert gmail._user("u").bucket.tokens == 60
    with pytest.raises(GmailError, match="deadline_exceeded"):
        await gmail.get_email("other", "m")
    assert len(calls) == 1
    clock.now = 60
    assert (await gmail.get_email("other", "m")).body == "hello"


async def test_network_retry_and_failure_release_worker_capacity():
    clock = FakeClock()
    calls = []

    def request(*args):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("private network details")
        return message()

    gmail = service(request, clock=clock)
    assert (await gmail.get_email("u", "m")).body == "hello"
    assert clock.delays == [1] and gmail.worker.limiter.borrowed_tokens == 0
    gmail._request = lambda *args: (_ for _ in ()).throw(error(401))
    with pytest.raises(GmailError, match="reconnect_required"):
        await gmail.get_email("u", "m")
    gmail._request = lambda *args: message()
    assert (await gmail.get_email("u", "m")).body == "hello"


async def test_list_failure_and_refresh_deadline_are_top_level_errors():
    clock = FakeClock()
    gmail = service(lambda *args: (_ for _ in ()).throw(error(404)), clock=clock)
    with pytest.raises(GmailError, match="not_found"):
        await gmail.search_emails("u", "")

    async def slow_credentials(user):
        clock.now += 16
        return Credentials(token="token")

    gmail.store.load_refreshed.side_effect = slow_credentials
    with pytest.raises(GmailError, match="deadline_exceeded"):
        await gmail.search_emails("u", "")
    assert not gmail._user("u").refresh_lock.locked()


async def test_search_preview_response_size_limit_preserves_ids():
    def request(creds, method, params, timeout):
        if method == "list":
            return {"messages": [{"id": str(i)} for i in range(2)]}
        return message(params["id"], part(headers=[{"name": "Subject", "value": "x" * 9000}]))

    gmail = service(request, limits=GmailLimits(response_bytes=1024))
    result = await gmail.search_emails("u", "")
    assert _size(result) <= 1024 and result.truncated
    assert [m.message_id for m in result.messages] == ["0", "1"]
    assert not result.metadata_incomplete


async def test_normalized_message_metadata_limits_and_combined_body_budget():
    payload = {
        "mimeType": "multipart/mixed",
        "headers": [{"name": "Cc", "value": "other@example.com"}],
        "parts": [part(b"123"), part(b"456")],
    }
    gmail = service(lambda *args: message(payload=payload), limits=GmailLimits(binary_bytes=5))
    result = await gmail.get_email("u", "m")
    assert result.body == "123" and result.truncated
    assert result.headers["cc"] == "other@example.com"
    assert result.errors[0].code == "size_limit"
    gmail.limits = GmailLimits(response_bytes=1024)
    payload["parts"] = [part(filename="x" * 4096) for _ in range(20)]
    result = await gmail.get_email("u", "m")
    assert result.truncated and _size(result) <= 1024


async def test_mime_parsing_runs_off_event_loop(monkeypatch):
    from assistant_agent import gmail_service as module

    main_thread = threading.get_ident()
    original = module.extract_body
    seen = []

    def extract(payload):
        seen.append(threading.get_ident())
        return original(payload)

    monkeypatch.setattr(module, "extract_body", extract)
    gmail = service(lambda *args: message())
    assert (await gmail.get_email("u", "m")).body == "hello"
    assert seen and main_thread not in seen


async def test_sdk_transport_is_not_shared_between_worker_threads(monkeypatch):
    from assistant_agent import gmail_service as module

    barrier = threading.Barrier(2)
    transports = []

    class Resource:
        def users(self):
            return self

        messages = users

        def get(self, **kwargs):
            assert kwargs["userId"] == "me"
            barrier.wait(timeout=3)
            return self

        def execute(self, **kwargs):
            return message()

    def build(*args, **kwargs):
        transports.append(kwargs["http"].http)
        return Resource()

    monkeypatch.setattr(module, "build", build)
    store = SimpleNamespace(
        google_worker=AsyncWorker(2),
        load_refreshed=AsyncMock(return_value=Credentials(token="token")),
    )
    gmail = GmailService(store)
    await asyncio.gather(gmail.get_email("u", "m"), gmail.get_email("other", "m"))
    assert len(transports) == 2 and transports[0] is not transports[1]
    await gmail.aclose()


@pytest.mark.parametrize(
    "changes",
    [
        {"attempts": 1.5},
        {"search_seconds": float("inf")},
        {"app_units_per_second": 39},
        {"mime_depth": 0},
    ],
)
def test_invalid_limits(changes):
    with pytest.raises(ValueError):
        GmailLimits(**changes)


async def test_attachment_subtrees_are_not_read_or_fetched():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            part(b"BODY"),
            {
                "mimeType": "message/rfc822",
                "filename": "forward.eml",
                "parts": [
                    part(b"ATTACHED CONTENT"),
                    {"mimeType": "text/plain", "body": {"attachmentId": "must-not-fetch"}},
                ],
            },
            {
                "mimeType": "multipart/mixed",
                "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                "parts": [part(b"ALSO ATTACHED")],
            },
        ],
    }

    def request(creds, method, params, timeout):
        assert method == "message"
        return message(payload=payload)

    result = await service(request).get_email("u", "m")
    assert result.body == "BODY" and len(result.attachments) == 2


async def test_inline_text_is_a_body_not_an_attachment():
    result = await service(
        lambda *args: message(
            payload=part(
                b"BODY",
                headers=[{"name": "Content-Disposition", "value": "inline"}],
            )
        )
    ).get_email("u", "m")
    assert result.body == "BODY" and result.attachments == []


async def test_cancelled_search_only_drains_active_previews():
    started = threading.Event()
    release = threading.Event()
    previews = []

    def request(creds, method, params, timeout):
        if method == "list":
            return {"messages": [{"id": str(i)} for i in range(10)]}
        previews.append(params["id"])
        started.set()
        assert release.wait(3)
        return message(params["id"])

    gmail = service(request, capacity=1, limits=GmailLimits(app_concurrency=1))
    task = asyncio.create_task(gmail.search_emails("u", ""))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done() and gmail.worker.limiter.borrowed_tokens == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert previews == ["0"] and gmail.worker.limiter.borrowed_tokens == 0
    await gmail.aclose()


async def test_revoked_refresh_error_is_sanitized_and_credentials_are_per_user():
    from google.auth.exceptions import RefreshError

    seen = []

    def request(creds, method, params, timeout):
        seen.append(creds.token)
        return message()

    gmail = service(request)
    gmail.store.load_refreshed.side_effect = RefreshError("private refresh details")
    with pytest.raises(GmailError, match="reconnect_required") as caught:
        await gmail.get_email("u", "m")
    assert str(caught.value) == "reconnect_required" and seen == []
    gmail.store.load_refreshed.side_effect = lambda user: Credentials(token=user)
    await asyncio.gather(gmail.get_email("first", "m"), gmail.get_email("second", "m"))
    assert sorted(seen) == ["first", "second"]


async def test_anyio_scope_cancellation_drains_request():
    import anyio

    started = threading.Event()
    release = threading.Event()

    def request(*args):
        started.set()
        assert release.wait(3)
        return message()

    gmail = service(request)
    scope = anyio.CancelScope()

    async def run():
        with scope:
            await gmail.get_email("u", "m")

    task = asyncio.create_task(run())
    while not started.is_set():
        await asyncio.sleep(0.001)
    scope.cancel()
    await asyncio.sleep(0.01)
    assert not task.done() and gmail.worker.limiter.borrowed_tokens == 1
    release.set()
    await asyncio.wait_for(task, 1)
    assert gmail.worker.limiter.borrowed_tokens == 0
    await gmail.aclose()


async def test_user_cooldown_does_not_hold_app_capacity():
    clock = FakeClock()
    sleeping = asyncio.Event()
    release = asyncio.Event()
    users = []

    def request(creds, method, params, timeout):
        users.append(creds.token)
        return message()

    gmail = service(request, clock=clock, limits=GmailLimits(app_concurrency=1))
    gmail.store.load_refreshed.side_effect = lambda user: Credentials(token=user)

    async def sleep(seconds):
        sleeping.set()
        await release.wait()
        clock.now += seconds

    gmail.sleep = sleep
    gmail._user("limited").bucket.cooldown = 1
    limited = asyncio.create_task(gmail.get_email("limited", "m"))
    await asyncio.wait_for(sleeping.wait(), 1)
    assert (await asyncio.wait_for(gmail.get_email("other", "m"), 1)).body == "hello"
    assert users == ["other"] and not limited.done()
    release.set()
    await limited
    assert users == ["other", "limited"]
    await gmail.aclose()


async def test_real_sdk_request_shapes_and_authentication(monkeypatch, caplog):
    from urllib.parse import parse_qs, urlparse
    from assistant_agent import gmail_service as module

    calls = []
    closed = []

    class HTTP:
        def __init__(self, timeout):
            self.timeout = timeout
            self.connections = {}

        def request(self, uri, method="GET", body=None, headers=None, **kwargs):
            url = urlparse(uri)
            calls.append((url.path, parse_qs(url.query), headers["authorization"], method))
            if url.path.endswith("/attachments/a"):
                result = {"data": encoded(b"attachment"), "size": 10}
            elif url.path.endswith("/threads/t"):
                result = {"messages": [message()]}
            elif url.path.endswith("/messages"):
                result = {"messages": [{"id": "m", "threadId": "t"}]}
            elif parse_qs(url.query).get("format") == ["raw"]:
                result = {"raw": encoded(b"raw"), "threadId": "t"}
            else:
                result = message()
            return httplib2.Response({"status": "200"}), json.dumps(result).encode()

        def close(self):
            closed.append(self)

    monkeypatch.setattr(module.httplib2, "Http", HTTP)
    store = SimpleNamespace(
        google_worker=AsyncWorker(1),
        load_refreshed=AsyncMock(side_effect=lambda user: Credentials(token="secret-" + user)),
    )
    gmail = GmailService(store)
    assert len((await gmail.search_emails("first", "from:sender", 1)).messages) == 1
    assert (await gmail.get_email("second", "m")).body == "hello"
    assert (await gmail.get_email("second", "m", "raw")).data == b"raw"
    assert (await gmail.get_thread("first", "t")).messages[0].message_id == "m"
    assert (await gmail.get_attachment("second", "m", "a")).data == b"attachment"
    assert all("/users/me/" in path and method == "GET" for path, _, _, method in calls)
    assert calls[0][1]["q"] == ["from:sender"] and calls[0][1]["maxResults"] == ["1"]
    assert calls[1][1]["format"] == ["metadata"]
    assert calls[1][1]["metadataHeaders"] == ["From", "To", "Subject", "Date"]
    assert [auth for _, _, auth, _ in calls] == [
        "Bearer secret-first",
        "Bearer secret-first",
        "Bearer secret-second",
        "Bearer secret-second",
        "Bearer secret-first",
        "Bearer secret-second",
    ]
    assert "secret-" not in caplog.text
    await gmail.aclose()
    assert len(closed) == 1


async def test_cancelled_request_queued_on_shared_worker_never_calls_gmail():
    started = threading.Event()
    release = threading.Event()
    calls = []
    gmail = service(lambda *args: calls.append(1) or message(), capacity=1)

    def occupy_worker():
        started.set()
        assert release.wait(3)

    occupied = asyncio.create_task(gmail.worker.run(occupy_worker))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task = asyncio.create_task(gmail.get_email("u", "m"))
    while gmail._app_semaphore._value == gmail.limits.app_concurrency:
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done() and calls == []
    release.set()
    await occupied
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [] and gmail.worker.limiter.borrowed_tokens == 0
    await gmail.aclose()
