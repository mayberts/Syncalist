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
    backups = configured.get("/backups").text
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
    assert "Stopped sharing" in configured.get("/backups").text


def test_skipped_tracks_are_listed(configured, plex):
    plex.no_access[CLEO_ID] = {2}
    gid = configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 10,
                                          "recipient": CLEO_ID}).headers["location"]
    page = configured.get(gid).text
    assert "1 skipped" in page and "Song 2 – Artist 2" in page


def test_restore_backup(configured, plex):
    configured.post("/share", data={"user_id": ADMIN_ID, "playlist_key": 11, "recipient": KATE_ID,
                                    f"mode_{KATE_ID}": "replace"})
    page = configured.get("/backups").text
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
