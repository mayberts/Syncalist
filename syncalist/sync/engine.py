"""The sync engine (M3): keeps every member's copy of a shared playlist the same.

One round for a group:
  1. Detect: ask Plex for each member's playlist summary. Unchanged `updatedAt` = no edits.
  2. Read: for changed members only, read the full track list (verified complete).
  3. Merge: diff each against what they had after our last write; combine (merge.py).
  4. Guard: a round that would remove a lot is paused for the admin to confirm.
  5. Write: back up anyone losing tracks, then update each copy, unless that member edited it
     again during the round (they're picked up next round instead). Every write is read back.
"""

from dataclasses import dataclass, field
from datetime import datetime

from ..models import GroupMember, HomeUser, ShareGroup, utcnow
from ..plex.gateway import PlaylistSummary, PlexError
from ..sharing import ReadMismatch, ShareError, ShareService
from ..users import UserNeedsAttention
from .merge import Edit, merge

MASS_REMOVAL_MIN = 5  # pause if a round removes more than this many tracks...
MASS_REMOVAL_FRACTION = 0.5  # ...and more than this fraction of the playlist


@dataclass
class SyncReport:
    group: str
    changed: bool = False
    paused: bool = False
    messages: list[str] = field(default_factory=list)


class SyncService:
    def __init__(self, shares: ShareService):
        self.shares = shares
        self.session = shares.session
        self.gateway = shares.gateway
        self.users = shares.users

    def sync_all(self) -> list[SyncReport]:
        reports = []
        for group in self.shares.groups():
            try:
                reports.append(self.sync_group(group))
            except Exception as e:  # noqa: BLE001 - one bad group must not stop the others
                self.session.rollback()
                self.shares._log(group, f"Sync failed: {type(e).__name__}: {e}", level="error")
                reports.append(SyncReport(group.name, messages=[str(e)]))
        return reports

    # ------------------------------------------------------------------------------------------
    def sync_group(self, group: ShareGroup, force: bool = False) -> SyncReport:
        report = SyncReport(group.name)
        if group.paused_reason and not force:
            report.paused = True
            return report
        base = list(group.base_snapshot)

        # 1-2. detect and read --------------------------------------------------------------------
        seen: dict[int, tuple[GroupMember, HomeUser, PlaylistSummary]] = {}
        current: dict[int, list[int]] = {}  # member id -> tracks now (read or assumed)
        edits: list[Edit] = []
        blocked: set[int] = set()
        read_ids: set[int] = set()
        renames: list[tuple[datetime | None, str, str]] = []
        for m in self.shares.members(group):
            user = self.shares._user(m.user_id)
            try:
                summary = self.users.call_as(user, lambda t: self.gateway.playlist(t, m.playlist_key))
            except (UserNeedsAttention, PlexError) as e:
                self._member_error(group, m, user, f"Couldn't check {user.title}'s copy: {e}")
                continue
            if summary is None:
                self.session.delete(m)
                self.session.commit()
                self.shares._log(group, f"{user.title} deleted their copy, so they've left the share", user)
                report.changed = True
                continue
            seen[m.id] = (m, user, summary)
            if summary.title != group.name:
                renames.append((summary.updated_at, user.title, summary.title))

            prev = m.last_known if m.last_known is not None else [k for k in base if k not in set(m.skipped)]
            unchanged = (m.status == "ok" and m.last_seen_updated_at is not None
                         and summary.updated_at == m.last_seen_updated_at)
            if unchanged:
                current[m.id] = prev
                continue
            try:
                keys = self.shares._read_keys(user, summary, group)
            except ReadMismatch as e:
                self._member_error(group, m, user, str(e))
                blocked.add(m.id)
                continue
            current[m.id] = keys
            read_ids.add(m.id)
            if keys != prev:
                edits.append(Edit(user.title, prev, keys, summary.updated_at))

        # 3. merge --------------------------------------------------------------------------------------
        result = merge(base, edits)
        new_name = max(renames, key=lambda r: (r[0] is not None, r[0]))[2] if renames else None
        removed = [k for k in base if k not in set(result.tracks)]

        if result.tracks == base and new_name is None:
            # Nothing to share. Members we read (e.g. a no-op touch, or an edit that was undone)
            # just have their state recorded; _write_member only writes if a copy differs.
            for mid in read_ids - blocked:
                self._write_member(group, *seen[mid], current[mid])
            group.last_synced_at = utcnow()
            self.session.add(group)
            self.session.commit()
            return report

        # 4. guard ---------------------------------------------------------------------------------------
        if not force and len(removed) > MASS_REMOVAL_MIN and len(removed) > MASS_REMOVAL_FRACTION * len(base):
            who = ", ".join(f"{name} removed {len(ks)}" for name, ks in result.removed.items() if ks)
            group.paused_reason = (f"This sync would remove {len(removed)} of {len(base)} tracks ({who}). "
                                   "Apply it, or undo it to put everyone back to the last synced version.")
            self.session.add(group)
            self.session.commit()
            self.shares._log(group, "Sync paused: " + group.paused_reason, level="warning")
            report.paused = True
            return report

        # 5. write -----------------------------------------------------------------------------------------
        self._log_changes(group, result, new_name)
        group.base_snapshot = result.tracks
        if new_name:
            group.name = new_name
        group.paused_reason = None
        self.session.add(group)
        self.session.commit()
        report.changed = True

        for mid, (m, user, summary) in seen.items():
            if mid in blocked:
                continue
            self._write_member(group, m, user, summary, current[mid])
        group.last_synced_at = utcnow()
        self.session.add(group)
        self.session.commit()
        return report

    def discard_pending(self, group: ShareGroup) -> None:
        """Undo a paused round: put every member back to the last synced list."""
        for m in self.shares.members(group):
            user = self.shares._user(m.user_id)
            try:
                summary = self.users.call_as(user, lambda t: self.gateway.playlist(t, m.playlist_key))
                if summary is None:
                    continue
                keys = self.shares._read_keys(user, summary, group)
            except (UserNeedsAttention, PlexError, ShareError) as e:
                self._member_error(group, m, user, str(e))
                continue
            self._write_member(group, m, user, summary, keys, check_unchanged=False)
        group.paused_reason = None
        group.last_synced_at = utcnow()
        self.session.add(group)
        self.session.commit()
        self.shares._log(group, "Undid the paused changes: everyone is back to the last synced version")

    # ------------------------------------------------------------------------------------------------------
    def _write_member(self, group: ShareGroup, m: GroupMember, user: HomeUser, summary: PlaylistSummary,
                      current: list[int], check_unchanged: bool = True) -> None:
        want = [k for k in group.base_snapshot if k not in set(m.skipped)]
        if current == want and summary.title == group.name:
            m.last_known = current
            m.last_seen_updated_at = summary.updated_at
            m.status, m.status_detail = "ok", None
            self.session.add(m)
            self.session.commit()
            return
        try:
            if check_unchanged:
                now = self.users.call_as(user, lambda t: self.gateway.playlist(t, m.playlist_key))
                if now is None or now.updated_at != summary.updated_at:
                    self.shares._log(group, f"{user.title} changed their copy during the sync; "
                                            "it'll be updated next round", user)
                    return
            if current != want:
                lost = [k for k in current if k not in set(group.base_snapshot)]
                if lost:
                    self.shares._backup(user, summary, current,
                                        reason=f"Before sync removed {len(lost)} track(s) from shared “{group.name}”")
            skipped = self.users.call_as(user, lambda t: self.gateway.set_playlist_tracks(
                t, m.playlist_key, group.base_snapshot, allow_removals=True))
            if summary.title != group.name:
                self.users.call_as(user, lambda t: self.gateway.rename_playlist(t, m.playlist_key, group.name))
            actual = self.users.call_as(user, lambda t: self.gateway.playlist_track_keys(t, m.playlist_key))
            after = self.users.call_as(user, lambda t: self.gateway.playlist(t, m.playlist_key))
        except (UserNeedsAttention, PlexError) as e:
            self._member_error(group, m, user, f"Couldn't update {user.title}'s copy: {e}")
            return
        expected = [k for k in group.base_snapshot if k not in set(skipped)]
        m.skipped = skipped
        m.last_known = actual
        m.last_seen_updated_at = after.updated_at if after else None
        if actual != expected:
            m.status = "warning"
            m.status_detail = f"After updating, {user.title}'s copy has {len(actual)} tracks; expected {len(expected)}"
            self.shares._log(group, m.status_detail, user, level="warning")
        else:
            m.status, m.status_detail = "ok", None
        self.session.add(m)
        self.session.commit()

    def _member_error(self, group: ShareGroup, m: GroupMember, user: HomeUser, message: str) -> None:
        if m.status_detail != message:  # don't repeat the same error every round
            self.shares._log(group, message, user, level="error")
        m.status, m.status_detail = "error", message
        self.session.add(m)
        self.session.commit()

    def _log_changes(self, group: ShareGroup, result, new_name: str | None) -> None:
        names = self._titles([k for ks in list(result.added.values()) + list(result.removed.values()) for k in ks])
        for who, ks in result.added.items():
            if ks:
                self.shares._log(group, f"{who} added {_describe(ks, names)}")
        for who, ks in result.removed.items():
            if ks:
                self.shares._log(group, f"{who} removed {_describe(ks, names)}")
        if result.order_from:
            self.shares._log(group, f"{result.order_from} reordered the playlist")
        if new_name:
            self.shares._log(group, f"Renamed from “{group.name}” to “{new_name}”")

    def _titles(self, keys: list[int]) -> dict[int, str]:
        if not keys or len(keys) > 50:
            return {}
        try:
            return {t.key: f"“{t.title}”" for t in self.gateway.tracks_info(keys)}
        except PlexError:
            return {}


def _describe(keys: list[int], names: dict[int, str]) -> str:
    if len(keys) <= 3 and all(k in names for k in keys):
        return ", ".join(names[k] for k in keys)
    return f"{len(keys)} track{'s' if len(keys) != 1 else ''}"
