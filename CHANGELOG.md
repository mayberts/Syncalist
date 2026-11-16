# Changelog

Docker images: `ghcr.io/mayberts/syncalist:<version>`. `latest` follows the main branch;
`0.5` follows the newest 0.5.x release; `0.5.0` never changes.

## 0.5.2 (2026-11-16)

- Shared playlists now show their real track count in each person's list, instead of Plex's
  stored count, which can be out of date (seen: 112 shown, 107 real). Hover the number to see
  Plex's figure when they differ.

## 0.5.1 (2026-11-16)

- Playlist poster thumbnails on the playlists page, for every playlist and in the Shared
  playlists table: the custom poster where one is set, otherwise Plex's automatic collage.
  Plex resizes them to thumbnail size, and the browser caches them until the poster changes.

## 0.5.0 (2026-11-16)

- **Artwork and description are shared and kept in sync**: the playlist's poster (icon),
  background art and description.
  - When you share, the sharer's custom poster, background and description go to everyone.
    Existing shares adopt the owner's on their first round after updating.
  - Afterwards, whoever changes one on their copy in Plex, it reaches everyone in the next
    round. If two people change the same thing, the most recent change wins.
  - Images are only downloaded and uploaded when one actually changes. Re-selecting the
    same picture does nothing.
  - Going back to Plex's automatic collage isn't synced (Plex's API doesn't support it well).
- Shared playlist pages show the current poster and description.
- Diagnostics show the raw artwork attributes Plex reports for a playlist.
- An artwork or description problem is logged and never stops tracks syncing.

## 0.4.1 (2026-11-16)

- New logo: a playlist inside two sync arrows, on amber. A simplified version is used at
  favicon sizes.
- Favicon (SVG, PNG and multi-size `.ico`), phone home-screen icons and a web manifest.
- Logo in the web interface header.
- Unraid: new container icon. The template and the image's `net.unraid.docker.icon` and
  `net.unraid.docker.webui` labels point at it, so Docker Compose containers get the icon
  and the WebUI link too.
- `branding/` holds the SVG sources, a script to regenerate every asset, and a GitHub
  social preview image.

## 0.4.0 (2026-11-16)

- **History** page replaces Backups in the menu: activity and backups together.
  - Each sync round is one entry: what changed (who added, removed, reordered or renamed
    what), with the per-copy updates folded under "N copies updated".
  - Filter by **Problems only** and by shared playlist; **Show older** pages back 50 at a time.
- Shared playlist pages show their recent activity, with a **Full history** link.
- Activity older than 90 days is deleted automatically after each sync round. The number of
  days can be changed in **Settings → Activity log** (7–3650). Backups aren't affected.
- `/backups` links still work (they redirect to History).

## 0.3.0 (2026-11-16)

- **Sync all** button on the playlists page runs a full round over every shared playlist.
- Status line on the playlists page: when the last full sync ran and what it did
  (checked / updated / paused / failed), when the next one is due, and how many shared
  playlists need attention.
- Red badge on **Playlists** in the menu, on every page, when any shared playlist is paused
  or has a member whose copy has a problem.
- A failed round (for example, Plex unreachable) is recorded and shown.
- Phone layout fixes for the menu and the shared playlists table.

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
