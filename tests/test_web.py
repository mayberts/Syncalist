from conftest import ADMIN_ID, CLEO_ID, KATE_ID, sign_in

from syncalist.models import HomeUser
from syncalist.plex.auth import AccountInfo
from syncalist.plex.gateway import MusicSection


def test_healthz(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_unauthenticated_redirects_to_login(client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "Sign in with Plex" in client.get("/login").text


def test_first_sign_in_goes_to_setup_with_env_default(client):
    r = sign_in(client)
    assert r.headers["location"] == "/setup"
    page = client.get("/setup").text
    assert 'value="http://plex:32400"' in page


def test_waiting_page_while_plex_sign_in_pending(client, auth):
    auth.token = None
    client.get("/auth/start")
    r = client.get("/auth/callback")
    assert r.status_code == 200 and "Waiting for Plex" in r.text


def test_setup_rejects_non_owner(client, plex):
    plex.owner = False
    sign_in(client)
    r = client.post("/setup", data={"plex_url": "http://plex:32400"})
    assert r.status_code == 400 and "doesn&#39;t own" in r.text
    assert client.get("/").headers["location"] == "/setup"


def test_setup_reports_unreachable_server(client):
    sign_in(client)
    r = client.post("/setup", data={"plex_url": "unreachable:32400"})
    assert r.status_code == 400 and "http://unreachable:32400" in r.text


def test_setup_asks_for_library_when_several(client, plex):
    plex.sections = [MusicSection("3", "Music"), MusicSection("7", "Audiobooks")]
    sign_in(client)
    r = client.post("/setup", data={"plex_url": "http://plex:32400"})
    assert r.headers["location"] == "/setup/library"
    assert "Audiobooks" in client.get("/setup/library").text
    r = client.post("/setup/library", data={"section_key": "7"})
    assert r.headers["location"] == "/"
    assert "Audiobooks library" in client.get("/").text


def test_dashboard_lists_each_users_playlists(configured):
    page = configured.get("/").text
    for text in ("Nick", "Admin Mix", "Kate", "Kate&#39;s favourites", "Road Trip", "Cleo", "No music playlists"):
        assert text in page


def test_playlist_detail(configured):
    page = configured.get(f"/users/{KATE_ID}/playlists/309551").text
    assert "Trapped" in page and "Soulja’s Story" in page and "4:45" in page


def test_playlist_detail_for_another_users_playlist_is_404(configured):
    assert configured.get(f"/users/{CLEO_ID}/playlists/309551").status_code == 404


def test_other_account_cannot_sign_in(configured, app, auth):
    configured.post("/logout")
    auth.account_info = AccountInfo(id=999, title="Neighbour", username="n")
    r = sign_in(configured)
    assert r.status_code == 403 and "Only the owner" in r.text
    assert configured.get("/").headers["location"] == "/login"


def test_rejected_token_is_refreshed(configured, plex):
    configured.get("/")  # caches Kate's token
    issued = plex.tokens_issued
    plex.rejected_tokens.update(f"user:{KATE_ID}:{n}" for n in range(1, issued + 1))
    page = configured.get("/").text
    assert "Road Trip" in page
    assert plex.tokens_issued > issued


def test_user_needs_attention_then_fallback_pin(configured, plex, app):
    configured.get("/")  # caches Kate's token
    plex.no_pin_fails_for.add(KATE_ID)
    plex.rejected_tokens.update(f"user:{KATE_ID}:{n}" for n in range(1, plex.tokens_issued + 1))
    page = configured.get("/").text
    assert "Couldn&#39;t get access" in page and "Admin Mix" in page  # others still shown

    r = configured.post(f"/users/{KATE_ID}/pin", data={"pin": "0000"})
    assert "didn&#39;t work" in configured.get("/settings").text
    configured.post(f"/users/{KATE_ID}/pin", data={"pin": "1234"})
    settings_page = configured.get("/settings").text
    assert "PIN saved for Kate" in settings_page
    assert "Road Trip" in configured.get("/").text


def test_poll_interval_validation(configured):
    configured.post("/settings", data={"poll_interval": "5"})
    assert "between 15 and 3600" in configured.get("/settings").text
    configured.post("/settings", data={"poll_interval": "120"})
    page = configured.get("/settings").text
    assert 'value="120"' in page


def test_admin_re_sign_in_stores_new_token(configured, auth, plex):
    configured.post("/logout")
    auth.token = "admin-token"  # same account, (possibly rotated) token is stored again
    r = sign_in(configured)
    assert r.headers["location"] == "/"
    assert configured.get("/").status_code == 200
