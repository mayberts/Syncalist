# Changelog

Docker images: `ghcr.io/mayberts/syncalist:<version>`. `latest` follows the main branch;
`0.2` follows the newest 0.2.x release; `0.2.0` never changes.

## 0.2.0 (2026-11-16)

First tagged release.

- Sign in with Plex (server owner only; managed Home users are told to switch account).
- Browse every Home user's music playlists. No PINs needed.
- Share playlists between Home users. Where someone already has a playlist with the same
  name, choose merge, replace or keep separate.
- Two-way automatic sync: additions, removals, reordering and renames reach everyone.
  - Removals win. Additions stay where they were added. The newest reorder decides order.
  - Tracks a person can't access are skipped for them and kept for everyone else.
  - Deleting your copy takes you out of the share.
- Safety:
  - Everything is read (and checked complete) before anything is written.
  - Only Replace can remove tracks when sharing. A sync never removes a track someone
    just added.
  - Large removals pause for Apply / Undo.
  - Copies are backed up before tracks are removed (10 newest per playlist).
  - Every write is read back and logged.
- Sync now button, and a sync interval you can change in Settings.
- Diagnostics page per playlist; `python -m syncalist.recover` restores playlists from
  Plex's own database backups.
- Unraid template and compose file; amd64 and arm64 images.

## 0.1.0

Untagged development builds (read-only browsing, first sharing version).
