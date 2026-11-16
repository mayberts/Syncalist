import pytest
from conftest import ADMIN_ID, CLEO_ID, KATE_ID, FakeGateway
from sqlmodel import Session

from syncalist.crypto import Secrets
from syncalist.db import make_engine
from syncalist.models import HomeUser
from syncalist.sharing import Mode, ShareService
from syncalist.sync.engine import SyncService
from syncalist.users import UserService


@pytest.fixture
def env(tmp_path, plex):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    session = Session(engine)
    users = UserService(session, Secrets(tmp_path / "key"), FakeGateway(plex, "http://plex", "admin-token"))
    users.refresh()
    shares = ShareService(session, users)
    yield shares, SyncService(shares)
    session.close()


def shared_road_trip(shares, plex):
    """Admin's Road Trip [1,2,3,4] shared with Kate (replace) and Cleo (new copy)."""
    g = shares.share(shares.session.get(HomeUser, ADMIN_ID), 11, {KATE_ID: Mode.REPLACE, CLEO_ID: Mode.NEW})
    keys = {m.user_id: m.playlist_key for m in shares.members(g)}
    return g, keys


def all_items(plex, keys):
    return {uid: plex.items(uid, k) for uid, k in keys.items()}


def test_nothing_to_do(env, plex):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    before = {uid: plex.playlists[uid][k]["updated"] for uid, k in keys.items()}
    assert not sync.sync_group(g).changed
    assert {uid: plex.playlists[uid][k]["updated"] for uid, k in keys.items()} == before  # no writes


def test_add_reaches_everyone(env, plex):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    plex.edit(KATE_ID, keys[KATE_ID], [1, 2, 9, 3, 4])
    assert sync.sync_group(g).changed
    assert set(map(tuple, all_items(plex, keys).values())) == {(1, 2, 9, 3, 4)}
    assert any("Kate added “Song 9”" in e.message for e in shares.events(g))
    # A second round sees our own writes and does nothing (echo suppression).
    assert not sync.sync_group(g).changed


def test_concurrent_edits_merge(env, plex):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    plex.edit(KATE_ID, keys[KATE_ID], [1, 2, 3, 4, 8])  # Kate adds 8
    plex.edit(CLEO_ID, keys[CLEO_ID], [1, 3, 4])  # Cleo removes 2
    plex.edit(ADMIN_ID, 11, [4, 1, 2, 3])  # Nick moves 4 to the top (newest)
    sync.sync_group(g)
    assert set(map(tuple, all_items(plex, keys).values())) == {(4, 1, 3, 8)}


def test_rename_reaches_everyone(env, plex):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    plex.edit(CLEO_ID, keys[CLEO_ID], title="Summer Road Trip")
    sync.sync_group(g)
    assert {plex.playlists[uid][k]["title"] for uid, k in keys.items()} == {"Summer Road Trip"}
    assert g.name == "Summer Road Trip"


def test_deleted_copy_leaves_share_others_untouched(env, plex):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    del plex.playlists[CLEO_ID][keys[CLEO_ID]]
    sync.sync_group(g)
    assert {m.user_id for m in shares.members(g)} == {ADMIN_ID, KATE_ID}
    assert plex.items(ADMIN_ID, 11) == [1, 2, 3, 4]
    assert any("Cleo deleted their copy" in e.message for e in shares.events(g))


def test_skipped_tracks_not_treated_as_removals(env, plex):
    shares, sync = env
    plex.no_access[CLEO_ID] = {3}
    g, keys = shared_road_trip(shares, plex)
    assert plex.items(CLEO_ID, keys[CLEO_ID]) == [1, 2, 4]
    plex.edit(CLEO_ID, keys[CLEO_ID], [1, 2, 4, 9])  # Cleo adds 9
    sync.sync_group(g)
    assert plex.items(ADMIN_ID, 11) == [1, 2, 3, 4, 9]  # 3 kept for everyone else
    assert plex.items(CLEO_ID, keys[CLEO_ID]) == [1, 2, 4, 9]


def test_mass_removal_pauses_then_apply(env, plex):
    shares, sync = env
    plex.add_playlist(ADMIN_ID, "Big", list(range(1, 21)), key=50)
    g = shares.share(shares.session.get(HomeUser, ADMIN_ID), 50, {KATE_ID: Mode.NEW})
    kate_key = [m.playlist_key for m in shares.members(g) if m.user_id == KATE_ID][0]
    plex.edit(KATE_ID, kate_key, [1, 2])  # removes 18 of 20
    r = sync.sync_group(g)
    assert r.paused and g.paused_reason and "remove 18 of 20" in g.paused_reason
    assert len(plex.items(ADMIN_ID, 50)) == 20  # nothing applied
    assert sync.sync_group(g).paused  # stays paused on later rounds
    sync.sync_group(g, force=True)
    assert plex.items(ADMIN_ID, 50) == [1, 2] and g.paused_reason is None
    assert any(b.playlist_key == 50 and len(b.track_keys) == 20 for b in shares.backups())  # backed up


def test_mass_removal_undo(env, plex):
    shares, sync = env
    plex.add_playlist(ADMIN_ID, "Big", list(range(1, 21)), key=50)
    g = shares.share(shares.session.get(HomeUser, ADMIN_ID), 50, {KATE_ID: Mode.NEW})
    kate_key = [m.playlist_key for m in shares.members(g) if m.user_id == KATE_ID][0]
    plex.edit(KATE_ID, kate_key, [1, 2])
    sync.sync_group(g)
    sync.discard_pending(g)
    assert plex.items(KATE_ID, kate_key) == list(range(1, 21)) and g.paused_reason is None
    assert not sync.sync_group(g).changed


def test_member_editing_mid_sync_is_left_for_next_round(env, plex, monkeypatch):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    plex.edit(KATE_ID, keys[KATE_ID], [1, 2, 3, 4, 9])
    real_playlist = FakeGateway.playlist
    calls = {"n": 0}

    def playlist(self, token, key):
        # Cleo edits right after we first look at her copy
        if key == keys[CLEO_ID]:
            calls["n"] += 1
            if calls["n"] == 2:
                plex.edit(CLEO_ID, keys[CLEO_ID], [1, 2, 3, 4, 7])
        return real_playlist(self, token, key)
    monkeypatch.setattr(FakeGateway, "playlist", playlist)
    sync.sync_group(g)
    assert plex.items(CLEO_ID, keys[CLEO_ID]) == [1, 2, 3, 4, 7]  # her edit wasn't overwritten
    monkeypatch.setattr(FakeGateway, "playlist", real_playlist)
    sync.sync_group(g)
    assert set(map(tuple, all_items(plex, keys).values())) == {(1, 2, 3, 4, 9, 7)}


def test_unreadable_member_is_skipped_others_sync(env, plex):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    plex.edit(KATE_ID, keys[KATE_ID], [1, 2, 3, 4, 9])
    plex.edit(CLEO_ID, keys[CLEO_ID], [1, 2, 3])
    plex.short_reads[keys[CLEO_ID]] = 1
    sync.sync_group(g)
    assert plex.items(ADMIN_ID, 11) == [1, 2, 3, 4, 9]  # Cleo's removal not applied: couldn't read her
    assert plex.items(CLEO_ID, keys[CLEO_ID]) == [1, 2, 3]  # and her copy wasn't touched
    cleo = [m for m in shares.members(g) if m.user_id == CLEO_ID][0]
    assert cleo.status == "error"


def test_sync_all_keeps_going_after_a_failure(env, plex, monkeypatch):
    shares, sync = env
    g1, _ = shared_road_trip(shares, plex)
    g2 = shares.share(shares.session.get(HomeUser, ADMIN_ID), 10, {CLEO_ID: Mode.NEW})
    orig = sync.sync_group
    monkeypatch.setattr(sync, "sync_group", lambda g, force=False: (_ for _ in ()).throw(RuntimeError("boom"))
                        if g.id == g1.id else orig(g))
    reports = sync.sync_all()
    assert len(reports) == 2 and any("boom" in e.message for e in shares.events(g1))


def test_backups_are_pruned(env, plex):
    shares, sync = env
    user = shares.session.get(HomeUser, ADMIN_ID)
    summary = FakeGateway(plex, "", "admin-token").playlist("admin-token", 10)
    for i in range(15):
        shares._backup(user, summary, [i], reason=f"r{i}")
    kept = [b for b in shares.backups() if b.playlist_key == 10]
    assert len(kept) == 10 and {b.reason for b in kept} == {f"r{i}" for i in range(5, 15)}


def test_new_addition_is_never_removed_even_if_merge_drops_it(env, plex, monkeypatch):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    plex.edit(KATE_ID, keys[KATE_ID], [1, 2, 3, 4, 9])
    import syncalist.sync.engine as engine_mod
    real_merge = engine_mod.merge
    # Simulate an unknown bug that loses Kate's addition (and also makes another change).
    def buggy(base, edits):
        r = real_merge(base, edits)
        r.tracks = [k for k in r.tracks if k != 9 and k != 1]
        return r
    monkeypatch.setattr(engine_mod, "merge", buggy)
    sync.sync_group(g)
    assert plex.items(KATE_ID, keys[KATE_ID]) == [1, 2, 3, 4, 9]  # protected
    kate = [m for m in shares.members(g) if m.user_id == KATE_ID][0]
    assert kate.status == "error" and "just added" in kate.status_detail


def test_each_write_is_logged(env, plex):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    plex.edit(KATE_ID, keys[KATE_ID], [1, 2, 3, 4, 9])
    sync.sync_group(g)
    msgs = [e.message for e in shares.events(g)]
    assert "Updated Nick's copy: +1 −0 (4 → 5 tracks)" in msgs
    assert "Updated Cleo's copy: +1 −0 (4 → 5 tracks)" in msgs


def test_addition_detected_even_if_plex_does_not_bump_updated_at(env, plex):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    pl = plex.playlists[KATE_ID][keys[KATE_ID]]
    pl["items"] = pl["items"] + [9]  # added from a Plex app; updatedAt NOT changed
    plex.edit(CLEO_ID, keys[CLEO_ID], [1, 2, 3])  # and someone else changes something
    sync.sync_group(g)
    assert set(map(tuple, all_items(plex, keys).values())) == {(1, 2, 3, 9)}


# -- artwork and description ------------------------------------------------------------------------

def test_first_round_shares_owners_artwork_and_description(env, plex):
    shares, sync = env
    plex.set_image(ADMIN_ID, 11, "poster", b"OWNER-POSTER")
    plex.set_image(ADMIN_ID, 11, "art", b"OWNER-ART")
    plex.playlists[ADMIN_ID][11]["summary"] = "Songs for the car"
    plex.set_image(KATE_ID, 253611, "poster", b"KATES-OLD-POSTER")  # merged/replaced copy had its own
    g, keys = shared_road_trip(shares, plex)
    sync.sync_group(g)
    for uid, k in keys.items():
        assert plex.image_of(uid, k, "poster") == b"OWNER-POSTER"
        assert plex.image_of(uid, k, "art") == b"OWNER-ART"
        assert plex.playlists[uid][k]["summary"] == "Songs for the car"
    assert g.summary == "Songs for the car" and g.meta_initialized


def test_poster_change_by_anyone_reaches_everyone_once(env, plex):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    sync.sync_group(g)  # first round: owner has no custom artwork, nothing to do
    assert getattr(plex, "uploads", 0) == 0
    plex.set_image(CLEO_ID, keys[CLEO_ID], "poster", b"CLEOS-POSTER")
    sync.sync_group(g)
    assert {plex.image_of(uid, k, "poster") for uid, k in keys.items()} == {b"CLEOS-POSTER"}
    assert any("Cleo set the poster" in e.message for e in shares.events(g))
    uploads = plex.uploads
    sync.sync_group(g)  # our own uploads aren't mistaken for new changes
    sync.sync_group(g)
    assert plex.uploads == uploads


def test_newest_artwork_change_wins(env, plex):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    sync.sync_group(g)
    plex.set_image(KATE_ID, keys[KATE_ID], "art", b"KATE-ART")
    plex.set_image(ADMIN_ID, 11, "art", b"NICK-ART")  # later
    sync.sync_group(g)
    assert {plex.image_of(uid, k, "art") for uid, k in keys.items()} == {b"NICK-ART"}


def test_reselecting_the_same_picture_is_not_reuploaded(env, plex):
    shares, sync = env
    plex.set_image(ADMIN_ID, 11, "poster", b"SAME")
    g, keys = shared_road_trip(shares, plex)
    sync.sync_group(g)
    uploads = plex.uploads
    plex.set_image(KATE_ID, keys[KATE_ID], "poster", b"SAME")  # new path, same bytes
    sync.sync_group(g)
    assert plex.uploads == uploads


def test_description_change_syncs(env, plex):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    sync.sync_group(g)
    plex.edit(KATE_ID, keys[KATE_ID])
    plex.playlists[KATE_ID][keys[KATE_ID]]["summary"] = "Kate's road trip picks"
    sync.sync_group(g)
    assert {plex.playlists[uid][k]["summary"] for uid, k in keys.items()} == {"Kate's road trip picks"}
    assert any("Kate changed the description" in e.message for e in shares.events(g))


def test_new_member_gets_current_artwork(env, plex):
    shares, sync = env
    plex.set_image(ADMIN_ID, 10, "poster", b"MIX-POSTER")
    g = shares.share(shares.session.get(HomeUser, ADMIN_ID), 10, {CLEO_ID: Mode.NEW})
    sync.sync_group(g)
    shares.add_members(g, {KATE_ID: Mode.NEW})
    sync.sync_group(g)
    kate_key = [m.playlist_key for m in shares.members(g) if m.user_id == KATE_ID][0]
    assert plex.image_of(KATE_ID, kate_key, "poster") == b"MIX-POSTER"


def test_artwork_failure_never_breaks_track_sync(env, plex, monkeypatch):
    shares, sync = env
    g, keys = shared_road_trip(shares, plex)
    monkeypatch.setattr(FakeGateway, "playlist_meta", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    plex.edit(KATE_ID, keys[KATE_ID], [1, 2, 3, 4, 9])
    sync.sync_group(g)
    assert plex.items(ADMIN_ID, 11) == [1, 2, 3, 4, 9]
    assert any("Couldn't sync artwork/description" in e.message for e in shares.events(g))
