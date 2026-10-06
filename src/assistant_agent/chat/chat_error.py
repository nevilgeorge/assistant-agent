"""User-facing chat errors."""

from __future__ import annotations


class ChatError(RuntimeError):
    """A user-facing chat error with the HTTP status returned by the web API."""

    def __init__(self, message, status=409):
        """Store the user-facing message and HTTP status."""
        super().__init__(message)
        self.status = status

