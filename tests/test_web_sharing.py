from conftest import ADMIN_ID, CLEO_ID, KATE_ID


def test_dashboard_offers_share_except_smart(configured):
    page = configured.get("/").text
    assert f"/share?user_id={ADMIN_ID}&playlist_key=10" in page
    assert f"playlist_key=12" not in page  # smart "All Music"


def test_share_page_asks_about_same_name(configured):
    page = configured.get(f"/share?user_id={ADMIN_ID}&playlist_key=11").text
    assert "Kate already has “Road Trip” (3 tracks)" in page
    assert f'name="mode_{KATE_ID}" value="merge" checked' in page
    assert "A new playlist called “Road Trip” will be created" in page  # Cleo


def test_share_merge_end_to_end(configured, plex):
    r = configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 11,
                                        "recipient": [KATE_ID, CLEO_ID], f"mode_{KATE_ID}": "merge",
                                        f"mode_{CLEO_ID}": "new"})
    assert r.status_code == 303 and r.headers["location"].startswith("/groups/")
    group_page = configured.get(r.headers["location"]).text
    assert "Shared “Road Trip”" in group_page and "6 tracks" in group_page
    assert group_page.count(">OK<") == 3
    assert plex.items(KATE_ID, 253611) == [1, 2, 3, 4, 5, 6]
    dash = configured.get("/").text
    assert "Shared playlists" in dash and 'class="tag shared"' in dash
    backups = configured.get("/history").text
    assert "Before merge into shared" in backups


def test_share_with_nobody_selected_shows_error(configured):
    r = configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 10})
    assert r.status_code == 303
    assert "Pick at least one person" in configured.get(r.headers["location"]).text


def test_already_shared_redirects_to_group(configured):
    r = configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 10, "recipient": CLEO_ID})
    gid = r.headers["location"]
    r = configured.get(f"/share?user_id={ADMIN_ID}&playlist_key=10")
    assert r.status_code == 303 and r.headers["location"] == gid


def test_add_remove_and_stop(configured, plex):
    gid = configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 10,
                                          "recipient": CLEO_ID}).headers["location"]
    configured.post(f"{gid}/members", data={"recipient": KATE_ID, f"mode_{KATE_ID}": "new"})
    page = configured.get(gid).text
    assert page.count(">OK<") == 3
    kate_copies = [k for k, p in plex.playlists[KATE_ID].items() if p["title"] == "Admin Mix"]
    assert len(kate_copies) == 1

    configured.post(f"{gid}/members/{KATE_ID}/remove", data={"delete_copy": "1"})
    assert kate_copies[0] not in plex.playlists[KATE_ID]
    assert configured.get(gid).text.count(">OK<") == 2

    configured.post(f"{gid}/stop")
    assert configured.get(gid).status_code == 404
    assert "Stopped sharing" in configured.get("/history").text


def test_skipped_tracks_are_listed(configured, plex):
    plex.no_access[CLEO_ID] = {2}
    gid = configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 10,
                                          "recipient": CLEO_ID}).headers["location"]
    page = configured.get(gid).text
    assert "1 skipped" in page and "Song 2 – Artist 2" in page


def test_restore_backup(configured, plex):
    configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 11, "recipient": KATE_ID,
                                    f"mode_{KATE_ID}": "replace"})
    page = configured.get("/history").text
    assert "Before replace into shared" in page
    import re
    backup_id = re.search(r'/backups/(\d+)/restore', page).group(1)
    configured.post(f"/backups/{backup_id}/restore")
    restored = [p for p in plex.playlists[KATE_ID].values() if p["title"].startswith("Road Trip (restored")]
    assert len(restored) == 1 and restored[0]["items"] == [3, 5, 6]


def test_debug_playlist_page(configured):
    r = configured.get(f"/debug/playlist?user_id={KATE_ID}&playlist_key=253611")
    assert r.status_code == 200 and r.json()["items()"]["count"] == 3 and "token" not in r.text.lower()


def test_sync_now_apply_and_undo_buttons(configured, plex):
    gid = configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 11, "recipient": KATE_ID,
                                          f"mode_{KATE_ID}": "replace"}).headers["location"]
    page = configured.get(gid).text
    assert "Sync now" in page and "aren&#39;t copied" not in page
    plex.edit(KATE_ID, 253611, [1, 2, 3, 4, 9])
    configured.post(f"{gid}/sync")
    assert plex.items(ADMIN_ID, 11) == [1, 2, 3, 4, 9]
    assert "Kate added" in configured.get(gid).text

    plex.add_playlist(ADMIN_ID, "Big", list(range(1, 21)), key=50)
    gid2 = configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 50,
                                           "recipient": CLEO_ID}).headers["location"]
    cleo_key = [k for k, p in plex.playlists[CLEO_ID].items() if p["title"] == "Big"][0]
    plex.edit(CLEO_ID, cleo_key, [1])
    configured.post(f"{gid2}/sync")
    page = configured.get(gid2).text
    assert "Syncing is paused" in page and "Paused: needs you" in configured.get("/").text
    configured.post(f"{gid2}/undo")
    assert plex.items(CLEO_ID, cleo_key) == list(range(1, 21))
    assert "Syncing is paused" not in configured.get(gid2).text


def test_scheduler_round_runs_against_configured_app(configured, app, plex):
    configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 11, "recipient": KATE_ID,
                                    f"mode_{KATE_ID}": "replace"})
    plex.edit(KATE_ID, 253611, [4, 3, 2, 1])
    app.state.run_sync_round()
    assert plex.items(ADMIN_ID, 11) == [4, 3, 2, 1]


def test_sync_all_and_status_line(configured, plex, app):
    assert "Sync all" not in configured.get("/").text  # nothing shared yet
    configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 11, "recipient": KATE_ID,
                                    f"mode_{KATE_ID}": "replace"})
    page = configured.get("/").text
    assert "Sync all" in page and "Waiting for the first sync round" in page

    plex.edit(KATE_ID, 253611, [1, 2, 3, 4, 9])
    r = configured.post("/sync")
    assert r.status_code == 303
    page = configured.get("/").text
    assert "Synced: 1 shared playlist checked, 1 updated." in page
    assert "Last full sync just now: 1 shared playlist checked, 1 updated." in page
    assert plex.items(ADMIN_ID, 11) == [1, 2, 3, 4, 9]

    app.state.run_sync_round()  # a scheduled round with nothing to do
    assert "1 shared playlist checked, no changes." in configured.get("/").text


def test_attention_badge(configured, plex):
    plex.add_playlist(ADMIN_ID, "Big", list(range(1, 21)), key=50)
    configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 50, "recipient": CLEO_ID})
    assert 'class="badge"' not in configured.get("/settings").text
    cleo_key = [k for k, p in plex.playlists[CLEO_ID].items() if p["title"] == "Big"][0]
    plex.edit(CLEO_ID, cleo_key, [1])
    configured.post("/sync")
    for page in ("/", "/settings", "/history"):
        text = configured.get(page).text
        assert 'class="badge"' in text and ">1</span>" in text
    assert "1 shared playlist needs attention." in configured.get("/").text
    assert "1 paused" in configured.get("/").text


def test_failed_round_is_shown(configured, plex, monkeypatch):
    configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 10, "recipient": CLEO_ID})
    import syncalist.web.app as appmod
    monkeypatch.setattr(appmod.SyncService, "sync_all",
                        lambda self: (_ for _ in ()).throw(RuntimeError("Plex unreachable <html>x</html>")))
    configured.post("/sync")
    page = configured.get("/").text
    assert "Sync failed: Plex unreachable" in page and "Sync round failed: Plex unreachable" in page
    assert "&lt;html" not in page


def test_backups_url_redirects_to_history(configured):
    r = configured.get("/backups")
    assert r.status_code == 303 and r.headers["location"] == "/history#backups"
    assert 'href="/history">History</a>' in configured.get("/").text


def test_sync_round_is_grouped_with_details_folded(configured, plex):
    gid = configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 11, "recipient": [KATE_ID, CLEO_ID],
                                          f"mode_{KATE_ID}": "replace", f"mode_{CLEO_ID}": "new"}).headers["location"]
    plex.edit(KATE_ID, 253611, [1, 2, 3, 4, 9])
    configured.post(f"{gid}/sync")
    page = configured.get(gid).text
    import re
    # the round's headline, with the two per-copy writes folded beneath it
    block = re.search(r"Kate added “Song 9”.*?</li>", page, re.S).group(0)
    assert "2 copies updated</summary>" in block
    assert "Updated Nick&#39;s copy: +1 −0 (4 → 5 tracks)" in block
    assert f'href="/history?group={gid.rsplit("/", 1)[1]}"' in page


def test_history_filters_and_paging(configured, plex):
    plex.no_access[CLEO_ID] = {2}
    configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 10, "recipient": CLEO_ID})
    everything = configured.get("/history").text
    assert "Created “Admin Mix” for Cleo" in everything and "skipped for Cleo" in everything
    problems = configured.get("/history?show=problems").text
    assert "skipped for Cleo" in problems and "Created “Admin Mix” for Cleo" not in problems

    import syncalist.web.app as appmod
    from sqlmodel import Session
    from syncalist.models import SyncEvent
    with Session(configured.app.state.engine) as s:
        for i in range(60):
            s.add(SyncEvent(message=f"filler {i}"))
        s.commit()
    first = configured.get("/history").text
    assert "Show older" in first and "filler 59" in first and "filler 5<" not in first
    second = configured.get("/history?page=2").text
    assert "← Newer" in second and "Created “Admin Mix” for Cleo" in second


def test_retention_setting_prunes_old_entries(configured):
    from datetime import timedelta
    from sqlmodel import Session, select
    from syncalist.models import SyncEvent, utcnow
    with Session(configured.app.state.engine) as s:
        s.add(SyncEvent(message="ancient", at=utcnow() - timedelta(days=40)))
        s.add(SyncEvent(message="recent", at=utcnow() - timedelta(days=5)))
        s.commit()
    configured.post("/settings/retention", data={"days": "3"})
    assert "between 7 and 3650" in configured.get("/settings").text
    configured.post("/settings/retention", data={"days": "30"})
    page = configured.get("/settings").text
    assert "kept for 30 days; removed 1 older entries" in page and 'value="30"' in page
    with Session(configured.app.state.engine) as s:
        msgs = {e.message for e in s.exec(select(SyncEvent)).all()}
    assert "ancient" not in msgs and "recent" in msgs


def test_share_applies_owner_artwork_and_page_shows_it(configured, plex):
    plex.set_image(ADMIN_ID, 10, "poster", b"\x89PNG-poster")
    plex.playlists[ADMIN_ID][10]["summary"] = "Nick's mix"
    gid = configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 10,
                                          "recipient": CLEO_ID}).headers["location"]
    cleo_key = [k for k, p in plex.playlists[CLEO_ID].items() if p["title"] == "Admin Mix"][0]
    assert plex.image_of(CLEO_ID, cleo_key, "poster") == b"\x89PNG-poster"  # applied at share time
    assert plex.playlists[CLEO_ID][cleo_key]["summary"] == "Nick's mix"
    page = configured.get(gid).text
    assert f'src="{gid}/image/poster"' in page and "Nick&#39;s mix" in page
    img = configured.get(f"{gid}/image/poster")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content == b"\x89PNG-poster"
    assert configured.get(f"{gid}/image/art").status_code == 404


def test_dashboard_shows_playlist_thumbnails(configured, plex):
    plex.set_image(ADMIN_ID, 11, "poster", b"P")
    configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 11, "recipient": KATE_ID,
                                    f"mode_{KATE_ID}": "replace"})
    page = configured.get("/").text
    poster_path = plex.playlists[ADMIN_ID][11]["poster"]
    from urllib.parse import quote
    assert f'/users/{ADMIN_ID}/thumb?path={quote(poster_path, safe="/")}' in page  # custom poster
    assert f'/users/{ADMIN_ID}/thumb?path=/playlists/10/composite/' in page  # Plex's collage
    assert page.count('class="thumb"') >= 6
    r = configured.get(f"/users/{ADMIN_ID}/thumb", params={"path": poster_path})
    assert r.status_code == 200 and r.content == f"thumb:96:{poster_path}".encode()
    assert "max-age" in r.headers["cache-control"]
    assert configured.get(f"/users/{ADMIN_ID}/thumb", params={"path": "http://evil/x"}).status_code == 400
