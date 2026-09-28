#!/usr/bin/env python3
"""Flatten an exported Google Calendar tree into a single JSONL index.

Usage:
    python3 build_index.py [OUT.jsonl]     # build the index (default: ./events.jsonl)
    python3 build_index.py --inventory     # print calendars, counts and date ranges

Reads every *.json under this script's directory, regardless of how the export
nests them (year folders, hashed calendar folders, flat — all work).
"""
import collections
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent


def load():
    """Yield (path, calendarId, event) for every event file under ROOT."""
    for f in sorted(ROOT.rglob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(d, dict) and "event" in d:
            yield f, d.get("calendarId"), d["event"]


def flatten(f, cal_id, e):
    org = e.get("organizer", {}) or {}
    s, en = e.get("start", {}), e.get("end", {})
    all_day = "date" in s

    if all_day:
        sd = dt.date.fromisoformat(s["date"])
        # Google's all-day end date is exclusive; store the inclusive last day.
        ed = dt.date.fromisoformat(en.get("date", s["date"]))
        dur_min = max((ed - sd).days, 0) * 24 * 60
        start_local = sd.isoformat() + "T00:00"
        end_date = (ed - dt.timedelta(days=1)).isoformat()
        tz = start_time = end_time = None
    else:
        sdt = dt.datetime.fromisoformat(s["dateTime"])
        edt = dt.datetime.fromisoformat(en.get("dateTime", s["dateTime"]))
        sd = sdt.date()
        dur_min = int((edt - sdt).total_seconds() // 60)
        start_local = sdt.isoformat()
        end_date = edt.date().isoformat()
        tz = s.get("timeZone")
        start_time, end_time = sdt.strftime("%H:%M"), edt.strftime("%H:%M")

    atts = e.get("attendees", []) or []
    return {
        "cal_id": cal_id,
        "date": sd.isoformat(),
        "weekday": sd.strftime("%a"),
        "end_date": end_date,
        "all_day": all_day,
        "start": start_local,
        "start_time": start_time,
        "end_time": end_time,
        "tz": tz,
        "duration_min": dur_min,
        "summary": e.get("summary", ""),
        "location": e.get("location"),
        "description": e.get("description"),
        "event_type": e.get("eventType"),
        "status": e.get("status"),
        "transparency": e.get("transparency", "opaque"),
        "organizer": org.get("email"),
        "organizer_name": org.get("displayName"),
        "creator": (e.get("creator") or {}).get("email"),
        "attendees": [a["email"] for a in atts if not a.get("self") and a.get("email")],
        "n_attendees": len(atts),
        "self_response": next(
            (a.get("responseStatus") for a in atts if a.get("self")), None
        ),
        "recurring": bool(e.get("recurringEventId")),
        "hangout": e.get("hangoutLink"),
        "id": e.get("id"),
        "ical_uid": e.get("iCalUID"),
        "file": str(f.relative_to(ROOT)),
    }


def build():
    rows, names = [], {}
    for f, cal_id, e in load():
        # A calendar names itself on the events it owns.
        org = e.get("organizer", {}) or {}
        if org.get("email") == cal_id and org.get("displayName"):
            names.setdefault(cal_id, org["displayName"])
        rows.append(flatten(f, cal_id, e))
    for r in rows:
        r["calendar"] = names.get(r["cal_id"], r["cal_id"])
    rows.sort(key=lambda r: (r["start"], r["summary"]))
    return rows


def inventory(rows):
    by = collections.defaultdict(list)
    for r in rows:
        by[r["calendar"]].append(r)
    print(f"{len(rows)} events across {len(by)} calendars\n")
    for cal, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        d = [r["date"] for r in rs]
        print(f"  {cal}\n    {len(rs):>4} events   {min(d)} -> {max(d)}")
        types = collections.Counter(r["event_type"] for r in rs)
        tzs = collections.Counter(r["tz"] for r in rs if r["tz"])
        print(f"    types: {dict(types)}")
        if tzs:
            print(f"    zones: {dict(tzs)}")
    print("\n  self response: ", dict(collections.Counter(
        r["self_response"] for r in rows if r["self_response"])))
    print("  frequent attendees:")
    who = collections.Counter(a for r in rows for a in r["attendees"])
    for email, n in who.most_common(15):
        print(f"    {n:>3}  {email}")


if __name__ == "__main__":
    rows = build()
    if "--inventory" in sys.argv[1:]:
        inventory(rows)
    else:
        args = [a for a in sys.argv[1:] if not a.startswith("-")]
        out = pathlib.Path(args[0]) if args else pathlib.Path("events.jsonl")
        with out.open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{len(rows)} events -> {out}")
