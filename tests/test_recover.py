import sqlite3

from sqlmodel import Session, select

from syncalist.config import Config
from syncalist.db import make_engine
from syncalist.models import GroupMember, HomeUser, PlaylistBackup
from syncalist.recover import main


def make_plex_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata_items (id INTEGER PRIMARY KEY, title TEXT, metadata_type INTEGER)")
    con.execute('CREATE TABLE play_queue_generators (id INTEGER PRIMARY KEY, playlist_id INTEGER, '
                'metadata_item_id INTEGER, "order" REAL, uri TEXT)')
    con.execute("INSERT INTO metadata_items VALUES (253611, 'Road Trip', 15)")
    # Out of insertion order on purpose: the "order" column decides.
    for i, (track, order) in enumerate([(30, 3000.0), (10, 1000.0), (20, 2000.0)]):
        con.execute("INSERT INTO play_queue_generators VALUES (?, 253611, ?, ?, '')", (i + 1, track, order))
    con.commit()
    con.close()


def test_recover_dry_run_then_apply(tmp_path, capsys):
    config = Config(data_dir=tmp_path)
    with Session(make_engine(f"sqlite:///{config.db_path}")) as s:
        s.add(HomeUser(id=7, title="Kate"))
        s.add(GroupMember(group_id=1, user_id=7, playlist_key=253611))
        s.add(GroupMember(group_id=2, user_id=7, playlist_key=999999))  # created after the backup
        s.commit()
    plex_db = tmp_path / "com.plexapp.plugins.library.db-2026-11-15"
    make_plex_db(plex_db)

    assert main([str(plex_db)], config) == 0
    out = capsys.readouterr().out
    assert "Kate's “Road Trip”: 3 tracks" in out and "999999: not in this backup" in out
    with Session(make_engine(f"sqlite:///{config.db_path}")) as s:
        assert s.exec(select(PlaylistBackup)).all() == []

    assert main([str(plex_db), "--apply"], config) == 0
    with Session(make_engine(f"sqlite:///{config.db_path}")) as s:
        (b,) = s.exec(select(PlaylistBackup)).all()
        assert b.track_keys == [10, 20, 30] and b.user_id == 7
        assert b.reason == "Recovered from Plex database backup 2026-11-15"
    assert not (tmp_path / "com.plexapp.plugins.library.db-2026-11-15-wal").exists()
