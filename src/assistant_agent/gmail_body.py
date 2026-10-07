"""Extract readable email bodies from decoded Gmail MIME payloads."""

from __future__ import annotations

import html
import html.parser
import re
from collections.abc import Iterator
from typing import Any

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
    except Exception:  # noqa: BLE001 - a broken message must not stop retrieval
        text = re.sub(r"<[^>]+>", " ", markup)
        text = html.unescape(text)
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def walk_parts(part: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield a MIME part and every part nested beneath it."""
    yield part
    for child in part.get("parts") or []:
        yield from walk_parts(child)


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

