"""Encryption for secrets stored in the database (Plex tokens, fallback PINs).

The key is generated on first run and kept in its own file next to the database,
so a copy of the database alone doesn't expose any tokens.
"""

import hashlib
import hmac
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class Secrets:
    def __init__(self, key_path: Path):
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if not key_path.exists():
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        self._key = key_path.read_bytes().strip()
        self._fernet = Fernet(self._key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            return None

    @property
    def session_secret(self) -> str:
        """Key for signing the browser session cookie, derived from the main key."""
        return hmac.new(self._key, b"syncalist-session", hashlib.sha256).hexdigest()
