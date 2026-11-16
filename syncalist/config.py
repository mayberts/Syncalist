"""Runtime configuration, read from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("SYNCALIST_DATA", "/config")))
    # Only a default for first-time setup; the URL chosen in setup is stored in the database.
    plex_url: str | None = field(default_factory=lambda: os.environ.get("PLEX_URL") or None)
    # Initial poll interval; can be changed later in the web UI.
    sync_interval: int = field(default_factory=lambda: int(os.environ.get("SYNC_INTERVAL", "60")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "syncalist.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "secret.key"
