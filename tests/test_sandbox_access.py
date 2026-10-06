import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from assistant_agent.database import (
    Base,
    SandboxAccessToken,
    User,
    make_async_engine,
    make_async_session_factory,
    utcnow,
)
from assistant_agent.sandbox import SandboxHandle
from assistant_agent.sandbox_access import (
    ALLOWED_TOOLS,
    TOKEN_AGE,
    SandboxAccessService,
    SandboxAuthorizationError,
)
from assistant_agent.web_store import token_hash


@pytest.fixture
async def grants(tmp_path):
    database_engine = make_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tokens.db'}")
    session_factory = make_async_session_factory(database_engine)
    async with database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory.begin() as database_session:
        for user_id in ("alice", "bob"):
            database_session.add(
                User(
                    user_id=user_id,
                    google_sub=user_id,
                    email=f"{user_id}@example.com",
                    credentials=b"encrypted",
                    scopes="[]",
                )
            )
    sandbox_handle = SandboxHandle(
        "container-alice", "alice", "conversation-alice", Path("/host/alice"), Path("/app/alice")
    )
    access_service = SandboxAccessService(session_factory)
    issued = await access_service.issue("alice", "conversation-alice", "container-alice")
    try:
        yield SimpleNamespace(
            service=access_service,
            session_factory=session_factory,
            issued=issued,
            handle=sandbox_handle,
            resolver=lambda user_id, conversation_id, container_id: sandbox_handle,
        )
    finally:
        await database_engine.dispose()


async def test_issue_hash_only_fixed_lifetime_and_immutable_context(grants):
    async with grants.session_factory() as database_session:
        grant = await database_session.get(SandboxAccessToken, grants.issued.token_id)
        assert grant.token_hash == token_hash(grants.issued.raw_token)
        assert grants.issued.raw_token not in repr(grants.issued)
        assert grants.issued.raw_token not in repr(grant.__dict__)
        assert len(grant.id) == 32
        assert grant.expires_at - grant.created_at == TOKEN_AGE
    seen = []

    def resolve(user_id, conversation_id, container_id):
        seen.append((user_id, conversation_id, container_id))
        return grants.handle

    context = await grants.service.authorize(grants.issued.raw_token, "search_emails", resolve)
    assert context.user_id == "alice"
    assert context.conversation_id == "conversation-alice"
    assert context.container_id == "container-alice"
    assert context.host_input_path == Path("/host/alice")
    assert context.app_input_path == Path("/app/alice")
    assert all(identity == ("alice", "conversation-alice", "container-alice") for identity in seen)
    with pytest.raises(FrozenInstanceError):
        context.user_id = "bob"


@pytest.mark.parametrize("tool_name", sorted(ALLOWED_TOOLS))
async def test_each_documented_tool_allowed(grants, tool_name):
    await grants.service.authorize(grants.issued.raw_token, tool_name, grants.resolver)


async def test_authenticate_resolves_trusted_context_without_tool(grants):
    authenticated_context = await grants.service.authenticate(grants.issued.raw_token, grants.resolver)
    authorized_context = await grants.service.authorize(
        grants.issued.raw_token, "search_emails", grants.resolver
    )
    assert authenticated_context == authorized_context


@pytest.mark.parametrize("raw_token", [None, "", "unknown"])
async def test_authenticate_missing_or_unknown_grant_denied(grants, raw_token):
    with pytest.raises(SandboxAuthorizationError, match="Sandbox access denied"):
        await grants.service.authenticate(raw_token, grants.resolver)


async def test_authenticate_revoked_and_inactive_grants_denied(grants):
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authenticate(grants.issued.raw_token, lambda *args: None)
    await grants.service.revoke_conversation(grants.handle.conversation_id)
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authenticate(grants.issued.raw_token, grants.resolver)


@pytest.mark.parametrize("raw_token", [None, "", "unknown"])
async def test_missing_and_unknown_tokens(grants, raw_token):
    with pytest.raises(SandboxAuthorizationError, match="Sandbox access denied"):
        await grants.service.authorize(raw_token, "search_emails", grants.resolver)


async def test_hash_is_not_a_bearer_token(grants):
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(
            token_hash(grants.issued.raw_token), "search_emails", grants.resolver
        )


@pytest.mark.parametrize("tool_name", ["", "send_email", "search_email", "SEARCH_EMAILS"])
async def test_unlisted_tools_denied_before_resolver(grants, tool_name):
    def should_not_resolve(*args):
        pytest.fail("Disallowed tool reached resolver")

    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(grants.issued.raw_token, tool_name, should_not_resolve)


@pytest.mark.parametrize("field", ["user_id", "conversation_id", "container_id"])
async def test_assignment_identity_must_match(grants, field):
    mismatched_handle = replace(grants.handle, **{field: "other"})
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(
            grants.issued.raw_token, "search_emails", lambda *args: mismatched_handle
        )


async def test_inactive_assignment_denied(grants):
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(grants.issued.raw_token, "get_email", lambda *args: None)


async def test_resolver_failure_safe(grants):
    def broken_resolver(*args):
        raise RuntimeError(grants.issued.raw_token)

    with pytest.raises(SandboxAuthorizationError) as denied:
        await grants.service.authorize(grants.issued.raw_token, "get_email", broken_resolver)
    assert grants.issued.raw_token not in str(denied.value)
    assert denied.value.__suppress_context__


async def test_expired_grant_denied(grants):
    async with grants.session_factory.begin() as database_session:
        await database_session.execute(
            update(SandboxAccessToken).values(expires_at=utcnow() - timedelta(seconds=1))
        )
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(grants.issued.raw_token, "get_email", grants.resolver)


async def test_expiry_checked_after_resolver(grants, monkeypatch):
    import assistant_agent.sandbox_access as module

    def resolver(*args):
        monkeypatch.setattr(module, "utcnow", lambda: grants.issued.expires_at)
        return grants.handle

    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(grants.issued.raw_token, "get_email", resolver)


async def test_retirement_during_session_close_denied(grants):
    calls = 0

    def resolver(*args):
        nonlocal calls
        calls += 1
        return grants.handle if calls == 1 else None

    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(grants.issued.raw_token, "get_email", resolver)


async def test_completed_revocation_during_session_close_denies_cached_grant(grants):
    class RevokeOnCloseFactory:
        @asynccontextmanager
        async def __call__(self):
            async with grants.session_factory() as database_session:
                yield database_session
            assert await grants.service.revoke_user("alice")

        def begin(self):
            return grants.session_factory.begin()

    grants.service.session_factory = RevokeOnCloseFactory()
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(grants.issued.raw_token, "get_email", grants.resolver)


@pytest.mark.parametrize("scope", ["conversation", "user"])
async def test_idempotent_revocation_preserves_other_users(grants, scope):
    other_grant = await grants.service.issue("bob", "conversation-bob", "container-bob")
    revoke = getattr(grants.service, f"revoke_{scope}")
    scope_id = "conversation-alice" if scope == "conversation" else "alice"
    assert await revoke(scope_id)
    assert await revoke(scope_id)
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(grants.issued.raw_token, "get_email", grants.resolver)
    async with grants.session_factory() as database_session:
        other_row = await database_session.get(SandboxAccessToken, other_grant.token_id)
        assert other_row.revoked_at is None


class BrokenFactory:
    def __call__(self):
        raise RuntimeError("database unavailable")

    def begin(self):
        raise RuntimeError("database unavailable")


@pytest.mark.parametrize("scope", ["conversation", "user"])
async def test_failed_revocation_denies_locally_and_retries(grants, scope, caplog):
    grants.service.session_factory = BrokenFactory()
    revoke = getattr(grants.service, f"revoke_{scope}")
    scope_id = "conversation-alice" if scope == "conversation" else "alice"
    assert not await revoke(scope_id)
    assert not await grants.service.retry_failed_revocations()
    grants.service.session_factory = grants.session_factory
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(grants.issued.raw_token, "get_email", grants.resolver)
    assert await grants.service.retry_failed_revocations()
    assert await grants.service.retry_failed_revocations()
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(grants.issued.raw_token, "get_email", grants.resolver)
    assert grants.issued.raw_token not in caplog.text
    assert "database unavailable" not in caplog.text


async def test_database_authorization_failure_denies(grants):
    grants.service.session_factory = BrokenFactory()
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(grants.issued.raw_token, "get_email", grants.resolver)


async def test_cancelled_revocation_retains_local_denial_and_retry(grants):
    class CancelledFactory:
        @asynccontextmanager
        async def begin(self):
            raise asyncio.CancelledError
            yield  # pragma: no cover

    grants.service.session_factory = CancelledFactory()
    with pytest.raises(asyncio.CancelledError):
        await grants.service.revoke_conversation("conversation-alice")
    grants.service.session_factory = grants.session_factory
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(grants.issued.raw_token, "get_email", grants.resolver)
    assert await grants.service.retry_failed_revocations()


async def test_startup_invalidation_failure_blocks_until_success(grants):
    grants.service.session_factory = BrokenFactory()
    with pytest.raises(RuntimeError, match="startup invalidation failed"):
        await grants.service.invalidate_outstanding()
    grants.service.session_factory = grants.session_factory
    with pytest.raises(RuntimeError, match="invalidation pending"):
        await grants.service.issue("alice", "conversation-alice", "container-alice")
    with pytest.raises(SandboxAuthorizationError):
        await grants.service.authorize(grants.issued.raw_token, "get_email", grants.resolver)
    await grants.service.invalidate_outstanding()
    async with grants.session_factory() as database_session:
        rows = list(await database_session.scalars(select(SandboxAccessToken)))
        assert all(row.revoked_at is not None for row in rows)
    new_grant = await grants.service.issue("alice", "conversation-alice", "container-alice")
    await grants.service.authorize(new_grant.raw_token, "get_email", grants.resolver)


async def test_restart_revokes_all_old_grants(grants):
    restarted_service = SandboxAccessService(grants.session_factory)
    await restarted_service.invalidate_outstanding()
    await restarted_service.invalidate_outstanding()
    with pytest.raises(SandboxAuthorizationError):
        await restarted_service.authorize(grants.issued.raw_token, "get_email", grants.resolver)
