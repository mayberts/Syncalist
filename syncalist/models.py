"""Database tables."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AppSettings(SQLModel, table=True):
    """Single-row table holding the app's configuration."""

    id: int = Field(default=1, primary_key=True)
    client_id: str  # X-Plex-Client-Identifier for this Syncalist install
    plex_url: str | None = None
    server_name: str | None = None
    machine_id: str | None = None
    admin_account_id: int | None = None
    admin_title: str | None = None
    admin_token_enc: str | None = None
    music_section_key: str | None = None
    music_section_title: str | None = None
    poll_interval: int = 60
    # Status of the most recent full sync round (all shared playlists).
    last_round_at: datetime | None = None
    last_round_ok: bool | None = None
    last_round_summary: str | None = None
    log_retention_days: int | None = None  # None = default (90)

    @property
    def is_configured(self) -> bool:
        return bool(self.admin_account_id and self.plex_url and self.music_section_key)


class HomeUser(SQLModel, table=True):
    """A Plex Home user (including the admin), cached from plex.tv."""

    id: int = Field(primary_key=True)  # plex.tv user id
    title: str
    thumb: str | None = None
    is_admin: bool = False
    has_pin: bool = False
    token_enc: str | None = None  # server access token for this user
    fallback_pin_enc: str | None = None  # only used if fetching a token without a PIN fails
    status: str = "ok"  # ok | needs_attention
    status_detail: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class ShareGroup(SQLModel, table=True):
    """One shared playlist: a set of real Plex playlists (one per member) kept identical."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    owner_user_id: int  # who shared it first
    base_snapshot: list[int] = Field(default_factory=list, sa_column=Column(JSON))  # last agreed track list
    created_at: datetime = Field(default_factory=utcnow)
    last_synced_at: datetime | None = None
    # Set when a sync was held back for confirmation (e.g. it would remove many tracks).
    paused_reason: str | None = None


class GroupMember(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("group_id", "user_id"), UniqueConstraint("user_id", "playlist_key"))

    id: int | None = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="sharegroup.id", index=True)
    user_id: int = Field(foreign_key="homeuser.id")
    playlist_key: int
    last_seen_updated_at: datetime | None = None  # Plex updatedAt after our last write (echo suppression)
    skipped: list[int] = Field(default_factory=list, sa_column=Column(JSON))  # tracks this user can't access
    # Exactly what this member's playlist held after our last write/read. Each sync diffs the
    # member against this (not the group list), so skipped or not-yet-received tracks never
    # look like removals. None = unknown (older installs): fall back to the group list.
    last_known: list[int] | None = Field(default=None, sa_column=Column(JSON))
    status: str = "ok"  # ok | error
    status_detail: str | None = None
    joined_at: datetime = Field(default_factory=utcnow)


class PlaylistBackup(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int
    user_title: str
    playlist_key: int
    title: str
    track_keys: list[int] = Field(default_factory=list, sa_column=Column(JSON))
    reason: str
    taken_at: datetime = Field(default_factory=utcnow)


class SyncEvent(SQLModel, table=True):
    """Activity log entry."""

    id: int | None = Field(default=None, primary_key=True)
    group_id: int | None = Field(default=None, index=True)
    user_id: int | None = None
    message: str
    level: str = "info"  # info | warning | error | detail (per-copy write lines, folded in the UI)
    round_id: str | None = Field(default=None, index=True)  # entries from one sync round share this
    at: datetime = Field(default_factory=utcnow)
