"""Process configuration.

Only infrastructure lives here (paths, bind address, secret key). Everything the
user can change at runtime — mailbox, parse rules, Discord channel, lapse window —
lives in the `settings` table so it is editable from the UI without a restart.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RM_", env_file=".env", extra="ignore")

    # Everything mutable lives under here so a container rebuild is survivable.
    data_dir: Path = Path("/data")

    host: str = "0.0.0.0"
    port: int = 8080

    # Trusted origin for CSRF/cookie hardening. Set to the real https:// URL in prod.
    base_url: str = "http://localhost:8080"

    # Marks the session cookie Secure, so the browser only ever sends it over
    # TLS. Turn this on when a reverse proxy terminates HTTPS in front of the
    # app — and only then: with it on, plain-HTTP access cannot log in at all,
    # because the browser withholds the cookie.
    secure_cookies: bool = False

    # Which upstreams may set X-Forwarded-For / X-Forwarded-Proto.
    #
    # "*" trusts those headers from anything that can reach the port, which lets
    # a direct caller forge its apparent client IP in the logs and in login
    # throttling. Behind a reverse proxy, set this to the proxy's IP.
    forwarded_allow_ips: str = "*"

    session_ttl_hours: int = 24 * 14
    login_max_attempts: int = 8
    login_lockout_minutes: int = 15

    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "receiptmanager.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+pysqlite:///{self.db_path}"

    @property
    def receipts_dir(self) -> Path:
        return self.data_dir / "receipts"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def tmp_dir(self) -> Path:
        # Same filesystem as receipts_dir so the final move is an atomic rename.
        return self.data_dir / "tmp"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret.key"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.receipts_dir,
            self.thumbs_dir,
            self.tmp_dir,
            self.backup_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def load_or_create_secret_key(self) -> bytes:
        """Fernet key used to encrypt secrets at rest (Graph client secret, bot token).

        Kept outside the database on purpose: a leaked DB backup is then not enough
        to recover credentials.
        """
        from cryptography.fernet import Fernet

        path = self.secret_key_path
        if path.exists():
            return path.read_bytes().strip()

        key = Fernet.generate_key()
        # Create 0600 from the outset — never briefly world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key


@lru_cache(maxsize=1)
def get_config() -> Config:
    cfg = Config()
    cfg.ensure_dirs()
    return cfg


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)
