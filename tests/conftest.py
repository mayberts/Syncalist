from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from syncalist.config import Config
from syncalist.plex.auth import AccountInfo, Pin
from syncalist.plex.gateway import (HomeUserInfo, MusicSection, PlaylistMeta, PlaylistSummary, PlexError,
                                    RemovalRefused, ServerInfo, TokenRejected, Track)
from syncalist.sync import planner
from syncalist.web.app import create_app

ADMIN_ID, KATE_ID, CLEO_ID = 1, 584691038, 129051550


class FakeAuth:
    """Stands in for plex.tv sign-in. Set .token / .account to control the result."""

    def __init__(self):
        self.token = "admin-token"
        self.account_info = AccountInfo(id=ADMIN_ID, title="Nick", username="nick")

    def create_pin(self, client_id):
        return Pin(id=42, code="abcd")

    def auth_url(self, client_id, pin, forward_url):
        return f"https://app.plex.tv/auth#?code={pin.code}&forwardUrl={forward_url}"

    def check_pin(self, client_id, pin_id):
        return self.token

    def account(self, client_id, token):
        return self.account_info


class FakePlex:
    """One fake Plex server shared by every gateway the app creates.

    playlists[user_id][key] = {"title", "smart", "items": [ratingKeys], "updated": datetime}
    """

    def __init__(self):
        self.owner = True
        self.sections = [MusicSection("3", "Music")]
        self.tokens_issued = 0
        self.rejected_tokens: set[str] = set()
        self.no_pin_fails_for: set[int] = set()
        self.pins = {KATE_ID: "1234"}
        self.users = [
            HomeUserInfo(ADMIN_ID, "Nick", None, False, True),
            HomeUserInfo(KATE_ID, "Kate", None, True, False),
            HomeUserInfo(CLEO_ID, "Cleo", None, False, False),
        ]
        self.catalog = {k: Track(k, f"Song {k}", f"Artist {k % 7}", f"Album {k % 5}", 200000 + k)
                        for k in range(1, 200)}
        self.catalog[143610] = Track(143610, "Trapped", "2Pac", "2Pacalypse Now", 285000)
        self.catalog[143611] = Track(143611, "Soulja’s Story", "2Pac", "2Pacalypse Now", 305000)
        self.no_access: dict[int, set[int]] = {}  # user -> track keys they can't see
        self.short_reads: dict[int, int] = {}  # playlist key -> number of tracks a read returns (simulated bug)
        self.hidden_count: dict[int, int] = {}  # playlist key -> leafCount Plex reports (simulated restrictions)
        self.clock = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
        self.next_key = 1000
        self.playlists: dict[int, dict[int, dict]] = {ADMIN_ID: {}, KATE_ID: {}, CLEO_ID: {}}
        self.add_playlist(ADMIN_ID, "Admin Mix", [1, 2], key=10)
        self.add_playlist(ADMIN_ID, "Road Trip", [1, 2, 3, 4], key=11)
        self.add_playlist(ADMIN_ID, "All Music", [1, 2, 3], key=12, smart=True)
        self.add_playlist(KATE_ID, "Kate's favourites", [143610, 143611], key=309551)
        self.add_playlist(KATE_ID, "Road Trip", [3, 5, 6], key=253611)

    def edit(self, user_id, key, items=None, title=None):
        """Simulate a person editing their playlist in a Plex app."""
        pl = self.playlists[user_id][key]
        if items is not None:
            pl["items"] = list(items)
        if title is not None:
            pl["title"] = title
        pl["updated"] = self.tick()

    def set_image(self, user_id, key, kind, data):
        """Simulate someone choosing a custom poster/background in a Plex app."""
        self.image_n = getattr(self, "image_n", 0) + 1
        path = f"/library/metadata/{key}/{'thumb' if kind == 'poster' else 'art'}/{self.image_n}"
        self.images = getattr(self, "images", {})
        self.images[path] = data
        self.playlists[user_id][key][kind] = path
        self.playlists[user_id][key]["updated"] = self.tick()
        return path

    def image_of(self, user_id, key, kind):
        path = self.playlists[user_id][key][kind]
        return getattr(self, "images", {}).get(path) if path else None

    def tick(self):
        self.clock += timedelta(seconds=1)
        return self.clock

    def add_playlist(self, user_id, title, items, key=None, smart=False):
        key = key or self.next_key
        self.next_key = max(self.next_key, key) + 1
        self.playlists[user_id][key] = {"title": title, "smart": smart, "items": list(items), "updated": self.tick(),
                                        "poster": None, "art": None, "summary": ""}
        return key

    def items(self, user_id, key):
        return self.playlists[user_id][key]["items"]

    def user_for_token(self, token):
        if token in self.rejected_tokens:
            raise TokenRejected("401")
        if token == "admin-token":
            return ADMIN_ID
        return int(token.split(":")[1])


class FakeGateway:
    def __init__(self, plex: FakePlex, url: str, admin_token: str):
        self.plex, self.url, self.admin_token = plex, url, admin_token

    def server_info(self):
        if "unreachable" in self.url:
            raise PlexError("connection refused")
        return ServerInfo("Delta", "machine-1", "1.43", self.plex.owner, self.plex.sections)

    def home_users(self):
        return list(self.plex.users)

    def user_token(self, user_id):
        if user_id == ADMIN_ID:
            return "admin-token"
        if user_id in self.plex.no_pin_fails_for:
            raise PlexError("no token")
        self.plex.tokens_issued += 1
        return f"user:{user_id}:{self.plex.tokens_issued}"

    def user_token_with_pin(self, user_id, pin):
        if self.plex.pins.get(user_id) != pin:
            raise PlexError("wrong PIN")
        self.plex.tokens_issued += 1
        return f"user:{user_id}:{self.plex.tokens_issued}"

    # -- reads ----------------------------------------------------------------------
    def _pl(self, token, key):
        uid = self.plex.user_for_token(token)
        pl = self.plex.playlists[uid].get(key)
        if pl is None:
            raise PlexError("404 not found")
        return uid, pl

    def _summary(self, key, pl):
        count = self.plex.hidden_count.get(key, len(pl["items"]))
        return PlaylistSummary(key, pl["title"], count, pl["updated"], pl["smart"])

    def playlists(self, token):
        uid = self.plex.user_for_token(token)
        return sorted((self._summary(k, p) for k, p in self.plex.playlists[uid].items()),
                      key=lambda p: p.title.lower())

    def playlist(self, token, key):
        uid = self.plex.user_for_token(token)
        pl = self.plex.playlists[uid].get(key)
        return self._summary(key, pl) if pl else None

    def playlist_tracks(self, token, key):
        _, pl = self._pl(token, key)
        return [self.plex.catalog[k] for k in pl["items"]]

    def playlist_track_keys(self, token, key):
        items = list(self._pl(token, key)[1]["items"])
        if key in self.plex.short_reads:  # the real gateway detects this via totalSize and raises
            raise PlexError(f"Plex returned {self.plex.short_reads[key]} of {len(items)} tracks")
        return items

    def tracks_info(self, keys):
        return [self.plex.catalog[k] for k in keys if k in self.plex.catalog]

    # -- writes ---------------------------------------------------------------------
    def _accessible(self, uid, keys):
        return [k for k in keys if k in self.plex.catalog and k not in self.plex.no_access.get(uid, set())]

    def create_playlist(self, token, title, keys):
        uid = self.plex.user_for_token(token)
        ok = self._accessible(uid, planner.dedupe(keys))
        if not ok:
            raise PlexError("This user can't access any of the playlist's tracks")
        key = self.plex.add_playlist(uid, title, [])
        self.set_playlist_tracks(token, key, keys, allow_removals=False)
        return self.playlist(token, key)

    def set_playlist_tracks(self, token, key, keys, allow_removals=False):
        uid, pl = self._pl(token, key)
        desired = planner.dedupe(keys)
        ok = self._accessible(uid, desired)
        if not allow_removals and any(k not in set(desired) for k in pl["items"]):
            raise RemovalRefused("Stopped before changing anything: this would have removed tracks")
        if pl["items"] != ok:
            p = planner.plan(pl["items"], ok)
            pl["items"] = planner.apply(pl["items"], p)
            assert pl["items"] == ok
            pl["updated"] = self.plex.tick()
        return [k for k in desired if k not in ok]

    def debug_playlist(self, token, key):
        _, pl = self._pl(token, key)
        return {"playlist_key": key, "items()": {"count": len(pl["items"])}}

    def rename_playlist(self, token, key, title):
        _, pl = self._pl(token, key)
        pl["title"] = title
        pl["updated"] = self.plex.tick()

    def playlist_meta(self, token, key):
        _, pl = self._pl(token, key)
        return PlaylistMeta(pl["poster"], pl["art"], pl["summary"])

    def download_image(self, token, path):
        self.plex.user_for_token(token)
        self.plex.downloads = getattr(self.plex, "downloads", 0) + 1
        return self.plex.images[path]

    def upload_image(self, token, key, kind, data):
        uid, _ = self._pl(token, key)
        self.plex.uploads = getattr(self.plex, "uploads", 0) + 1
        self.plex.set_image(uid, key, kind, data)

    def set_summary(self, token, key, summary):
        _, pl = self._pl(token, key)
        pl["summary"] = summary
        pl["updated"] = self.plex.tick()

    def delete_playlist(self, token, key):
        uid, _ = self._pl(token, key)
        del self.plex.playlists[uid][key]


@pytest.fixture
def plex():
    return FakePlex()


@pytest.fixture
def auth():
    return FakeAuth()


@pytest.fixture
def app(tmp_path, plex, auth):
    config = Config(data_dir=tmp_path, plex_url="http://plex:32400", sync_interval=60)
    return create_app(config, gateway_factory=lambda url, tok: FakeGateway(plex, url, tok), plex_auth=auth,
                      start_scheduler=False)


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


def sign_in(client):
    r = client.get("/auth/start")
    assert r.status_code == 303 and r.headers["location"].startswith("https://app.plex.tv/auth")
    return client.get("/auth/callback")


@pytest.fixture
def configured(client):
    """A client that has signed in and finished setup."""
    sign_in(client)
    r = client.post("/setup", data={"plex_url": "http://plex:32400"})
    assert r.status_code == 303 and r.headers["location"] == "/", r.text
    return client
