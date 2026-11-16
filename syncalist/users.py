"""Home users and their server tokens.

Tokens are fetched from the admin account (no PIN needed) and cached encrypted.
If the server rejects a cached token, a fresh one is fetched once. If that
fails too, a fallback PIN is tried if the admin entered one. Failing that, the
user is marked "needs attention" and skipped, and everyone else carries on.
"""

from collections.abc import Callable
from typing import TypeVar

from sqlmodel import Session, select

from .crypto import Secrets
from .models import HomeUser, utcnow
from .plex.gateway import Gateway, PlexError, TokenRejected

T = TypeVar("T")


class UserNeedsAttention(Exception):
    pass


class UserService:
    def __init__(self, session: Session, secrets: Secrets, gateway: Gateway):
        self.session = session
        self.secrets = secrets
        self.gateway = gateway

    def all(self) -> list[HomeUser]:
        users = self.session.exec(select(HomeUser)).all()
        return sorted(users, key=lambda u: (not u.is_admin, u.title.lower()))

    def refresh(self) -> list[HomeUser]:
        """Sync the user list from plex.tv. Removed Home users are deleted."""
        seen = set()
        for info in self.gateway.home_users():
            seen.add(info.id)
            user = self.session.get(HomeUser, info.id) or HomeUser(id=info.id, title=info.title)
            user.title = info.title
            user.thumb = info.thumb
            user.is_admin = info.is_admin
            user.has_pin = info.has_pin
            user.updated_at = utcnow()
            self.session.add(user)
        for user in self.session.exec(select(HomeUser)).all():
            if user.id not in seen:
                self.session.delete(user)
        self.session.commit()
        return self.all()

    def set_fallback_pin(self, user: HomeUser, pin: str) -> None:
        """Verify a PIN by fetching a token with it, then store both."""
        token = self.gateway.user_token_with_pin(user.id, pin)
        user.fallback_pin_enc = self.secrets.encrypt(pin)
        self._store_token(user, token)

    def call_as(self, user: HomeUser, fn: Callable[[str], T]) -> T:
        """Run fn(token) as this user, refreshing the token once if it is rejected."""
        token = self.secrets.decrypt(user.token_enc) or self._fetch_token(user)
        try:
            return fn(token)
        except TokenRejected:
            return fn(self._fetch_token(user))

    def _fetch_token(self, user: HomeUser) -> str:
        errors = []
        try:
            token = self.gateway.user_token(user.id)
            self._store_token(user, token)
            return token
        except PlexError as e:
            errors.append(f"without PIN: {e}")
        pin = self.secrets.decrypt(user.fallback_pin_enc)
        if pin:
            try:
                token = self.gateway.user_token_with_pin(user.id, pin)
                self._store_token(user, token)
                return token
            except PlexError as e:
                errors.append(f"with saved PIN: {e}")
        user.status = "needs_attention"
        user.status_detail = "Couldn't get access for this user (" + "; ".join(errors) + ")"
        user.token_enc = None
        self.session.add(user)
        self.session.commit()
        raise UserNeedsAttention(user.status_detail)

    def _store_token(self, user: HomeUser, token: str) -> None:
        user.token_enc = self.secrets.encrypt(token)
        user.status = "ok"
        user.status_detail = None
        self.session.add(user)
        self.session.commit()
