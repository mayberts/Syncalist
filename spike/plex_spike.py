#!/usr/bin/env python3
"""
Syncalist M0 spike: checks that the things the sync engine relies on actually
work against your Plex Home.

What it does:
  1. Connects to your Plex server as the admin and lists your Home users.
  2. Switches to one Home user, both with and without their PIN, and gets
     that user's token for the server.
  3. Lists that user's music playlists (read-only).
  4. Creates a TEMPORARY playlist as that user, then adds, removes, moves,
     duplicates and renames tracks. After each change it records whether
     `updatedAt` changed, because the sync engine uses it to spot edits.
     The temporary playlist is deleted at the end (unless --keep is passed).
  5. Checks that a track has the same ratingKey for the admin and the user.

It never changes or deletes any existing playlist. Use --read-only to skip
step 4 entirely.

Results go to spike_report.json (tokens and PINs are never written to it).

Usage:
    pip install plexapi
    python plex_spike.py --url http://192.168.1.10:32400 --token <ADMIN_TOKEN>
    # or set PLEX_URL / PLEX_TOKEN environment variables instead.

Finding your admin token: https://support.plex.tv/articles/204059436
"""

import argparse
import getpass
import json
import os
import sys
import time
import traceback
from datetime import datetime

try:
    from plexapi.exceptions import BadRequest, NotFound, Unauthorized
    from plexapi.playlist import Playlist
    from plexapi.server import PlexServer
    import plexapi
except ImportError:
    sys.exit("plexapi is not installed. Run:  pip install plexapi")


REPORT = {"started": datetime.now().isoformat(timespec="seconds"), "steps": []}
OP_DELAY = 2.0  # seconds between writes; updatedAt only has 1-second resolution


def step(name, fn, *, fatal=False):
    """Run one check, record the result, and keep going unless fatal."""
    print(f"\n== {name}")
    t0 = time.monotonic()
    entry = {"step": name}
    try:
        detail = fn()
        entry.update(ok=True, detail=detail)
        print(f"   OK  {json.dumps(detail, default=str)[:400] if detail is not None else ''}")
    except Exception as e:  # noqa: BLE001 - this is a diagnostic script
        entry.update(ok=False, error=f"{type(e).__name__}: {e}",
                     trace=traceback.format_exc(limit=3))
        print(f"   FAIL  {type(e).__name__}: {e}")
    entry["seconds"] = round(time.monotonic() - t0, 2)
    REPORT["steps"].append(entry)
    if fatal and not entry["ok"]:
        finish(1)
    return entry


def finish(code=0):
    REPORT["finished"] = datetime.now().isoformat(timespec="seconds")
    path = os.path.abspath("spike_report.json")
    with open(path, "w") as f:
        json.dump(REPORT, f, indent=2, default=str)
    passed = sum(s["ok"] for s in REPORT["steps"])
    print(f"\n{passed}/{len(REPORT['steps'])} steps passed. Report written to {path}")
    print("No tokens or PINs are in the report, so it is safe to share.")
    sys.exit(code)


def pl_state(pl: Playlist):
    pl.reload()
    items = pl.items()
    return {
        "title": pl.title,
        "updatedAt": pl.updatedAt,
        "leafCount": pl.leafCount,
        "ratingKeys": [i.ratingKey for i in items],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("PLEX_URL"), help="Plex server URL, e.g. http://192.168.1.10:32400")
    ap.add_argument("--token", default=os.environ.get("PLEX_TOKEN"), help="Admin X-Plex-Token")
    ap.add_argument("--user", help="Home user to test (name). You are asked to choose if omitted.")
    ap.add_argument("--library", help="Music library name. Defaults to the first music library.")
    ap.add_argument("--read-only", action="store_true", help="Skip the write tests")
    ap.add_argument("--keep", action="store_true", help="Don't delete the temporary test playlist")
    ap.add_argument("--use-pin-token", action="store_true",
                    help="Run the read/write tests with the PIN token instead of the no-PIN token")
    args = ap.parse_args()
    if not args.url or not args.token:
        ap.error("--url and --token are required (or set PLEX_URL / PLEX_TOKEN)")

    REPORT["plexapi_version"] = plexapi.VERSION
    ctx = {}

    # 1. Admin connection ------------------------------------------------------
    def connect_admin():
        ctx["admin"] = admin = PlexServer(args.url, args.token)
        ctx["account"] = acct = admin.myPlexAccount()
        music = [s for s in admin.library.sections() if s.type == "artist"]
        if not music:
            raise RuntimeError("No music libraries found on this server")
        if args.library:
            music = [s for s in music if s.title == args.library] or \
                    sys.exit(f"Music library '{args.library}' not found")
        ctx["section_title"] = music[0].title
        return {
            "server": admin.friendlyName, "version": admin.version,
            "machineIdentifier": admin.machineIdentifier,
            "account_is_home_admin": acct.homeAdmin,
            "music_libraries": [s.title for s in admin.library.sections() if s.type == "artist"],
            "using_library": music[0].title,
        }
    step("Connect to server as admin", connect_admin, fatal=True)

    # 2. Home users ------------------------------------------------------------
    def list_users():
        users = [u for u in ctx["account"].users() if u.home]
        ctx["users"] = users
        return [{"title": u.title, "id": u.id, "protected_pin": u.protected,
                 "managed": u.restricted} for u in users]
    step("List Plex Home users", list_users, fatal=True)

    users = ctx["users"]
    if not users:
        print("No other Home users found.")
        finish(1)
    if args.user:
        match = [u for u in users if u.title.lower() == args.user.lower()]
        if not match:
            sys.exit(f"Home user '{args.user}' not found. Options: {[u.title for u in users]}")
        user = match[0]
    else:
        print("\nHome users:")
        for i, u in enumerate(users, 1):
            print(f"  {i}. {u.title}{'  (PIN)' if u.protected else ''}{'  (managed)' if u.restricted else ''}")
        user = users[int(input("Pick a user to test with [number]: ")) - 1]
    REPORT["tested_user"] = {"title": user.title, "protected_pin": user.protected, "managed": user.restricted}
    admin = ctx["admin"]

    # 3a. Token without PIN (shared-server token) ------------------------------
    def token_without_pin():
        tok = user.get_token(admin.machineIdentifier)
        if not tok:
            raise RuntimeError("plex.tv returned no shared-server token for this user")
        srv = PlexServer(args.url, tok)
        ctx["nopin_server"] = srv
        ctx.setdefault("user_server", srv)
        return {"got_token": True}
    step("Get user's server token WITHOUT PIN (get_token)", token_without_pin)

    # 3b. Token with PIN (switchHomeUser) --------------------------------------
    pin = getpass.getpass(f"\nEnter PIN for {user.title} (blank to skip the PIN test): ") if user.protected \
        else ""
    def token_with_pin():
        ua = ctx["account"].switchHomeUser(user, pin=pin or None)
        res = [r for r in ua.resources() if r.clientIdentifier == admin.machineIdentifier]
        if not res:
            raise RuntimeError("Switched user has no resource entry for this server")
        srv = PlexServer(args.url, res[0].accessToken)
        ctx["pin_server"] = srv
        if args.use_pin_token or "user_server" not in ctx:
            ctx["user_server"] = srv
        return {"switched_account": ua.title, "got_token": True}
    if user.protected and not pin:
        print("   (PIN test skipped)")
    else:
        step("Get user's server token WITH PIN (switchHomeUser)", token_with_pin)

    if "user_server" not in ctx:
        print("\nCould not get a server token for the user by either method. Stopping.")
        finish(1)
    usrv: PlexServer = ctx["user_server"]
    REPORT["tests_use_token"] = "pin" if usrv is ctx.get("pin_server") else "no-pin"
    print(f"\nRunning the playlist tests with the {REPORT['tests_use_token']} token.")

    # 4. Read the user's playlists ---------------------------------------------
    def read_playlists():
        pls = usrv.playlists(playlistType="audio")
        out = []
        for p in pls:
            out.append({"title": p.title, "ratingKey": p.ratingKey, "smart": p.smart,
                        "leafCount": p.leafCount, "updatedAt": p.updatedAt})
        admin_keys = {p.ratingKey for p in admin.playlists()}
        ctx["user_pl_keys"] = [p.ratingKey for p in pls]
        return {"count": len(out), "playlists": out,
                "admin_can_see_user_playlists": any(k in admin_keys for k in ctx["user_pl_keys"])}
    step("Read user's music playlists", read_playlists)

    def compare_tokens():
        """Whose playlists does the no-PIN token actually see?"""
        def keys(srv):
            return sorted(p.ratingKey for p in srv.playlists(playlistType="audio"))
        nopin = keys(ctx["nopin_server"])
        res = {"nopin_token_playlist_count": len(nopin),
               "nopin_sees_same_as_admin": nopin == keys(admin)}
        if "pin_server" in ctx:
            res["nopin_sees_same_as_pin_user"] = nopin == keys(ctx["pin_server"])
        return res
    if "nopin_server" in ctx:
        step("Compare no-PIN token with PIN token", compare_tokens)

    def time_largest():
        pls = [p for p in usrv.playlists(playlistType="audio") if not p.smart]
        if not pls:
            return "no non-smart playlists to time"
        big = max(pls, key=lambda p: p.leafCount)
        t0 = time.monotonic()
        n = len(big.items())
        return {"playlist": big.title, "tracks": n, "fetch_seconds": round(time.monotonic() - t0, 2)}
    step("Time fetching the user's largest playlist", time_largest)

    if args.read_only:
        finish(0)

    # 5. Write tests on a temporary playlist ------------------------------------
    section = usrv.library.section(ctx["section_title"])
    tracks = section.searchTracks(maxresults=4)
    if len(tracks) < 4:
        print("Need at least 4 tracks the user can see for the write tests.")
        finish(1)
    t1, t2, t3, t4 = tracks
    title = f"Syncalist spike {datetime.now():%Y%m%d-%H%M%S}"
    pl_holder = {}

    def create():
        pl_holder["pl"] = Playlist.create(usrv, title, items=[t1, t2, t3])
        st = pl_state(pl_holder["pl"])
        pl_holder["last"] = st
        return st

    def write_op(label, action, expect=None):
        def run():
            time.sleep(OP_DELAY)
            before = pl_holder["last"]
            action(pl_holder["pl"])
            after = pl_state(pl_holder["pl"])
            pl_holder["last"] = after
            res = {"updatedAt_changed": after["updatedAt"] != before["updatedAt"],
                   "before": before["ratingKeys"], "after": after["ratingKeys"],
                   "title": after["title"]}
            if expect is not None:
                res["matches_expected"] = expect(after)
            return res
        step(label, run)

    try:
        if not step("Create temp playlist as user", create)["ok"]:
            finish(1)

        write_op("Add a track", lambda p: p.addItems([t4]),
                 lambda s: s["ratingKeys"] == [t1.ratingKey, t2.ratingKey, t3.ratingKey, t4.ratingKey])

        write_op("Remove a track", lambda p: p.removeItems([i for i in p.items() if i.ratingKey == t1.ratingKey]),
                 lambda s: s["ratingKeys"] == [t2.ratingKey, t3.ratingKey, t4.ratingKey])

        write_op("Move last track to the top",
                 lambda p: p.moveItem(p.items()[-1], after=None),
                 lambda s: s["ratingKeys"] == [t4.ratingKey, t2.ratingKey, t3.ratingKey])

        write_op("Move a track after another",
                 lambda p: (lambda its: p.moveItem(its[0], after=its[1]))(p.items()),
                 lambda s: s["ratingKeys"] == [t2.ratingKey, t4.ratingKey, t3.ratingKey])

        write_op("Add a duplicate (does Plex allow it?)", lambda p: p.addItems([t2]))

        write_op("Rename the playlist", lambda p: p.editTitle(title + " (renamed)"),
                 lambda s: s["title"] == title + " (renamed)")

        def same_keys():
            out = []
            for t in (t1, t2, t3, t4):
                a = admin.fetchItem(t.ratingKey)
                out.append({"ratingKey": t.ratingKey, "user_title": t.title, "admin_title": a.title,
                            "same": a.title == t.title})
            return {"all_same": all(o["same"] for o in out), "tracks": out}
        step("Same ratingKey for admin and user", same_keys)

        def admin_cannot_edit():
            key = pl_holder["pl"].ratingKey
            try:
                admin.fetchItem(key)
                return {"admin_can_fetch_user_playlist": True}
            except (NotFound, Unauthorized, BadRequest):
                return {"admin_can_fetch_user_playlist": False}
        step("Can the admin token fetch the user's playlist by key?", admin_cannot_edit)

    finally:
        pl = pl_holder.get("pl")
        if pl and not args.keep:
            step("Delete temp playlist", lambda: pl.delete())
        elif pl:
            print(f"\nKept test playlist '{pl.title}' for {user.title}. Delete it in Plex when done.")

    finish(0)


if __name__ == "__main__":
    main()
