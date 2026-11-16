"""Everything Syncalist does on the Plex server goes through PlexGateway.

The rest of the app only sees the small dataclasses below, so tests can swap in
a fake gateway without a Plex server.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import requests
from plexapi.exceptions import BadRequest, NotFound, Unauthorized
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

TIMEOUT = 15


class PlexError(Exception):
    """Plex couldn't be reached or refused the request."""


class TokenRejected(PlexError):
    """The server rejected a user's token (expired or revoked)."""


@dataclass(frozen=True)
class MusicSection:
    key: str
    title: str


@dataclass(frozen=True)
class ServerInfo:
    name: str
    machine_id: str
    version: str
    owner_is_signed_in_account: bool
    music_sections: list[MusicSection]


@dataclass(frozen=True)
class HomeUserInfo:
    id: int
    title: str
    thumb: str | None
    has_pin: bool
    is_admin: bool


@dataclass(frozen=True)
class PlaylistSummary:
    key: int
    title: str
    track_count: int
    updated_at: datetime | None
    smart: bool


@dataclass(frozen=True)
class Track:
    key: int
    title: str
    artist: str
    album: str
    duration_ms: int | None


class Gateway(Protocol):
    def server_info(self) -> ServerInfo: ...
    def home_users(self) -> list[HomeUserInfo]: ...
    def user_token(self, user_id: int) -> str: ...
    def user_token_with_pin(self, user_id: int, pin: str) -> str: ...
    def playlists(self, user_token: str) -> list[PlaylistSummary]: ...
    def playlist_tracks(self, user_token: str, playlist_key: int) -> list[Track]: ...


def _wrap(fn):
    """Turn plexapi/requests exceptions into our own."""
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Unauthorized as e:
            raise TokenRejected(str(e)) from e
        except (BadRequest, NotFound, requests.RequestException) as e:
            raise PlexError(str(e)) from e
    return inner


class PlexApiGateway:
    """Real gateway backed by python-plexapi. One instance per admin token."""

    def __init__(self, url: str, admin_token: str):
        self.url = url.rstrip("/")
        self._admin_token = admin_token
        self._admin_server: PlexServer | None = None
        self._account: MyPlexAccount | None = None

    # -- admin side ------------------------------------------------------------
    @property
    def admin_server(self) -> PlexServer:
        if self._admin_server is None:
            self._admin_server = _wrap(PlexServer)(self.url, self._admin_token, timeout=TIMEOUT)
        return self._admin_server

    @property
    def account(self) -> MyPlexAccount:
        if self._account is None:
            self._account = _wrap(MyPlexAccount)(token=self._admin_token, timeout=TIMEOUT)
        return self._account

    @_wrap
    def server_info(self) -> ServerInfo:
        srv = self.admin_server
        owned = any(r.clientIdentifier == srv.machineIdentifier and r.owned
                    for r in self.account.resources())
        sections = [MusicSection(key=str(s.key), title=s.title)
                    for s in srv.library.sections() if s.type == "artist"]
        return ServerInfo(name=srv.friendlyName, machine_id=srv.machineIdentifier,
                          version=srv.version, owner_is_signed_in_account=owned,
                          music_sections=sections)

    @_wrap
    def home_users(self) -> list[HomeUserInfo]:
        acct = self.account
        users = [HomeUserInfo(id=int(acct.id), title=acct.title or acct.username, thumb=acct.thumb,
                              has_pin=bool(getattr(acct, "protected", False)), is_admin=True)]
        for u in acct.users():
            if u.home:
                users.append(HomeUserInfo(id=int(u.id), title=u.title, thumb=u.thumb,
                                          has_pin=bool(u.protected), is_admin=False))
        return users

    def _find_user(self, user_id: int):
        for u in self.account.users():
            if int(u.id) == user_id:
                return u
        raise PlexError(f"Home user {user_id} not found on plex.tv")

    @_wrap
    def user_token(self, user_id: int) -> str:
        """Server token for a Home user, fetched with the admin account. No PIN needed (proven in M0)."""
        if user_id == int(self.account.id):
            return self._admin_token
        token = self._find_user(user_id).get_token(self.admin_server.machineIdentifier)
        if not token:
            raise PlexError("plex.tv returned no server token for this user")
        return token

    @_wrap
    def user_token_with_pin(self, user_id: int, pin: str) -> str:
        """Fallback: switch to the Home user with their PIN, then read their server token."""
        user_acct = self.account.switchHomeUser(self._find_user(user_id), pin=pin)
        machine_id = self.admin_server.machineIdentifier
        for res in user_acct.resources():
            if res.clientIdentifier == machine_id and res.accessToken:
                return res.accessToken
        raise PlexError("The user has no access to this server")

    # -- user side -------------------------------------------------------------
    def _user_server(self, user_token: str) -> PlexServer:
        return PlexServer(self.url, user_token, timeout=TIMEOUT)

    @_wrap
    def playlists(self, user_token: str) -> list[PlaylistSummary]:
        out = [PlaylistSummary(key=int(p.ratingKey), title=p.title, track_count=int(p.leafCount or 0),
                               updated_at=p.updatedAt, smart=bool(p.smart))
               for p in self._user_server(user_token).playlists(playlistType="audio")]
        return sorted(out, key=lambda p: p.title.lower())

    @_wrap
    def playlist_tracks(self, user_token: str, playlist_key: int) -> list[Track]:
        pl = self._user_server(user_token).fetchItem(playlist_key)
        return [Track(key=int(t.ratingKey), title=t.title,
                      artist=t.originalTitle or t.grandparentTitle or "",
                      album=t.parentTitle or "", duration_ms=t.duration)
                for t in pl.items()]
