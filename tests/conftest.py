from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from syncalist.config import Config
from syncalist.plex.auth import AccountInfo, Pin
from syncalist.plex.gateway import (HomeUserInfo, MusicSection, PlaylistSummary, PlexError, ServerInfo,
                                    TokenRejected, Track)
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
    """One fake Plex server shared by every gateway the app creates."""

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
        self.playlists = {
            ADMIN_ID: [PlaylistSummary(10, "Admin Mix", 2, datetime(2026, 11, 1, 10, 0), False)],
            KATE_ID: [PlaylistSummary(309551, "Kate's favourites", 2, datetime(2026, 11, 12, 10, 27), False),
                      PlaylistSummary(253611, "Road Trip", 1, datetime(2026, 8, 25, 14, 45), False)],
            CLEO_ID: [],
        }
        self.tracks = {
            309551: [Track(143610, "Trapped", "2Pac", "2Pacalypse Now", 285000),
                     Track(143611, "Soulja’s Story", "2Pac", "2Pacalypse Now", 305000)],
        }

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

    def playlists(self, token):
        return self.plex.playlists[self.plex.user_for_token(token)]

    def playlist_tracks(self, token, key):
        self.plex.user_for_token(token)
        return self.plex.tracks.get(key, [])


@pytest.fixture
def plex():
    return FakePlex()


@pytest.fixture
def auth():
    return FakeAuth()


@pytest.fixture
def app(tmp_path, plex, auth):
    config = Config(data_dir=tmp_path, plex_url="http://plex:32400", sync_interval=60)
    return create_app(config, gateway_factory=lambda url, tok: FakeGateway(plex, url, tok), plex_auth=auth)


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
