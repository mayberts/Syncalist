"""Creating and managing shared playlists (M2).

A share group links one real Plex playlist per member. Sharing:
  1. backs up every existing playlist that will change,
  2. works out the shared track list (the source, plus merged-in tracks),
  3. creates or links each member's playlist and makes it match,
  4. records each member's Plex `updatedAt` so the sync engine (M3) can tell
     our own writes apart from users' edits.
"""

from dataclasses import dataclass
from enum import Enum

from sqlmodel import Session, select

from .models import GroupMember, HomeUser, PlaylistBackup, ShareGroup, SyncEvent, utcnow
from .plex.gateway import PlaylistSummary, PlexError
from .sync.planner import dedupe, union
from .users import UserNeedsAttention, UserService


class ShareError(Exception):
    pass


class Mode(str, Enum):
    NEW = "new"  # create a new playlist for the recipient
    MERGE = "merge"  # link their same-name playlist and combine tracks
    REPLACE = "replace"  # link their same-name playlist and overwrite it


@dataclass
class Candidate:
    """A user who could be added to a share, and their same-name playlist if any."""
    user: HomeUser
    existing: PlaylistSummary | None = None
    existing_is_shared: bool = False
    error: str | None = None


class ShareService:
    def __init__(self, session: Session, users: UserService):
        self.session = session
        self.users = users
        self.gateway = users.gateway

    # -- queries ---------------------------------------------------------------------------
    def groups(self) -> list[ShareGroup]:
        return list(self.session.exec(select(ShareGroup).order_by(ShareGroup.name)).all())

    def members(self, group: ShareGroup) -> list[GroupMember]:
        return list(self.session.exec(select(GroupMember).where(GroupMember.group_id == group.id)).all())

    def member_index(self) -> dict[tuple[int, int], GroupMember]:
        """(user_id, playlist_key) -> membership, for marking shared playlists."""
        return {(m.user_id, m.playlist_key): m for m in self.session.exec(select(GroupMember)).all()}

    def events(self, group: ShareGroup | None = None, limit: int = 50) -> list[SyncEvent]:
        q = select(SyncEvent).order_by(SyncEvent.at.desc(), SyncEvent.id.desc()).limit(limit)
        if group is not None:
            q = q.where(SyncEvent.group_id == group.id)
        return list(self.session.exec(q).all())

    def candidates(self, title: str, exclude_user_ids: set[int]) -> list[Candidate]:
        shared = self.member_index()
        out = []
        for user in self.users.all():
            if user.id in exclude_user_ids:
                continue
            cand = Candidate(user=user)
            try:
                for p in self.users.call_as(user, self.gateway.playlists):
                    if not p.smart and p.title.strip().lower() == title.strip().lower():
                        cand.existing = p
                        cand.existing_is_shared = (user.id, p.key) in shared
                        break
            except (UserNeedsAttention, PlexError) as e:
                cand.error = str(e)
            out.append(cand)
        return out

    # -- sharing ---------------------------------------------------------------------------
    def share(self, owner: HomeUser, playlist_key: int, recipients: dict[int, Mode]) -> ShareGroup:
        """Share one of owner's playlists with recipients (user_id -> how to handle a same-name playlist)."""
        if not recipients:
            raise ShareError("Pick at least one person to share with.")
        if (owner.id, playlist_key) in self.member_index():
            raise ShareError("That playlist is already shared. Add people from its shared playlist page.")
        source = self._summary(owner, playlist_key)
        if source.smart:
            raise ShareError("Smart playlists can't be shared.")
        tracks = self.users.call_as(owner, lambda t: self.gateway.playlist_track_keys(t, playlist_key))
        if not tracks:
            raise ShareError("That playlist is empty. Add some tracks before sharing it.")

        group = ShareGroup(name=source.title, owner_user_id=owner.id, base_snapshot=dedupe(tracks))
        self.session.add(group)
        self.session.commit()
        self.session.refresh(group)
        owner_member = GroupMember(group_id=group.id, user_id=owner.id, playlist_key=playlist_key,
                                   last_seen_updated_at=source.updated_at)
        self.session.add(owner_member)
        self.session.commit()
        self._log(group, f"{owner.title} shared “{group.name}” ({len(group.base_snapshot)} tracks)", owner)
        try:
            self._add(group, recipients, existing_members=[owner_member])
        except ShareError:
            self._discard(group)
            raise
        return group

    def add_members(self, group: ShareGroup, recipients: dict[int, Mode]) -> None:
        if not recipients:
            raise ShareError("Pick at least one person to add.")
        current = self.members(group)
        already = {m.user_id for m in current}
        recipients = {uid: mode for uid, mode in recipients.items() if uid not in already}
        self._add(group, recipients, existing_members=current)

    def _add(self, group: ShareGroup, recipients: dict[int, Mode], existing_members: list[GroupMember]) -> None:
        shared = self.member_index()
        new_members: list[GroupMember] = []
        merged_sources: list[list[int]] = []

        for user_id, mode in recipients.items():
            user = self._user(user_id)
            try:
                if mode is Mode.NEW:
                    summary = self.users.call_as(
                        user, lambda t: self.gateway.create_playlist(t, group.name, group.base_snapshot))
                    member = GroupMember(group_id=group.id, user_id=user.id, playlist_key=summary.key)
                    self._log(group, f"Created “{group.name}” for {user.title}", user)
                else:
                    existing = self._same_name(user, group.name)
                    if existing is None:
                        raise ShareError(f"{user.title} no longer has a playlist called “{group.name}”.")
                    if (user.id, existing.key) in shared:
                        raise ShareError(f"{user.title}'s “{existing.title}” is already in another shared playlist.")
                    keys = self.users.call_as(user, lambda t: self.gateway.playlist_track_keys(t, existing.key))
                    self._backup(user, existing, keys, reason=f"Before {mode.value} into shared “{group.name}”")
                    if mode is Mode.MERGE:
                        merged_sources.append(keys)
                    member = GroupMember(group_id=group.id, user_id=user.id, playlist_key=existing.key)
                    verb = "Merged" if mode is Mode.MERGE else "Replaced"
                    self._log(group, f"{verb} {user.title}'s “{existing.title}” ({len(keys)} tracks)", user)
            except (ShareError, UserNeedsAttention, PlexError) as e:
                self._log(group, f"Couldn't add {user.title}: {e}", user, level="error")
                continue
            self.session.add(member)
            self.session.commit()
            new_members.append(member)

        if not new_members:
            raise ShareError("Nobody could be added. See the activity log for details.")

        # Merges change the shared list, so every member (including existing ones) is updated.
        if merged_sources:
            before = len(group.base_snapshot)
            group.base_snapshot = union(group.base_snapshot, *merged_sources)
            self._log(group, f"Shared list now has {len(group.base_snapshot)} tracks "
                             f"({len(group.base_snapshot) - before} added by merging)")
            for m in existing_members:
                user = self._user(m.user_id)
                summary = self._summary(user, m.playlist_key)
                keys = self.users.call_as(user, lambda t: self.gateway.playlist_track_keys(t, m.playlist_key))
                self._backup(user, summary, keys, reason=f"Before merge into shared “{group.name}”")
            to_push = existing_members + new_members
        else:
            to_push = new_members

        for m in to_push:
            self._push(group, m)
        group.last_synced_at = utcnow()
        self.session.add(group)
        self.session.commit()

    def _push(self, group: ShareGroup, member: GroupMember) -> None:
        """Make one member's playlist match the shared list and record its updatedAt."""
        user = self._user(member.user_id)
        try:
            skipped = self.users.call_as(
                user, lambda t: self.gateway.set_playlist_tracks(t, member.playlist_key, group.base_snapshot))
            summary = self._summary(user, member.playlist_key)
            member.skipped = skipped
            member.last_seen_updated_at = summary.updated_at
            member.status, member.status_detail = "ok", None
            if skipped:
                self._log(group, f"{len(skipped)} track(s) skipped for {user.title}: no access to them", user,
                          level="warning")
        except (UserNeedsAttention, PlexError, ShareError) as e:
            member.status, member.status_detail = "error", str(e)
            self._log(group, f"Couldn't update {user.title}'s playlist: {e}", user, level="error")
        self.session.add(member)
        self.session.commit()

    # -- leaving / stopping ---------------------------------------------------------------------
    def remove_member(self, group: ShareGroup, user_id: int, delete_playlist: bool) -> None:
        member = self.session.exec(select(GroupMember).where(
            GroupMember.group_id == group.id, GroupMember.user_id == user_id)).first()
        if member is None:
            return
        user = self._user(user_id)
        if delete_playlist:
            try:
                summary = self._summary(user, member.playlist_key)
                keys = self.users.call_as(user, lambda t: self.gateway.playlist_track_keys(t, member.playlist_key))
                self._backup(user, summary, keys, reason=f"Before deleting (left shared “{group.name}”)")
                self.users.call_as(user, lambda t: self.gateway.delete_playlist(t, member.playlist_key))
            except (UserNeedsAttention, PlexError, ShareError) as e:
                raise ShareError(f"Couldn't delete {user.title}'s copy: {e}") from e
        self.session.delete(member)
        self.session.commit()
        what = "removed and their copy deleted" if delete_playlist else "removed; they keep their copy"
        self._log(group, f"{user.title} {what}", user)

    def stop_sharing(self, group: ShareGroup) -> None:
        """Dissolve the group. Every member keeps their playlist as a normal one."""
        self._log(None, f"Stopped sharing “{group.name}”. Everyone keeps their own copy.")
        self._discard(group)

    def _discard(self, group: ShareGroup) -> None:
        for m in self.members(group):
            self.session.delete(m)
        for e in self.session.exec(select(SyncEvent).where(SyncEvent.group_id == group.id)).all():
            e.group_id = None
            self.session.add(e)
        self.session.delete(group)
        self.session.commit()

    # -- backups ------------------------------------------------------------------------------------
    def backups(self) -> list[PlaylistBackup]:
        return list(self.session.exec(select(PlaylistBackup).order_by(PlaylistBackup.taken_at.desc())).all())

    def restore(self, backup: PlaylistBackup) -> PlaylistSummary:
        """Restore a backup as a NEW playlist, so it never fights with a shared copy."""
        user = self._user(backup.user_id)
        title = f"{backup.title} (restored {backup.taken_at:%Y-%m-%d})"
        try:
            summary = self.users.call_as(user, lambda t: self.gateway.create_playlist(t, title, backup.track_keys))
        except (UserNeedsAttention, PlexError) as e:
            raise ShareError(f"Couldn't restore: {e}") from e
        self._log(None, f"Restored “{backup.title}” for {user.title} as “{title}”", user)
        return summary

    # -- helpers ------------------------------------------------------------------------------------
    def _user(self, user_id: int) -> HomeUser:
        user = self.session.get(HomeUser, user_id)
        if user is None:
            raise ShareError(f"Unknown Home user {user_id}. Try “Refresh users”.")
        return user

    def _summary(self, user: HomeUser, playlist_key: int) -> PlaylistSummary:
        summary = self.users.call_as(user, lambda t: self.gateway.playlist(t, playlist_key))
        if summary is None:
            raise ShareError(f"{user.title}'s playlist no longer exists.")
        return summary

    def _same_name(self, user: HomeUser, title: str) -> PlaylistSummary | None:
        for p in self.users.call_as(user, self.gateway.playlists):
            if not p.smart and p.title.strip().lower() == title.strip().lower():
                return p
        return None

    def _backup(self, user: HomeUser, summary: PlaylistSummary, keys: list[int], reason: str) -> None:
        self.session.add(PlaylistBackup(user_id=user.id, user_title=user.title, playlist_key=summary.key,
                                        title=summary.title, track_keys=keys, reason=reason))
        self.session.commit()

    def _log(self, group: ShareGroup | None, message: str, user: HomeUser | None = None,
             level: str = "info") -> None:
        self.session.add(SyncEvent(group_id=group.id if group else None, user_id=user.id if user else None,
                                   message=message, level=level))
        self.session.commit()
