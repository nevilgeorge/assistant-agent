import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from httplib2 import Response
from googleapiclient.errors import HttpError

from assistant_agent.export_2026 import END_MS, START_MS, Exporter, RequestGate, call


class Request:
    def __init__(self, result):
        self.result = result

    def execute(self, num_retries=0):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Resource:
    def __init__(self, handler):
        self.handler = handler

    def list(self, **kwargs):
        return Request(self.handler("list", kwargs))

    def get(self, **kwargs):
        return Request(self.handler("get", kwargs))


class Gmail:
    def __init__(self, handler):
        self.resource = Resource(handler)

    def users(self):
        return self

    def messages(self):
        return self.resource


class Calendar:
    def __init__(self, list_handler, event_handler):
        self.list_resource = Resource(list_handler)
        self.event_resource = Resource(event_handler)

    def calendarList(self):
        return self.list_resource

    def events(self):
        return self.event_resource


def fake_exporter(output, gmail, calendar):
    exporter = Exporter.__new__(Exporter)
    exporter.output = output
    exporter.gate = RequestGate(interval=0)
    exporter.local = threading.local()
    exporter.count_lock = threading.Lock()
    exporter._service = lambda name: gmail if name == "gmail" else calendar
    exporter.counts = {
        "gmail_listed": 0, "gmail_saved": 0, "gmail_skipped": 0,
        "gmail_outside_window": 0, "calendar_count": 0,
        "calendar_saved": 0, "calendar_skipped": 0,
    }
    exporter.incomplete = []
    return exporter


class ExportTests(unittest.TestCase):
    def test_pages_boundaries_recurring_overlap_and_rerun(self):
        gmail_pages = []
        gets = []

        def gmail_handler(method, kwargs):
            if method == "list":
                gmail_pages.append(kwargs)
                if kwargs["pageToken"] is None:
                    return {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "two"}
                return {"messages": [{"id": "c"}, {"id": "d"}, {"id": "a"}]}
            gets.append(kwargs["id"])
            stamps = {"a": START_MS - 1, "b": START_MS, "c": END_MS - 1, "d": END_MS}
            return {"id": kwargs["id"], "internalDate": str(stamps[kwargs["id"]]), "payload": {}}

        calendar_pages = []

        def calendar_list(method, kwargs):
            if kwargs["pageToken"] is None:
                return {"items": [{"id": "first@example.com"}], "nextPageToken": "next"}
            return {"items": [{"id": "second@example.com"}, {"id": "hidden", "hidden": True}]}

        def events(method, kwargs):
            calendar_pages.append(kwargs)
            if kwargs["calendarId"] == "first@example.com" and kwargs["pageToken"] is None:
                return {"items": [
                    {"id": "overlap", "start": {"dateTime": "2025-12-31T23:00:00Z"}, "end": {"dateTime": "2026-01-01T01:00:00Z"}},
                    {"id": "cancelled", "status": "cancelled"},
                ], "nextPageToken": "more"}
            if kwargs["calendarId"] == "first@example.com":
                return {"items": [{"id": "instance", "recurringEventId": "series", "start": {"dateTime": "2026-09-30T10:00:00Z"}}]}
            return {"items": [{"id": "instance", "recurringEventId": "other-series"}]}

        with TemporaryDirectory() as tmp:
            exporter = fake_exporter(Path(tmp), Gmail(gmail_handler), Calendar(calendar_list, events))
            report = exporter.run()
            self.assertTrue(report["complete"])
            self.assertEqual(report["counts"]["gmail_saved"], 2)
            self.assertEqual(report["counts"]["gmail_outside_window"], 2)
            self.assertEqual(report["counts"]["calendar_saved"], 3)
            self.assertEqual(len(gets), 4)
            self.assertEqual(len(gmail_pages), 2)
            self.assertEqual(gmail_pages[0]["maxResults"], 500)
            self.assertFalse(gmail_pages[0]["includeSpamTrash"])
            self.assertTrue(all(p["singleEvents"] and p["maxResults"] == 2500 for p in calendar_pages))
            self.assertTrue(all(p["timeMin"] == "2026-01-01T00:00:00Z" for p in calendar_pages))
            files = list((Path(tmp) / "calendar" / "2026").glob("*/*.json"))
            self.assertEqual(len(files), 3)
            self.assertEqual({json.loads(f.read_text())["calendarId"] for f in files}, {"first@example.com", "second@example.com"})
            second = fake_exporter(Path(tmp), Gmail(gmail_handler), Calendar(calendar_list, events))
            rerun = second.run()
            self.assertEqual(rerun["counts"]["gmail_skipped"], 2)
            self.assertEqual(rerun["counts"]["calendar_skipped"], 3)
            self.assertEqual(len(gets), 6)  # Out-of-window messages are checked again.

    def test_retry_and_incomplete_pagination(self):
        error = HttpError(Response({"status": "429", "retry-after": "0"}), b'{"error":{}}')
        attempts = 0

        def request():
            nonlocal attempts
            attempts += 1
            return Request(error if attempts == 1 else {"ok": True})

        with patch("assistant_agent.export_2026.time.sleep"):
            self.assertEqual(call(RequestGate(interval=0), request), {"ok": True})
        self.assertEqual(attempts, 2)

        def failed_list(method, kwargs):
            raise ValueError("page failed")

        exporter = fake_exporter(Path("/tmp/unused-export-test"), Gmail(failed_list), Calendar(lambda *_: {"items": []}, lambda *_: {"items": []}))
        with TemporaryDirectory() as tmp:
            exporter.output = Path(tmp)
            report = exporter.run()
            self.assertFalse(report["complete"])
            self.assertIn("Gmail pagination", report["incomplete"][0])

    def test_calendar_only_never_calls_gmail_or_changes_email_files(self):
        def forbidden_gmail(*_):
            raise AssertionError("Gmail must not be called")

        calendar = Calendar(
            lambda *_: {"items": [{"id": "primary"}]},
            lambda *_: {"items": [{"id": "event-1", "summary": "Meeting"}]},
        )
        with TemporaryDirectory() as tmp:
            output = Path(tmp)
            email_path = output / "emails" / "2026" / "existing.json"
            email_path.parent.mkdir(parents=True)
            email_path.write_text('{"id":"existing"}')
            exporter = fake_exporter(output, Gmail(forbidden_gmail), calendar)
            report = exporter.run(calendar_only=True)
            self.assertTrue(report["complete"])
            self.assertEqual(report["mode"], "calendar-only")
            self.assertEqual(report["counts"], {
                "calendar_count": 1, "calendar_saved": 1, "calendar_skipped": 0
            })
            self.assertEqual(email_path.read_text(), '{"id":"existing"}')
            self.assertFalse((output / "export-2026-report.json").exists())
            self.assertEqual(
                json.loads((output / "export-2026-calendar-report.json").read_text()), report
            )

    def test_global_request_limits(self):
        gate = RequestGate(interval=0.02, concurrent=4)
        lock = threading.Lock()
        starts = []
        active = 0
        maximum = 0

        class TimedRequest:
            def execute(self, num_retries=0):
                nonlocal active, maximum
                with lock:
                    starts.append(time.monotonic())
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.06)
                with lock:
                    active -= 1
                return True

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertTrue(all(pool.map(lambda _: gate.execute(TimedRequest()), range(8))))
        self.assertLessEqual(maximum, 4)
        self.assertTrue(all(b - a >= 0.018 for a, b in zip(starts, starts[1:])))


if __name__ == "__main__":
    unittest.main()
