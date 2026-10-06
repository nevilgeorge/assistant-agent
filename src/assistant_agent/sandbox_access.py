"""Database-backed, fixed-lifetime grants for a live conversation assignment."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant_agent.database import SandboxAccessToken, utcnow
from assistant_agent.sandbox import SandboxHandle
from assistant_agent.web_store import token_hash

TOKEN_AGE = timedelta(hours=24)
ALLOWED_TOOLS = frozenset(
    {"search_emails", "get_email", "get_thread", "download_emails", "download_attachment"}
)
LiveAssignmentResolver = Callable[[str, str, str], SandboxHandle | None]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IssuedSandboxAccess:
    token_id: str
    raw_token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class AuthorizedSandboxContext:
    user_id: str
    conversation_id: str
    container_id: str
    host_input_path: Path
    app_input_path: Path


class SandboxAuthorizationError(Exception):
    """A grant could not authorize the requested operation."""

    def __init__(self) -> None:
        """Create a denial error without exposing token or backend details."""
        super().__init__("Sandbox access denied")


def _as_utc(timestamp: datetime) -> datetime:
    """Treat naive database timestamps as UTC, preserving existing timezones."""
    # SQLite drops timezone information; production timestamps are timezone-aware.
    return timestamp.replace(tzinfo=timestamp.tzinfo or timezone.utc)


class SandboxAccessService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Initialize database access and loop-owned revocation retry state."""
        self.session_factory = session_factory
        self._failed_conversations: set[str] = set()
        self._failed_users: set[str] = set()
        self._invalidation_pending = False
        self._revocation_generation = 0
        self._revocation_lock = asyncio.Lock()

    async def issue(
        self, user_id: str, conversation_id: str, container_id: str
    ) -> IssuedSandboxAccess:
        """Persist a grant's hash and return its raw token with a fixed expiry.

        The caller must keep the raw token private. Startup invalidation must
        finish before issuance; database failures propagate to the caller.
        """
        if self._invalidation_pending:
            raise RuntimeError("Sandbox access startup invalidation pending")
        raw_token = secrets.token_urlsafe(48)
        created_at = utcnow()
        expires_at = created_at + TOKEN_AGE
        grant = SandboxAccessToken(
            token_hash=token_hash(raw_token),
            user_id=user_id,
            conversation_id=conversation_id,
            container_id=container_id,
            created_at=created_at,
            expires_at=expires_at,
        )
        async with self.session_factory.begin() as database_session:
            database_session.add(grant)
            await database_session.flush()
            token_id = grant.id
        return IssuedSandboxAccess(token_id, raw_token, expires_at)

    def _locally_revoked(self, grant: SandboxAccessToken) -> bool:
        """Deny grants covered by pending startup invalidation or revocation."""
        return (
            self._invalidation_pending
            or grant.conversation_id in self._failed_conversations
            or grant.user_id in self._failed_users
        )

    async def authorize(
        self,
        raw_token: str | None,
        tool_name: str,
        live_assignment_resolver: LiveAssignmentResolver,
    ) -> AuthorizedSandboxContext:
        """Authorize an allowed tool against an unexpired, live assignment.

        Resolve trusted identities and input paths from the assignment, checking
        expiry and retirement again after database work. Invalid grants and
        backend or resolver failures raise a generic SandboxAuthorizationError.
        """
        if not raw_token or tool_name not in ALLOWED_TOOLS or self._invalidation_pending:
            raise SandboxAuthorizationError()
        revocation_generation = self._revocation_generation
        try:
            async with self.session_factory() as database_session:
                grant = await database_session.scalar(
                    select(SandboxAccessToken).where(
                        SandboxAccessToken.token_hash == token_hash(raw_token)
                    )
                )
                if (
                    grant is None
                    or grant.revoked_at is not None
                    or _as_utc(grant.expires_at) <= utcnow()
                    or self._locally_revoked(grant)
                ):
                    raise SandboxAuthorizationError()
                sandbox_handle = live_assignment_resolver(
                    grant.user_id, grant.conversation_id, grant.container_id
                )
                if (
                    sandbox_handle is None
                    or sandbox_handle.user_id != grant.user_id
                    or sandbox_handle.conversation_id != grant.conversation_id
                    or sandbox_handle.container_id != grant.container_id
                ):
                    raise SandboxAuthorizationError()
                # Session close can yield; check lifetime and local retirement after it.
                authorized_context = AuthorizedSandboxContext(
                    grant.user_id,
                    grant.conversation_id,
                    grant.container_id,
                    sandbox_handle.host_input_path,
                    sandbox_handle.app_input_path,
                )
            if (
                _as_utc(grant.expires_at) <= utcnow()
                or self._locally_revoked(grant)
                or revocation_generation != self._revocation_generation
            ):
                raise SandboxAuthorizationError()
            # Closing the database session may also have allowed assignment retirement.
            if live_assignment_resolver(
                grant.user_id, grant.conversation_id, grant.container_id
            ) != sandbox_handle:
                raise SandboxAuthorizationError()
            if _as_utc(grant.expires_at) <= utcnow() or self._locally_revoked(grant):
                raise SandboxAuthorizationError()
            return authorized_context
        except Exception:
            # Neither database exceptions nor resolver errors may disclose bearer material.
            raise SandboxAuthorizationError() from None

    async def _revoke(self, scope_name: str, scope_id: str) -> bool:
        """Revoke a scope idempotently, retaining failures for denial and retry.

        Return whether the database update committed. Mark the scope pending
        before awaiting work so failures or cancellation keep access denied.
        """
        pending_scopes = (
            self._failed_conversations if scope_name == "conversation_id" else self._failed_users
        )
        pending_scopes.add(scope_id)
        self._revocation_generation += 1
        async with self._revocation_lock:
            try:
                async with self.session_factory.begin() as database_session:
                    await database_session.execute(
                        update(SandboxAccessToken)
                        .where(
                            getattr(SandboxAccessToken, scope_name) == scope_id,
                            SandboxAccessToken.revoked_at.is_(None),
                        )
                        .values(revoked_at=utcnow())
                    )
            except Exception:
                logger.warning("Sandbox access revocation failed; retry pending")
                return False
            pending_scopes.discard(scope_id)
            return True

    async def revoke_conversation(self, conversation_id: str) -> bool:
        """Revoke all conversation grants, returning whether the update committed."""
        return await self._revoke("conversation_id", conversation_id)

    async def revoke_user(self, user_id: str) -> bool:
        """Revoke all user grants, returning whether the update committed."""
        return await self._revoke("user_id", user_id)

    async def invalidate_outstanding(self) -> None:
        """Revoke outstanding grants across the database during startup recovery.

        Block authorization and issuance until the update commits. Database
        failures raise RuntimeError and leave the gate closed for a later retry.
        """
        self._invalidation_pending = True
        self._revocation_generation += 1
        async with self._revocation_lock:
            try:
                async with self.session_factory.begin() as database_session:
                    await database_session.execute(
                        update(SandboxAccessToken)
                        .where(SandboxAccessToken.revoked_at.is_(None))
                        .values(revoked_at=utcnow())
                    )
            except Exception:
                logger.warning("Sandbox access startup invalidation failed; retry pending")
                raise RuntimeError("Sandbox access startup invalidation failed") from None
            self._invalidation_pending = False

    async def retry_failed_revocations(self) -> bool:
        """Retry pending user and conversation scopes, returning whether all succeed."""
        all_succeeded = True
        for conversation_id in tuple(self._failed_conversations):
            if not await self.revoke_conversation(conversation_id):
                all_succeeded = False
        for user_id in tuple(self._failed_users):
            if not await self.revoke_user(user_id):
                all_succeeded = False
        return all_succeeded
