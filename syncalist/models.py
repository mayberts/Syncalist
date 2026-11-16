"""Database tables."""

from datetime import datetime, timezone

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
