# Syncalist — Plan

Keeps music playlists in sync between users of a Plex Home. The Plex Home admin picks which playlists are shared and who they go to. After that, any member can edit their copy and the changes reach everyone else.

## 1. Decisions so far

| Area | Decision |
|---|---|
| Sync model | **Two-way / collaborative.** Every member of a share can add, remove and reorder tracks. |
| Web UI users | **Admin only.** The Plex Home admin signs in and manages sharing for everyone. |
| Media | **Music (audio) playlists only.** Smart playlists, video and photo are out of scope for v1. |
| Stack / deploy | **Python in one Docker container**, running next to Plex. |
| Plex servers | **One server.** No multi-server support. |
| PIN-protected users | **Supported.** The admin enters each user's PIN once in the UI. It is stored encrypted. |
| Duplicates | **Never.** A shared playlist holds each track at most once. |
| Titles | **One name for everyone.** Renaming any member's copy renames all copies. |
| Member deletes their copy | **They drop out of the share.** Nobody else's copy is touched. |
| Poll interval | **60 s by default, changeable in the UI** (Settings page, applied without a restart). |
| Network | **Home network only.** Plain HTTP, no reverse proxy or HTTPS needed. |

## 2. How Plex makes this possible (and where it gets in the way)

- **Playlists belong to one user.** Each Home user has their own playlists on the server. To read or write one, the app has to act as that user.
- **Acting as a Home user.** With the admin's plex.tv token, `python-plexapi` can switch to each Home user (`MyPlexAccount.switchHomeUser(user, pin)`) and get that user's token for the server. All your users have **PINs**, so the admin enters each PIN once in the UI. The app stores it encrypted and uses it whenever a user's token has to be refreshed.
- **All users share the same tracks.** Users are on the same server, so a track has the same `ratingKey` for all of them. A playlist can be copied by ID, with no fuzzy matching on title or artist.
- **Users can have restricted library access.** If a recipient can't see a track's library, adding it will fail. The app skips that track and flags it in the UI.
- **Plex doesn't announce playlist edits.** Webhooks don't fire for playlist changes, so the app **polls**. Each playlist's `updatedAt` and `leafCount` make that cheap: it only downloads the full track list when one of those has changed.
- **Reordering is one track at a time.** `moveItem` moves a single item, so the app works out the smallest set of moves (via the longest increasing subsequence) instead of rebuilding the playlist.

## 3. Core concept: the share group

A **share group** is one logical playlist made up of N real Plex playlists, one per member user.

```
Share group "Road Trip"
 ├─ base snapshot: [t1, t2, t3, t4]   ← last agreed state, stored in SQLite
 ├─ Alice's playlist  (ratingKey 1001, owner/creator)
 ├─ Bob's playlist    (ratingKey 2044)
 └─ Kid's playlist    (ratingKey 3107)
```

### Sync algorithm (three-way merge per poll)

1. **Detect.** For each member, compare the Plex `updatedAt` with the value saved after the last sync. Unchanged members are skipped.
2. **Diff.** For each changed member, diff its current track list against the **base snapshot**. That gives `adds`, `removes` and its new order.
3. **Merge** into a new canonical list:
   - Start from the base.
   - Apply **all removes**. Removing wins over keeping.
   - Apply **all adds**. Each new track goes next to the track it followed in the editor's playlist, or at the end if that track is gone.
   - **Order:** take the order from the member who edited most recently, then slot in the other members' adds.
4. **Push.** For each member, work out the smallest set of ops (`removeItems`, `addItems`, `moveItem`) that turns their playlist into the canonical list, and apply them.
5. **Title.** If any member renamed their copy since the last sync, the most recent rename becomes the group's name and is applied to every copy.
6. **Commit.** Save the canonical list as the new base. Save each member's new `updatedAt` **after** the app's own writes, so its edits don't look like user edits on the next poll (echo suppression).

The merge is a **pure function** (`merge(base, {member: current_list}) -> canonical`). That keeps it testable in isolation, which matters because this is where the bugs will be.

### Edge cases and default rules

| Case | Default behaviour |
|---|---|
| Two people add the same track | Added once. |
| One person removes a track, another moves it | Remove wins. |
| A member deletes their Plex playlist | They **drop out** of the group. Nobody else's copy is deleted. Shown in the UI as "left", and the admin can re-add them. |
| A recipient can't access a track (library restrictions) | Skipped for that user and flagged in the UI. Kept for everyone else. |
| Someone adds a duplicate track | The duplicate is removed again on the next sync. Each track appears once. |
| Two members rename their copies between polls | The rename with the later `updatedAt` wins. Every copy gets that name. |
| A user's PIN changes, or their token stops working | That member is marked "needs PIN" and skipped. The other members keep syncing. The dashboard asks the admin to re-enter the PIN. |
| Sharing an existing playlist to a new user | A new playlist is created for them with the same title, filled with the canonical list. |
| Stop sharing with a user | Choice in the UI: keep their copy as a normal playlist, or delete it. |
| First sync of a group | Back up every member's playlist first, so it can be restored. |

## 4. Architecture

```
┌──────────────────── Docker container ─────────────────────┐
│  FastAPI app                                              │
│   ├─ Web UI (Jinja2 + HTMX, no JS build step)             │
│   ├─ REST API  /api/...                                   │
│   ├─ Auth: Plex sign-in (PIN flow) → must be Home admin   │
│   ├─ Sync engine                                          │
│   │    ├─ PlexGateway (python-plexapi, per-user tokens)   │
│   │    ├─ merge.py (pure 3-way merge)                     │
│   │    └─ planner.py (minimal add/remove/move ops)        │
│   └─ Scheduler (APScheduler, poll every N seconds)        │
│  SQLite (SQLModel) at /config/syncalist.db                │
└──────────────┬────────────────────────────────────────────┘
               │ HTTP (LAN)
          Plex Media Server  ◄──►  plex.tv (user tokens)
```

### Data model (SQLite)

- `settings`: Plex URL, server machineId, admin token (encrypted), poll interval (seconds, default 60).
- `home_user`: id, plex_user_id, title, avatar, PIN (encrypted), cached server token (encrypted), status (ok / needs PIN).
- `share_group`: id, name (the shared title), created_at, enabled, base_snapshot (JSON list of ratingKeys), last_synced_at.
- `group_member`: group_id, home_user_id, plex_playlist_key, last_seen_updated_at, status (active / left / error).
- `sync_event`: timestamp, group_id, member, action (add/remove/move/error), track info. Feeds the activity log.
- `playlist_backup`: playlist_key, user, taken_at, items JSON.

**Secrets:** the admin token, user PINs and user tokens are encrypted with Fernet. The key is generated on first run and saved to `/config/secret.key`, so a copy of the database alone doesn't expose them.

### Web UI pages

1. **Sign in** with Plex. The app checks that the account is the Home admin.
2. **Setup:** pick the music library, then enter each Home user's PIN. Each PIN is checked against plex.tv before it is saved.
3. **Settings:** change the poll interval (minimum 15 s), re-enter PINs, and see the Plex connection status. A new interval applies right away.
4. **Dashboard:** share groups with members, last sync, status and errors, plus a "Sync now" button.
5. **Playlists:** all music playlists grouped by user. "Share…" opens a dialog to pick recipient users.
6. **Group detail:** track list, members (add or remove users), per-member skipped tracks, and an activity log.
7. **Backups:** restore any playlist from a backup.

## 5. Milestones

| # | Milestone | Outcome |
|---|---|---|
| M0 | **Spike** | Script that signs in as admin, lists Home users, switches to a PIN-protected user with their PIN, then reads, writes and renames one of their playlists. Confirms the riskiest assumptions. |
| M1 | Skeleton | FastAPI app, Plex sign-in, SQLite, Docker image. Lists users and their music playlists, read-only. |
| M2 | One-way share | Create a share group and copy the playlist to recipients. Backups. |
| M3 | Two-way sync | Merge engine plus planner with full unit tests, scheduler, echo suppression. |
| M4 | UX and safety | Activity log, skipped-track warnings, leave/unshare flows, restore from backup, dry-run mode. |
| M5 | Release | docker-compose example, README, health check, multi-arch image (amd64/arm64 for NAS / Raspberry Pi). The UI is for the home network only, so there is no HTTPS or reverse-proxy setup. |

## 6. Testing

- **Unit:** `merge()` and `planner()` tested against a table of scenarios: concurrent adds, remove vs move, reorder conflicts, empty playlists, large (5k-track) lists. Property-based tests with `hypothesis` check that applying the plan always produces the canonical list.
- **Integration:** a fake `PlexGateway` held in memory. The scheduler loop runs end to end against it.
- **Manual / E2E:** a disposable Plex server in Docker with a small music library and two Home users.

## 7. Deployment

```yaml
services:
  syncalist:
    image: ghcr.io/<you>/syncalist:latest
    ports: ["8585:8585"]
    volumes: ["./config:/config"]
    environment:
      - PLEX_URL=http://plex:32400
      - SYNC_INTERVAL=60   # initial default only; change it later in the UI
    restart: unless-stopped
```

## 8. Open questions

1. **Does the no-PIN token act as the user?** The no-PIN token method returned a token, but the first run couldn't tell whose playlists it sees. If it sees the user's own playlists, PINs aren't needed at all and setup is simpler. If not, the PIN method (proven below) stays. Needs one more `--read-only` run of the updated spike.

## 9. M0 spike results (2026-11-16)

Run against server "Delta" (Plex 1.43.5) as Home user Kate (PIN-protected, managed), with python-plexapi 4.18.3.

| Assumption | Result | Effect on the design |
|---|---|---|
| Admin token can list Home users | ✅ 4 users, all managed, 2 with PINs | As planned. |
| Switching to a PIN user with their PIN gives a server token | ✅ `switchHomeUser(user, pin)` + matching resource `accessToken` | **This is the token method.** |
| Reading a user's music playlists | ✅ Fast: an 841-track playlist loads in 0.7 s | Polling every 60 s is cheap. Full reads are fine even for big playlists. |
| `updatedAt` changes on add / remove / move / rename | ✅ Every operation | Change detection by `updatedAt` works. Renames are detectable. |
| Duplicates | Plex **silently ignores** adding a track that's already in the playlist (`updatedAt` still changes) | "No duplicates" comes free. The merge still dedupes, to be safe. Echo suppression must not assume `updatedAt` means the content changed: compare track lists too. |
| Same ratingKey for admin and user | ✅ | Copy tracks by ID, no matching needed. |
| `moveItem` (to top, and after another track) | ✅ | Planner can use single-item moves. |
| Admin token vs user playlists | The admin's playlist list doesn't include them, but the admin token **can** fetch one by its ratingKey | Not relied on. All reads and writes use the user's own token. |
