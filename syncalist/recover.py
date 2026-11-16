"""Recover playlists from one of Plex's own database backups.

Plex keeps a copy of its database every few days (com.plexapp.plugins.library.db-YYYY-MM-DD).
This reads, read-only, the playlists Syncalist has touched from such a copy and adds them to
Syncalist's Backups page, where "Restore" recreates them as new playlists.

Usage (inside the Syncalist container):
    python -m syncalist.recover /config/plex-backup.db            # show what would be recovered
    python -m syncalist.recover /config/plex-backup.db --apply    # add them to the Backups page
    python -m syncalist.recover /config/plex-backup.db --playlist 253611 --user Kate --apply
    python -m syncalist.recover /config/plex-backup.db --find "Road Trip"    # look up playlist ids by name
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from .config import Config
from .db import make_engine
from .models import GroupMember, HomeUser, PlaylistBackup


def open_readonly(path: Path) -> sqlite3.Connection:
    # immutable=1: never write, never create -wal/-shm files next to the backup.
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def playlist_from_backup(con: sqlite3.Connection, playlist_key: int) -> tuple[str, list[int]] | None:
    """(title, ordered track ratingKeys) of a playlist in a Plex database, or None if absent."""
    row = con.execute("SELECT title FROM metadata_items WHERE id = ?", (playlist_key,)).fetchone()
    if row is None:
        return None
    cols = {r[1] for r in con.execute("PRAGMA table_info(play_queue_generators)")}
    if not {"playlist_id", "metadata_item_id", "order"} <= cols:
        raise SystemExit(f"Unexpected Plex database layout (play_queue_generators has {sorted(cols)}). "
                         "Nothing was changed; send this message to the developer.")
    rows = con.execute(
        'SELECT metadata_item_id FROM play_queue_generators WHERE playlist_id = ? ORDER BY "order", id',
        (playlist_key,)).fetchall()
    return row[0], [r[0] for r in rows if r[0] is not None]


def touched_playlists(session: Session) -> dict[int, int]:
    """playlist_key -> user_id for every playlist Syncalist has backed up or shared."""
    out = {b.playlist_key: b.user_id for b in session.exec(select(PlaylistBackup)).all()}
    out.update({m.playlist_key: m.user_id for m in session.exec(select(GroupMember)).all()})
    return out


def main(argv: list[str] | None = None, config: Config | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plex_db", type=Path, help="Copy of a Plex database backup file")
    ap.add_argument("--apply", action="store_true", help="Add the recovered playlists to the Backups page")
    ap.add_argument("--playlist", type=int, action="append", help="Playlist id (default: all Syncalist touched)")
    ap.add_argument("--user", help="Owner's name, required with --playlist for playlists Syncalist never touched")
    ap.add_argument("--find", metavar="TITLE", help="List playlists in the backup whose name contains TITLE")
    args = ap.parse_args(argv)

    if not args.plex_db.is_file():
        print(f"Not found: {args.plex_db}")
        return 1
    if args.find:
        con = open_readonly(args.plex_db)
        rows = con.execute(
            "SELECT m.id, m.title, (SELECT COUNT(*) FROM play_queue_generators g WHERE g.playlist_id = m.id) "
            "FROM metadata_items m WHERE m.metadata_type = 15 AND m.title LIKE ? ORDER BY m.title, m.id",
            (f"%{args.find}%",)).fetchall()
        if not rows:
            print(f"No playlists matching {args.find!r} in this backup.")
        for pid, title, count in rows:
            print(f"  {pid}: “{title}”: {count} tracks")
        print("\nRecover one with:  --playlist <id> --user <owner's name>   (add --apply to save it)")
        return 0

    config = config or Config()
    session = Session(make_engine(f"sqlite:///{config.db_path}"))
    users = {u.id: u for u in session.exec(select(HomeUser)).all()}
    owners = touched_playlists(session)

    if args.playlist:
        targets = {}
        for key in args.playlist:
            if key in owners:
                targets[key] = owners[key]
            elif args.user:
                match = [u for u in users.values() if u.title.lower() == args.user.lower()]
                if not match:
                    known = [u.title for u in users.values()]
                    hint = "" if known else " Finish setup in the web UI first so Syncalist knows your Home users."
                    print(f"No Home user called {args.user!r}. Known: {known}.{hint}")
                    return 1
                targets[key] = match[0].id
            else:
                print(f"Playlist {key} isn't one Syncalist touched; add --user <owner>.")
                return 1
    else:
        targets = owners
        if not targets:
            print("Syncalist has no record of touching any playlists (for example, its database is new).\n"
                  "Name them instead:  --playlist <id> --user <owner's name>\n"
                  "Find ids by name:   --find \"Road Trip\"")
            return 1

    stamp = args.plex_db.name.rsplit("-", 3)[-3:] if args.plex_db.name.count("-") >= 3 else None
    source = "-".join(stamp) if stamp else args.plex_db.name
    con = open_readonly(args.plex_db)
    found = 0
    print(f"Reading {args.plex_db} (read-only)\n")
    for key, user_id in sorted(targets.items()):
        user = users.get(user_id)
        result = playlist_from_backup(con, key)
        who = user.title if user else f"user {user_id}"
        if result is None:
            print(f"  {key}: not in this backup (created after it was taken)")
            continue
        title, tracks = result
        print(f"  {key}: {who}'s “{title}”: {len(tracks)} tracks")
        found += 1
        if args.apply and tracks:
            session.add(PlaylistBackup(
                user_id=user_id, user_title=who, playlist_key=key, title=title, track_keys=tracks,
                reason=f"Recovered from Plex database backup {source}", taken_at=datetime.now(timezone.utc)))
    if args.apply:
        session.commit()
        print(f"\nAdded {found} playlist(s) to the Backups page. Use Restore there to recreate them.")
    else:
        print("\nNothing changed. Run again with --apply to add these to the Backups page.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
