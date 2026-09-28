"""Encrypted per-user credentials and opaque, expiring database sessions."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta, timezone

from cryptography.fernet import Fernet
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from sqlalchemy import delete, select

from assistant_agent.config import get_settings
from assistant_agent.database import User, WebSession, make_session_factory, utcnow

SESSION_AGE = timedelta(days=14)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class WebStore:
    def __init__(self, factory=None, key: str | None = None):
        self.factory = factory or make_session_factory()
        self.cipher = Fernet((key or get_settings().credential_encryption_key).encode())

    def new_session(self) -> tuple[str, WebSession]:
        token = secrets.token_urlsafe(48)
        session = WebSession(session_token_hash=token_hash(token), csrf_token=secrets.token_urlsafe(32), expires_at=utcnow() + SESSION_AGE)
        with self.factory.begin() as db:
            db.execute(delete(WebSession).where(WebSession.expires_at < utcnow()))
            db.add(session)
        return token, session

    def session(self, token: str | None) -> WebSession | None:
        if not token:
            return None
        with self.factory() as db:
            row = db.scalar(select(WebSession).where(WebSession.session_token_hash == token_hash(token)))
            if row is None or row.expires_at.replace(tzinfo=row.expires_at.tzinfo or timezone.utc) <= utcnow():
                return None
            db.expunge(row)
            return row

    def save_session(self, row: WebSession) -> None:
        with self.factory.begin() as db:
            db.merge(row)

    def rotate(self, old_token: str, user_id: str) -> tuple[str, WebSession]:
        token = secrets.token_urlsafe(48)
        row = WebSession(session_token_hash=token_hash(token), user_id=user_id, csrf_token=secrets.token_urlsafe(32), expires_at=utcnow() + SESSION_AGE)
        with self.factory.begin() as db:
            db.execute(delete(WebSession).where(WebSession.session_token_hash == token_hash(old_token)))
            db.add(row)
        return token, row

    def user(self, user_id: str | None) -> User | None:
        if not user_id:
            return None
        with self.factory() as db:
            row = db.get(User, user_id)
            if row:
                db.expunge(row)
            return row

    def user_by_google_sub(self, google_sub: str) -> User | None:
        with self.factory() as db:
            row = db.scalar(select(User).where(User.google_sub == google_sub))
            if row:
                db.expunge(row)
            return row

    def save_credentials(self, google_sub: str, email: str, creds: Credentials) -> str:
        blob = json.loads(creds.to_json())
        blob.pop("client_id", None)
        blob.pop("client_secret", None)
        encrypted = self.cipher.encrypt(json.dumps(blob).encode())
        with self.factory.begin() as db:
            user = db.scalar(select(User).where(User.google_sub == google_sub))
            if user is None:
                user = User(google_sub=google_sub, email=email, credentials=encrypted, scopes=json.dumps(list(creds.scopes or [])))
                db.add(user)
            else:
                user.email = email
                user.credentials = encrypted
                user.scopes = json.dumps(list(creds.scopes or []))
                user.updated_at = utcnow()
            db.flush()
            return user.user_id

    def load_credentials(self, user_id: str) -> Credentials | None:
        user = self.user(user_id)
        if user is None:
            return None
        blob = json.loads(self.cipher.decrypt(user.credentials))
        settings = get_settings()
        blob.update(client_id=settings.google_client_id, client_secret=settings.google_client_secret)
        return Credentials.from_authorized_user_info(blob, scopes=blob.get("scopes"))

    def load_refreshed(self, user_id: str) -> Credentials | None:
        creds = self.load_credentials(user_id)
        if creds is None:
            return None
        if creds.valid:
            return creds
        if not creds.refresh_token:
            return None
        try:
            creds.refresh(GoogleRequest())
        except GoogleAuthError:
            return None
        user = self.user(user_id)
        if user:
            self.save_credentials(user.google_sub, user.email, creds)
        return creds

    def disconnect(self, user_id: str) -> None:
        with self.factory.begin() as db:
            db.execute(delete(WebSession).where(WebSession.user_id == user_id))
            db.execute(delete(User).where(User.user_id == user_id))
