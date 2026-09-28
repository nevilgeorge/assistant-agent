#!/usr/bin/env python3
"""Flatten an exported Gmail tree into a queryable index plus a plain-text corpus.

Usage:
    python3 build_index.py                 # build .index/ beside this script
    python3 build_index.py --out DIR       # build into DIR instead
    python3 build_index.py --inventory     # print a profile of the corpus
    python3 build_index.py --no-bodies     # metadata only, skip the text corpus

Produces, under the output directory:
    messages.jsonl    one JSON row per message (metadata, recipients, attachments)
    bodies/<id>.txt   the message body as plain text, HTML stripped

Standard library only, so it runs anywhere the export is mounted.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import html
import html.parser
import json
import pathlib
import re
import sys
from collections.abc import Iterator
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent

# Tags whose CONTENTS are markup or styling, never readable text. Only tags that
# always carry a closing tag belong here: a void element such as <meta> or <link>
# would raise the suppression counter and never lower it, blanking the whole message.
_DROP_CONTENT = {"script", "style", "title", "noscript"}
# Tags that imply a line break when they open or close.
_BREAK = {
    "br", "p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "section", "article", "header", "footer", "hr",
}
_WS = re.compile(
    "[ \\t\\r\\f\\v\\u00a0\\u200b\\u200c\\u200d\\ufeff]+"
)  # nbsp + zero-width chars, written as escapes so they stay visible in source
_BLANKS = re.compile(r"\n{3,}")
# HTML wins over a text/plain part only when it yields this much more text.
_STUB_RATIO = 2.0


class _TextExtractor(html.parser.HTMLParser):
    """Collapse an HTML document to readable text, preserving block structure."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT:
            self._suppress += 1
        elif tag in _BREAK:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT:
            self._suppress = max(0, self._suppress - 1)
        elif tag in _BREAK:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppress:
            self._out.append(data)

    def text(self) -> str:
        return "".join(self._out)


def html_to_text(markup: str) -> str:
    """Best-effort HTML -> text. Never raises; malformed markup degrades gracefully."""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
        text = parser.text()
    except Exception:  # noqa: BLE001 - a broken message must not stop the build
        text = re.sub(r"<[^>]+>", " ", markup)
        text = html.unescape(text)
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def walk_parts(part: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield a MIME part and every part nested beneath it."""
    yield part
    for child in part.get("parts") or []:
        yield from walk_parts(child)


def header_map(part: dict[str, Any]) -> dict[str, str]:
    """Lower-cased header name -> value. Later duplicates win, as Gmail displays them."""
    return {h["name"].lower(): h["value"] for h in part.get("headers") or []}


_ADDR = re.compile(r"^\s*(?:\"?(?P<name>[^\"<]*?)\"?\s*)?<(?P<email>[^>]+)>\s*$")


def split_address(raw: str) -> tuple[str | None, str | None]:
    """Split 'Name <a@b.c>' into its parts. A bare address yields no name."""
    raw = raw.strip()
    if not raw:
        return None, None
    m = _ADDR.match(raw)
    if m:
        name = (m.group("name") or "").strip() or None
        return name, m.group("email").strip().lower()
    return None, raw.lower()


def split_address_list(raw: str | None) -> list[str]:
    """Extract just the addresses from a comma-separated recipient header."""
    if not raw:
        return []
    out: list[str] = []
    for chunk in re.split(r",(?![^<]*>)", raw):
        _, email = split_address(chunk)
        if email:
            out.append(email)
    return out


def extract_body(payload: dict[str, Any]) -> tuple[str, str]:
    """Return (text, source) where source is 'plain', 'html', 'calendar' or 'none'.

    text/plain parts are preferred; HTML is stripped only when no plain part exists,
    because the stripped form loses link targets and table structure.
    """
    plain: list[str] = []
    markup: list[str] = []
    calendar: list[str] = []
    for part in walk_parts(payload):
        data = (part.get("body") or {}).get("data")
        if not isinstance(data, str) or not data:
            continue
        if part.get("filename"):  # an attachment, not body text
            continue
        mime = part.get("mimeType") or ""
        if mime == "text/plain":
            plain.append(data)
        elif mime == "text/html":
            markup.append(data)
        elif mime in ("text/calendar", "application/ics"):
            calendar.append(data)
    # Prefer text/plain, but only when it is the real message. Senders routinely ship
    # a plain part that is empty or a bare stub ("Ticketmaster") beside a full HTML
    # body; taking it on faith silently drops the content. Measured across a real
    # corpus the ratio is bimodal - genuine alternatives land under 1.5x, stubs above
    # 2x - so fall back to HTML whenever HTML yields more than twice the text.
    plain_text = "\n\n".join(p.strip() for p in plain).strip()
    html_text = "\n\n".join(html_to_text(m) for m in markup).strip()
    if plain_text and len(html_text) <= _STUB_RATIO * len(plain_text):
        return plain_text, "plain"
    if html_text:
        return html_text, "html"
    if plain_text:
        return plain_text, "plain"
    joined = "\n\n".join(c.strip() for c in calendar).strip()
    if joined:
        return joined, "calendar"
    return "", "none"


def parse_dates(headers: dict[str, str], internal_ms: str | None) -> dict[str, Any]:
    """Gmail's internalDate is authoritative for ordering; the Date header carries
    the sender's UTC offset, which is what a human means by 'when it arrived'."""
    out: dict[str, Any] = {
        "epoch_ms": None, "date_utc": None, "date_local": None,
        "date": None, "weekday": None, "utc_offset": None,
    }
    if internal_ms:
        epoch = int(internal_ms)
        out["epoch_ms"] = epoch
        # dt.UTC (ruff UP017) needs Python 3.11+; keep the portable spelling.
        utc = dt.datetime.fromtimestamp(epoch / 1000, dt.timezone.utc)  # noqa: UP017
        out["date_utc"] = utc.isoformat()
        out["date"] = utc.date().isoformat()
        out["weekday"] = utc.strftime("%a")
    raw = headers.get("date")
    if raw:
        try:
            import email.utils

            local = email.utils.parsedate_to_datetime(raw)
            if local is not None:
                out["date_local"] = local.isoformat()
                if local.tzinfo is not None:
                    offset = local.utcoffset()
                    if offset is not None:
                        total = int(offset.total_seconds())
                        sign = "+" if total >= 0 else "-"
                        out["utc_offset"] = f"{sign}{abs(total)//3600:02d}:{abs(total)%3600//60:02d}"
                    out["date"] = local.date().isoformat()
                    out["weekday"] = local.strftime("%a")
        except (TypeError, ValueError, IndexError):
            pass
    return out


def flatten(path: pathlib.Path, msg: dict[str, Any]) -> dict[str, Any]:
    payload = msg.get("payload") or {}
    h = header_map(payload)
    from_name, from_email = split_address(h.get("from", ""))
    labels = msg.get("labelIds") or []
    category = next(
        (lbl[len("CATEGORY_"):].lower() for lbl in labels if lbl.startswith("CATEGORY_")),
        None,
    )
    attachments = [
        {
            "filename": p["filename"],
            "mime": p.get("mimeType"),
            "size": (p.get("body") or {}).get("size"),
            # Content is NOT in the export; it must be refetched from the API by id.
            "attachment_id": (p.get("body") or {}).get("attachmentId"),
        }
        for p in walk_parts(payload)
        if p.get("filename")
    ]
    row: dict[str, Any] = {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "year_dir": path.parent.name,
        "from_name": from_name,
        "from_email": from_email,
        "to": split_address_list(h.get("to")),
        "cc": split_address_list(h.get("cc")),
        "reply_to": split_address_list(h.get("reply-to")),
        "subject": h.get("subject", ""),
        "snippet": html.unescape(msg.get("snippet") or ""),
        "labels": labels,
        "category": category,
        "is_sent": "SENT" in labels,
        "is_inbox": "INBOX" in labels,
        "is_unread": "UNREAD" in labels,
        "is_starred": "STARRED" in labels,
        "is_draft": "DRAFT" in labels,
        # Raw signals for classifying bulk vs personal mail at query time.
        "list_id": h.get("list-id"),
        "is_bulk": bool(h.get("list-unsubscribe") or h.get("list-id")),
        "in_reply_to": h.get("in-reply-to"),
        "has_references": bool(h.get("references")),
        "size_estimate": msg.get("sizeEstimate"),
        "mime_top": payload.get("mimeType"),
        "has_attachments": bool(attachments),
        "attachments": attachments,
        "file": str(path.relative_to(ROOT)),
    }
    row.update(parse_dates(h, msg.get("internalDate")))
    return row


def iter_messages(root: pathlib.Path) -> Iterator[tuple[pathlib.Path, dict[str, Any]]]:
    """Yield every message file, skipping dot-directories such as .index/."""
    for path in sorted(root.rglob("*.json")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            msg = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            print(f"  skipping {path.name}: {exc}", file=sys.stderr)
            continue
        if isinstance(msg, dict) and "payload" in msg:
            yield path, msg


def build(out_dir: pathlib.Path, write_bodies: bool = True) -> list[dict[str, Any]]:
    bodies_dir = out_dir / "bodies"
    if write_bodies:
        bodies_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for n, (path, msg) in enumerate(iter_messages(ROOT), 1):
        row = flatten(path, msg)
        text, source = extract_body(msg.get("payload") or {})
        row["body_source"] = source
        row["body_chars"] = len(text)
        if write_bodies and text:
            target = bodies_dir / f"{row['id']}.txt"
            target.write_text(text, encoding="utf-8")
            row["body_file"] = str(target.relative_to(out_dir))
        else:
            row["body_file"] = None
        rows.append(row)
        if n % 500 == 0:
            print(f"  {n} messages...", flush=True)

    sizes = collections.Counter(r["thread_id"] for r in rows)
    order: dict[str, int] = collections.defaultdict(int)
    for r in sorted(rows, key=lambda r: (r["epoch_ms"] or 0)):
        r["thread_size"] = sizes[r["thread_id"]]
        order[r["thread_id"]] += 1
        r["thread_pos"] = order[r["thread_id"]]

    rows.sort(key=lambda r: (r["epoch_ms"] or 0))
    with (out_dir / "messages.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rows


def inventory(rows: list[dict[str, Any]]) -> None:
    dates = [r["date"] for r in rows if r["date"]]
    print(f"{len(rows)} messages")
    if dates:
        print(f"  date range : {min(dates)} -> {max(dates)}")
    print(f"  threads    : {len({r['thread_id'] for r in rows})}"
          f"  (multi-message: {sum(1 for r in rows if r['thread_size'] > 1 and r['thread_pos'] == 1)})")
    print(f"  sent       : {sum(1 for r in rows if r['is_sent'])}"
          f"   bulk/list: {sum(1 for r in rows if r['is_bulk'])}"
          f"   with attachments: {sum(1 for r in rows if r['has_attachments'])}")
    print(f"  body source: {dict(collections.Counter(r['body_source'] for r in rows))}")
    print(f"  categories : {dict(collections.Counter(r['category'] for r in rows))}")
    per_year = collections.Counter(r["year_dir"] for r in rows)
    print(f"  year dirs  : {dict(per_year)}")
    per_month = collections.Counter(r["date"][:7] for r in rows if r["date"])
    print("  per month  : " + ", ".join(f"{k}={v}" for k, v in sorted(per_month.items())))
    print("\n  top senders:")
    for email_addr, n in collections.Counter(
        r["from_email"] for r in rows if r["from_email"]
    ).most_common(15):
        print(f"    {n:>5}  {email_addr}")
    print("\n  attachment types:")
    exts = collections.Counter(
        (a["filename"].rsplit(".", 1)[-1].lower() if "." in a["filename"] else "?")
        for r in rows for a in r["attachments"]
    )
    print(f"    {dict(exts.most_common(12))}")
    print("\n  NOTE: attachment bodies are not in the export; only names and types.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / ".index",
                    help="output directory (default: .index beside this script)")
    ap.add_argument("--inventory", action="store_true",
                    help="print a corpus profile after building")
    ap.add_argument("--no-bodies", action="store_true",
                    help="write messages.jsonl only, skipping the text corpus")
    args = ap.parse_args()

    rows = build(args.out, write_bodies=not args.no_bodies)
    print(f"{len(rows)} messages -> {args.out / 'messages.jsonl'}")
    if not args.no_bodies:
        written = sum(1 for r in rows if r["body_file"])
        print(f"{written} body files -> {args.out / 'bodies'}/")
    if args.inventory:
        print()
        inventory(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
