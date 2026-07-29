"""Password hashing, sessions, CSRF, and secret encryption.

"It's only on my LAN" is not a security boundary — this process holds a Microsoft
Graph credential, a Discord bot token, and the company's financial records.
"""

from __future__ import annotations

import datetime as dt
import hmac
import logging
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.config import get_config
from app.models import Session, Setting, User, utcnow

logger = logging.getLogger(__name__)

SESSION_COOKIE = "rm_session"

_hasher = PasswordHasher()
_fernet: Fernet | None = None


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def validate_password_strength(password: str) -> str | None:
    """Return an error message, or None if acceptable.

    Length is the only requirement that reliably matters; composition rules push
    people toward predictable substitutions.
    """
    if len(password) < 12:
        return "Password must be at least 12 characters."
    if len(password) > 1024:
        return "Password must be at most 1024 characters."
    return None


# --------------------------------------------------------------------------- #
# Secret encryption at rest
# --------------------------------------------------------------------------- #

def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(get_config().load_or_create_secret_key())
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        # Almost always a restored DB paired with a lost/rotated secret.key.
        raise RuntimeError(
            "Unable to decrypt a stored secret. The data directory's secret.key "
            "does not match the database — restore the matching key or re-enter "
            "the credentials in Settings."
        ) from exc


# --------------------------------------------------------------------------- #
# Settings access (secrets transparently encrypted)
# --------------------------------------------------------------------------- #

def get_setting(db: OrmSession, key: str, default: str | None = None) -> str | None:
    """Read a setting, decrypting it if it is a secret.

    An undecryptable secret returns the default rather than raising. That case
    means the data directory's ``secret.key`` no longer matches the database —
    usually a restore without the key. Raising here would 500 the Settings and
    Health pages, i.e. exactly the two pages needed to diagnose and fix it, so
    the app degrades to "this credential is unset" and logs loudly instead.
    :func:`secret_is_unreadable` lets the health page report it honestly.
    """
    row = db.get(Setting, key)
    if row is None or row.value is None:
        return default
    if row.is_secret:
        try:
            return decrypt_secret(row.value)
        except RuntimeError:
            logger.error(
                "Cannot decrypt setting %r — secret.key does not match the database. "
                "Re-enter this credential in Settings.", key,
            )
            return default
    return row.value


def secret_is_unreadable(db: OrmSession, key: str) -> bool:
    """True when a secret is stored but cannot be decrypted with the current key."""
    row = db.get(Setting, key)
    if row is None or not row.value or not row.is_secret:
        return False
    try:
        decrypt_secret(row.value)
    except RuntimeError:
        return True
    return False


def set_setting(db: OrmSession, key: str, value: str | None, *, is_secret: bool = False) -> None:
    row = db.get(Setting, key)
    stored = encrypt_secret(value) if (is_secret and value) else value
    if row is None:
        db.add(Setting(key=key, value=stored, is_secret=is_secret))
    else:
        row.value = stored
        row.is_secret = is_secret
        row.updated_at = utcnow()


def has_setting(db: OrmSession, key: str) -> bool:
    row = db.get(Setting, key)
    return row is not None and bool(row.value)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

def is_setup_complete(db: OrmSession) -> bool:
    return db.scalar(select(User.id).limit(1)) is not None


def create_session(
    db: OrmSession, user: User, *, user_agent: str | None = None, ip: str | None = None
) -> Session:
    cfg = get_config()
    session = Session(
        id=secrets.token_urlsafe(48),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        # A user with TOTP enrolled starts half-authenticated; the guard in
        # app.web.deps refuses every route but the code form until this clears.
        totp_pending=bool(user.totp_secret),
        expires_at=utcnow() + dt.timedelta(hours=cfg.session_ttl_hours),
        user_agent=(user_agent or "")[:255] or None,
        ip=(ip or "")[:64] or None,
    )
    db.add(session)
    return session


def load_session(db: OrmSession, token: str | None) -> Session | None:
    if not token:
        return None
    session = db.get(Session, token)
    if session is None:
        return None
    if session.expires_at <= utcnow():
        db.delete(session)
        return None
    return session


def destroy_session(db: OrmSession, token: str | None) -> None:
    if not token:
        return
    session = db.get(Session, token)
    if session is not None:
        db.delete(session)


def check_csrf(session: Session, submitted: str | None) -> bool:
    if not submitted:
        return False
    return hmac.compare_digest(session.csrf_token, submitted)


# --------------------------------------------------------------------------- #
# TOTP
# --------------------------------------------------------------------------- #

def new_totp_secret() -> str:
    import pyotp

    return str(pyotp.random_base32())


def totp_uri(secret: str, username: str, issuer: str = "ReceiptManager") -> str:
    import pyotp

    return str(pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer))


def verify_totp(secret: str, code: str) -> bool:
    """Check a 6-digit code, tolerating one step of clock drift each way.

    ``valid_window=1`` accepts the adjacent 30-second windows, which is the usual
    trade-off: without it, a phone whose clock is a few seconds off can never log
    in; much wider and the code stays usable long after it is shown.
    """
    import pyotp

    cleaned = (code or "").strip().replace(" ", "").replace("-", "")
    if not cleaned.isdigit():
        return False
    try:
        return bool(pyotp.TOTP(secret).verify(cleaned, valid_window=1))
    except Exception:
        return False


def totp_qr_svg(uri: str) -> str:
    """Inline SVG QR code.

    Rendered as SVG rather than a PNG data URI so the enrollment secret never
    touches the filesystem and stays inside the authenticated response.
    """
    import io

    import qrcode
    import qrcode.image.svg

    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


# --------------------------------------------------------------------------- #
# Login throttling
# --------------------------------------------------------------------------- #

def is_locked_out(user: User) -> bool:
    return user.locked_until is not None and user.locked_until > utcnow()


def register_failed_login(db: OrmSession, user: User) -> None:
    cfg = get_config()
    user.failed_logins += 1
    if user.failed_logins >= cfg.login_max_attempts:
        user.locked_until = utcnow() + dt.timedelta(minutes=cfg.login_lockout_minutes)
        user.failed_logins = 0
    db.add(user)


def register_successful_login(db: OrmSession, user: User) -> None:
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    db.add(user)
