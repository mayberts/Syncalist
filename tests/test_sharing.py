import pytest
from conftest import ADMIN_ID, CLEO_ID, KATE_ID, FakeGateway
from sqlmodel import Session

from syncalist.crypto import Secrets
from syncalist.db import make_engine
from syncalist.models import GroupMember, HomeUser, PlaylistBackup
from syncalist.sharing import Mode, ShareError, ShareService
from syncalist.users import UserService


@pytest.fixture
def svc(tmp_path, plex):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    session = Session(engine)
    users = UserService(session, Secrets(tmp_path / "key"), FakeGateway(plex, "http://plex", "admin-token"))
    users.refresh()
    yield ShareService(session, users)
    session.close()


def user(svc, uid) -> HomeUser:
    return svc.session.get(HomeUser, uid)


def members_by_user(svc, group):
    return {m.user_id: m for m in svc.members(group)}


def test_share_new_copy(svc, plex):
    g = svc.share(user(svc, ADMIN_ID), 10, {CLEO_ID: Mode.NEW})
    m = members_by_user(svc, g)
    assert set(m) == {ADMIN_ID, CLEO_ID}
    cleo_pl = plex.playlists[CLEO_ID][m[CLEO_ID].playlist_key]
    assert cleo_pl["title"] == "Admin Mix" and cleo_pl["items"] == [1, 2]
    assert g.base_snapshot == [1, 2]
    assert all(x.last_seen_updated_at is not None for x in m.values())
    assert plex.items(ADMIN_ID, 10) == [1, 2]  # source untouched


def test_share_merge_combines_and_updates_everyone(svc, plex):
    # Admin's Road Trip [1,2,3,4], Kate's Road Trip [3,5,6]
    g = svc.share(user(svc, ADMIN_ID), 11, {KATE_ID: Mode.MERGE})
    assert g.base_snapshot == [1, 2, 3, 4, 5, 6]
    assert plex.items(ADMIN_ID, 11) == [1, 2, 3, 4, 5, 6]
    assert plex.items(KATE_ID, 253611) == [1, 2, 3, 4, 5, 6]
    reasons = {(b.user_id, b.playlist_key) for b in svc.backups()}
    assert reasons == {(KATE_ID, 253611), (ADMIN_ID, 11)}  # both changed, both backed up


def test_share_replace_overwrites_theirs_after_backup(svc, plex):
    g = svc.share(user(svc, ADMIN_ID), 11, {KATE_ID: Mode.REPLACE})
    assert plex.items(KATE_ID, 253611) == [1, 2, 3, 4]
    assert plex.items(ADMIN_ID, 11) == [1, 2, 3, 4]
    backups = svc.backups()
    assert [(b.user_id, b.track_keys) for b in backups] == [(KATE_ID, [3, 5, 6])]
    assert members_by_user(svc, g)[KATE_ID].playlist_key == 253611


def test_skipped_tracks_when_user_lacks_access(svc, plex):
    plex.no_access[CLEO_ID] = {2}
    g = svc.share(user(svc, ADMIN_ID), 10, {CLEO_ID: Mode.NEW})
    m = members_by_user(svc, g)[CLEO_ID]
    assert m.skipped == [2]
    assert plex.items(CLEO_ID, m.playlist_key) == [1]
    assert any("skipped" in e.message for e in svc.events(g))


def test_cannot_share_smart_or_twice(svc):
    with pytest.raises(ShareError, match="Smart"):
        svc.share(user(svc, ADMIN_ID), 12, {CLEO_ID: Mode.NEW})
    svc.share(user(svc, ADMIN_ID), 10, {CLEO_ID: Mode.NEW})
    with pytest.raises(ShareError, match="already shared"):
        svc.share(user(svc, ADMIN_ID), 10, {KATE_ID: Mode.NEW})


def test_failed_share_leaves_nothing_behind(svc, plex):
    plex.no_access[CLEO_ID] = {1, 2}
    with pytest.raises(ShareError, match="Nobody could be added"):
        svc.share(user(svc, ADMIN_ID), 10, {CLEO_ID: Mode.NEW})
    assert svc.groups() == [] and svc.member_index() == {}
    assert any("Couldn't add Cleo" in e.message for e in svc.events())


def test_partial_failure_still_shares_with_others(svc, plex):
    plex.no_access[CLEO_ID] = {1, 2}
    g = svc.share(user(svc, ADMIN_ID), 10, {CLEO_ID: Mode.NEW, KATE_ID: Mode.NEW})
    assert set(members_by_user(svc, g)) == {ADMIN_ID, KATE_ID}


def test_add_member_later_with_merge_updates_existing_members(svc, plex):
    g = svc.share(user(svc, ADMIN_ID), 11, {CLEO_ID: Mode.NEW})
    cleo_key = members_by_user(svc, g)[CLEO_ID].playlist_key
    svc.add_members(g, {KATE_ID: Mode.MERGE})
    for uid, key in ((ADMIN_ID, 11), (CLEO_ID, cleo_key), (KATE_ID, 253611)):
        assert plex.items(uid, key) == [1, 2, 3, 4, 5, 6]


def test_same_name_playlist_already_shared_is_refused(svc, plex):
    svc.share(user(svc, KATE_ID), 253611, {CLEO_ID: Mode.NEW})  # Kate's Road Trip is now shared
    with pytest.raises(ShareError):
        svc.share(user(svc, ADMIN_ID), 11, {KATE_ID: Mode.MERGE})


def test_candidates_detect_same_name(svc):
    cands = {c.user.id: c for c in svc.candidates("road trip", exclude_user_ids={ADMIN_ID})}
    assert cands[KATE_ID].existing.key == 253611
    assert cands[CLEO_ID].existing is None


def test_remove_member_keep_or_delete(svc, plex):
    g = svc.share(user(svc, ADMIN_ID), 10, {CLEO_ID: Mode.NEW, KATE_ID: Mode.NEW})
    kate_key = members_by_user(svc, g)[KATE_ID].playlist_key
    cleo_key = members_by_user(svc, g)[CLEO_ID].playlist_key
    svc.remove_member(g, KATE_ID, delete_playlist=False)
    assert kate_key in plex.playlists[KATE_ID]
    svc.remove_member(g, CLEO_ID, delete_playlist=True)
    assert cleo_key not in plex.playlists[CLEO_ID]
    assert any(b.user_id == CLEO_ID for b in svc.backups())  # backed up before deleting
    assert set(members_by_user(svc, g)) == {ADMIN_ID}


def test_stop_sharing_keeps_all_copies(svc, plex):
    g = svc.share(user(svc, ADMIN_ID), 10, {CLEO_ID: Mode.NEW})
    cleo_key = members_by_user(svc, g)[CLEO_ID].playlist_key
    svc.stop_sharing(g)
    assert svc.groups() == [] and not svc.session.exec(__import__("sqlmodel").select(GroupMember)).all()
    assert cleo_key in plex.playlists[CLEO_ID] and 10 in plex.playlists[ADMIN_ID]


def test_restore_creates_new_playlist(svc, plex):
    svc.share(user(svc, ADMIN_ID), 11, {KATE_ID: Mode.REPLACE})
    backup = svc.backups()[0]
    restored = svc.restore(backup)
    assert restored.title.startswith("Road Trip (restored ")
    assert plex.items(KATE_ID, restored.key) == [3, 5, 6]
