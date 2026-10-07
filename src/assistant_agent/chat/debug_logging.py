"""Best-effort JSON logging for completed Claude content."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

_debug_logger_lock = threading.Lock()


class _DebugStreamHandler(logging.StreamHandler):
    """Discard sink failures without exposing content in logging tracebacks."""

    def handleError(self, record: logging.LogRecord) -> None:
        """Keep debug output best-effort when stderr is unavailable."""


def _get_debug_logger() -> logging.Logger:
    """Configure one independent stderr logger for JSON debug records."""
    with _debug_logger_lock:
        debug_logger = logging.getLogger("assistant_agent.claude_debug_stream")
        if not debug_logger.handlers:
            handler = _DebugStreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            debug_logger.addHandler(handler)
        debug_logger.setLevel(logging.INFO)
        debug_logger.propagate = False
        return debug_logger


def emit_debug_record(record: dict[str, Any]) -> None:
    """Emit one escaped JSON line without allowing logging to interrupt delivery."""
    try:
        _get_debug_logger().info(json.dumps(record, ensure_ascii=True))
    except Exception:
        pass
