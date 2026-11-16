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
from plexapi.playlist import Playlist
from plexapi.server import PlexServer

from ..sync import planner

TIMEOUT = 15
CHUNK = 100  # ratingKeys per request, keeps URLs a sensible length


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
    def playlist(self, user_token: str, playlist_key: int) -> PlaylistSummary | None: ...
    def playlist_track_keys(self, user_token: str, playlist_key: int) -> list[int]: ...
    def create_playlist(self, user_token: str, title: str, track_keys: list[int]) -> PlaylistSummary: ...
    def set_playlist_tracks(self, user_token: str, playlist_key: int, track_keys: list[int]) -> list[int]: ...
    def delete_playlist(self, user_token: str, playlist_key: int) -> None: ...
    def tracks_info(self, track_keys: list[int]) -> list[Track]: ...


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
        out = [_summary(p) for p in self._user_server(user_token).playlists(playlistType="audio")]
        return sorted(out, key=lambda p: p.title.lower())

    @_wrap
    def playlist_tracks(self, user_token: str, playlist_key: int) -> list[Track]:
        return [_track(t) for t in self._fetch_playlist(user_token, playlist_key).items()]

    @_wrap
    def playlist(self, user_token: str, playlist_key: int) -> PlaylistSummary | None:
        """Summary of one of the user's playlists, or None if it no longer exists."""
        for p in self._user_server(user_token).playlists(playlistType="audio"):
            if int(p.ratingKey) == playlist_key:
                return _summary(p)
        return None

    @_wrap
    def playlist_track_keys(self, user_token: str, playlist_key: int) -> list[int]:
        return [int(i.ratingKey) for i in self._fetch_playlist(user_token, playlist_key).items()]

    @_wrap
    def create_playlist(self, user_token: str, title: str, track_keys: list[int]) -> PlaylistSummary:
        """Create a playlist for the user. Plex needs at least one track to create one,
        so it is created with the first track the user can access, then filled in."""
        if not track_keys:
            raise PlexError("Can't create an empty playlist")
        srv = self._user_server(user_token)
        pl = None
        for k in track_keys:
            uri = f"{srv._uriRoot()}/library/metadata/{k}"
            args = {"uri": uri, "type": "audio", "title": title, "smart": 0}
            try:
                data = srv.query(f"/playlists{_join(args)}", method=srv._session.post)[0]
            except (BadRequest, NotFound):
                continue  # track not accessible to this user; try the next
            pl = Playlist(srv, data, initpath="/playlists")
            break
        if pl is None:
            raise PlexError("This user can't access any of the playlist's tracks")
        self._set_tracks(srv, pl, track_keys)
        return _summary(srv.fetchItem(int(pl.ratingKey)))

    @_wrap
    def set_playlist_tracks(self, user_token: str, playlist_key: int, track_keys: list[int]) -> list[int]:
        """Make the playlist hold exactly track_keys, in order. Returns tracks that
        couldn't be added because the user can't access them."""
        srv = self._user_server(user_token)
        return self._set_tracks(srv, srv.fetchItem(playlist_key), track_keys)

    @_wrap
    def delete_playlist(self, user_token: str, playlist_key: int) -> None:
        self._fetch_playlist(user_token, playlist_key).delete()

    @_wrap
    def tracks_info(self, track_keys: list[int]) -> list[Track]:
        """Track details via the admin, who can see every library."""
        out = []
        for i in range(0, len(track_keys), CHUNK):
            chunk = ",".join(str(k) for k in track_keys[i:i + CHUNK])
            out += [_track(t) for t in self.admin_server.fetchItems(f"/library/metadata/{chunk}")]
        return out

    # -- helpers ---------------------------------------------------------------------
    def _fetch_playlist(self, user_token: str, playlist_key: int) -> Playlist:
        pl = self._user_server(user_token).fetchItem(playlist_key)
        if not isinstance(pl, Playlist) or pl.playlistType != "audio" or pl.smart:
            raise PlexError("Not a regular music playlist")
        return pl

    def _set_tracks(self, srv: PlexServer, pl: Playlist, desired: list[int]) -> list[int]:
        desired = planner.dedupe(desired)
        wanted = set(desired)

        # 1. Remove tracks that shouldn't be there, and any duplicates (keep the first).
        seen: set[int] = set()
        for item in self._fresh_items(srv, pl):
            k = int(item.ratingKey)
            if k in wanted and k not in seen:
                seen.add(k)
            else:
                srv.query(f"{pl.key}/items/{item.playlistItemID}", method=srv._session.delete)

        # 2. Append missing tracks by ratingKey. Plex ignores ones the user can't access.
        adds = [k for k in desired if k not in seen]
        for i in range(0, len(adds), CHUNK):
            self._add_keys(srv, pl, adds[i:i + CHUNK])
        if adds:
            # If Plex dropped a whole batch because of one inaccessible track, retry the
            # missing ones individually. Anything still missing is reported as skipped.
            present_now = {int(i.ratingKey) for i in self._fresh_items(srv, pl)}
            for k in adds:
                if k not in present_now:
                    self._add_keys(srv, pl, [k])

        # 3. Reorder, using what is really there now.
        items = self._fresh_items(srv, pl)
        item_ids = {int(i.ratingKey): i.playlistItemID for i in items}
        present = [k for k in desired if k in item_ids]
        current = [int(i.ratingKey) for i in items]
        for key, after in planner.moves(current, present):
            path = f"{pl.key}/items/{item_ids[key]}/move"
            if after is not None:
                path += f"?after={item_ids[after]}"
            srv.query(path, method=srv._session.put)

        return [k for k in desired if k not in item_ids]

    @staticmethod
    def _add_keys(srv: PlexServer, pl: Playlist, keys: list[int]) -> None:
        uri = f"{srv._uriRoot()}/library/metadata/{','.join(str(k) for k in keys)}"
        try:
            srv.query(f"{pl.key}/items{_join({'uri': uri})}", method=srv._session.put)
        except (BadRequest, NotFound):
            if len(keys) > 1:
                for k in keys:
                    PlexApiGateway._add_keys(srv, pl, [k])

    @staticmethod
    def _fresh_items(srv: PlexServer, pl: Playlist) -> list:
        """The playlist's items, read from the server (plexapi caches pl.items())."""
        return srv.fetchItems(f"{pl.key}/items")


def _join(args: dict) -> str:
    from plexapi import utils
    return utils.joinArgs(args)


def _summary(p) -> PlaylistSummary:
    # plexapi returns naive local times; make them timezone-aware for storage and comparison.
    updated = p.updatedAt.astimezone() if p.updatedAt else None
    return PlaylistSummary(key=int(p.ratingKey), title=p.title, track_count=int(p.leafCount or 0),
                           updated_at=updated, smart=bool(p.smart))


def _track(t) -> Track:
    return Track(key=int(t.ratingKey), title=t.title, artist=t.originalTitle or t.grandparentTitle or "",
                 album=t.parentTitle or "", duration_ms=t.duration)
