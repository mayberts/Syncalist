import sqlite3

from sqlmodel import Session, select

from syncalist.db import make_engine
from syncalist.models import GroupMember


def test_old_database_gets_new_columns(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    # groupmember as created by the M2 release (no last_known column)
    con.execute("""CREATE TABLE groupmember (id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL, playlist_key INTEGER NOT NULL, last_seen_updated_at DATETIME,
        skipped JSON, status VARCHAR NOT NULL, status_detail VARCHAR, joined_at DATETIME NOT NULL)""")
    con.execute("INSERT INTO groupmember VALUES (1, 1, 7, 99, NULL, '[]', 'ok', NULL, '2026-11-16 10:00:00')")
    con.commit()
    con.close()
    engine = make_engine(f"sqlite:///{db}")
    with Session(engine) as s:
        (m,) = s.exec(select(GroupMember)).all()
        assert m.last_known is None and m.playlist_key == 99
