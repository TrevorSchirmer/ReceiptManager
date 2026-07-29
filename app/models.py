"""Database schema.

Design notes that matter:

* Money is stored as ``amount_minor`` (integer cents) plus an ISO currency code.
  Never floats — this is a financial record.
* All timestamps are timezone-aware UTC. The business timezone is a display concern.
* ``Attachment.transaction_id`` is nullable *on purpose*: a NULL row is the orphan
  queue, i.e. a receipt we captured but could not confidently match.
* ``RawEmail`` keeps the original body forever so parse rules can be fixed and
  replayed after the fact.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes that survive a round trip through SQLite.

    SQLite has no native timestamp type and silently discards ``tzinfo``, so a
    plain ``DateTime(timezone=True)`` column hands back *naive* values on read.
    Comparing one of those against an aware ``utcnow()`` raises TypeError — which
    would break session expiry, the lapse sweep, and the heartbeat, all of which
    do exactly that comparison.

    So: normalise to UTC and store naive on the way in, re-attach UTC on the way
    out. Everything above this layer can then assume aware UTC unconditionally.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC).replace(tzinfo=None)

    def process_result_value(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


class EnumString(TypeDecorator):
    """Store an enum by its ``.value``, and return it as the enum on read.

    A plain ``String`` column round-trips a ``str``-mixin enum into a bare string,
    because SQLite hands back whatever text it stored. That looks harmless until a
    template evaluates ``tx.status.value`` — ``str`` has no ``.value``, Jinja2
    swallows the AttributeError into an empty string, and every status badge
    silently renders blank with no error anywhere.

    Binding accepts either an enum member or its raw string, so query filters can
    keep using ``.in_(["new", "notified"])``.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[enum.Enum], length: int = 32) -> None:
        self.enum_cls = enum_cls
        super().__init__(length)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_cls):
            return str(value.value)
        return str(self.enum_cls(value).value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        return self.enum_cls(value)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON}


class TransactionStatus(str, enum.Enum):
    new = "new"                                # parsed, not yet posted to Discord
    notified = "notified"                      # posted, awaiting a receipt
    receipt_attached = "receipt_attached"      # at least one receipt captured
    verified = "verified"                      # human confirmed in the UI
    lapsed = "lapsed"                          # aged out of implicit matching (§7)
    no_receipt_required = "no_receipt_required"
    needs_attention = "needs_attention"        # parse failed; raw body retained
    ignored = "ignored"

    @classmethod
    def open_statuses(cls) -> tuple["TransactionStatus", ...]:
        """Statuses eligible for *implicit* matching and for nudging.

        Deliberately excludes ``lapsed``: a lapsed charge can still be matched by
        an explicit ``#code`` or from the UI, but it no longer clutters the select
        menu or triggers reminders.
        """
        return (cls.new, cls.notified)


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    dead = "dead"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # random token
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    csrf_token: Mapped[str] = mapped_column(String(64))
    # True between a correct password and a correct TOTP code. Such a session can
    # reach only the TOTP form — every other route bounces it back.
    totp_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255))
    ip: Mapped[Optional[str]] = mapped_column(String(64))


class Setting(Base):
    """Runtime configuration, editable from the UI.

    ``is_secret`` values are Fernet-encrypted at rest and are never rendered back
    to the browser — the UI shows a masked placeholder and only writes on change.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class ParseRule(Base):
    __tablename__ = "parse_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower wins

    # Pre-filters. Empty string means "match anything".
    sender_match: Mapped[str] = mapped_column(String(255), default="")
    subject_match: Mapped[str] = mapped_column(String(255), default="")
    match_is_regex: Mapped[bool] = mapped_column(Boolean, default=False)

    # Regex with named groups: merchant, amount, currency, card_ending,
    # cardholder, occurred_at. Applied to the normalized plain-text body.
    # `card_last4` is accepted as an alias for `card_ending`.
    body_regex: Mapped[str] = mapped_column(Text)

    # Fallbacks for fields the regex does not capture.
    default_currency: Mapped[str] = mapped_column(String(3), default="USD")
    # strptime format for occurred_at; NULL -> dateutil fuzzy parse.
    date_format: Mapped[Optional[str]] = mapped_column(String(64))

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)


class MerchantRule(Base):
    """Auto-handling for merchants you never need a receipt from.

    Recurring SaaS charges are the main case: without this the bot nags every
    month for a receipt that will never come, and the noise trains you to ignore
    the channel — which is how real receipts start getting missed.
    """

    __tablename__ = "merchant_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    pattern: Mapped[str] = mapped_column(String(255))
    is_regex: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # When true the charge is filed as no_receipt_required and never announced.
    skip_receipt: Mapped[bool] = mapped_column(Boolean, default=True)
    # Optional GL category applied automatically.
    category: Mapped[Optional[str]] = mapped_column(String(64))
    note: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    def matches(self, merchant: str) -> bool:
        import re

        if not self.enabled or not self.pattern:
            return False
        candidate = merchant or ""
        if self.is_regex:
            try:
                return re.search(self.pattern, candidate, re.IGNORECASE) is not None
            except re.error:
                return False  # a bad pattern must never break ingest
        return self.pattern.lower() in candidate.lower()


class RawEmail(Base):
    __tablename__ = "emails_raw"
    __table_args__ = (Index("ix_emails_received", "received_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # The idempotency key. Graph delta queries redeliver; this makes that harmless.
    internet_message_id: Mapped[str] = mapped_column(String(512), unique=True)
    graph_message_id: Mapped[Optional[str]] = mapped_column(String(512))
    folder_id: Mapped[Optional[str]] = mapped_column(String(255))

    sender: Mapped[str] = mapped_column(String(320), default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[Optional[str]] = mapped_column(Text)
    body_text: Mapped[Optional[str]] = mapped_column(Text)

    received_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    processed_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    parse_error: Mapped[Optional[str]] = mapped_column(Text)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="email")


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_tx_status_occurred", "status", "occurred_at"),
        Index("ix_tx_merchant", "merchant"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Short human-typable handle shown in Discord, e.g. "1042". Unambiguous
    # alphabet (no 0/O/1/I/l) — see services/shortcode.py.
    short_code: Mapped[str] = mapped_column(String(16), unique=True)

    email_id: Mapped[Optional[int]] = mapped_column(ForeignKey("emails_raw.id"))
    email: Mapped[Optional[RawEmail]] = relationship(back_populates="transactions")

    occurred_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    merchant: Mapped[str] = mapped_column(String(255), default="")
    amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    card_ending: Mapped[Optional[str]] = mapped_column(String(8))
    cardholder: Mapped[Optional[str]] = mapped_column(String(128))

    status: Mapped[TransactionStatus] = mapped_column(
        EnumString(TransactionStatus), default=TransactionStatus.new
    )
    category: Mapped[Optional[str]] = mapped_column(String(64))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # Reconciliation: the alert is the *authorization*. The posted amount can
    # differ (tips, FX). Filled in later from a statement import.
    amount_final_minor: Mapped[Optional[int]] = mapped_column(Integer)

    # A refund points back at the charge it reverses. Self-referential so a
    # credit and its original stay linked in the export.
    refund_of_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )
    refund_of: Mapped[Optional["Transaction"]] = relationship(
        remote_side="Transaction.id", foreign_keys=[refund_of_id]
    )

    notified_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    lapsed_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    matched_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    # The Discord message announcing this charge; replying to it is the strongest
    # matching signal we have.
    notify_message_id: Mapped[Optional[str]] = mapped_column(String(32))

    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="transaction",
        order_by="Attachment.received_at",
        foreign_keys="Attachment.transaction_id",
    )

    @property
    def has_receipt(self) -> bool:
        return bool(self.attachments)

    @property
    def is_refund(self) -> bool:
        return self.amount_minor < 0


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        Index("ix_att_tx", "transaction_id"),
        # Same photo sent twice attaches once.
        UniqueConstraint("transaction_id", "sha256", name="uq_att_tx_sha"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL == orphan queue: captured but unmatched. Never block capture on matching.
    transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )
    transaction: Mapped[Optional[Transaction]] = relationship(back_populates="attachments")

    path: Mapped[str] = mapped_column(String(512))          # relative to receipts_dir
    thumb_path: Mapped[Optional[str]] = mapped_column(String(512))
    original_filename: Mapped[Optional[str]] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)

    ocr_text: Mapped[Optional[str]] = mapped_column(Text)

    source_message_id: Mapped[Optional[str]] = mapped_column(String(32))
    uploader_id: Mapped[Optional[str]] = mapped_column(String(32))
    uploader_name: Mapped[Optional[str]] = mapped_column(String(128))
    received_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    # True once the source Discord message has been deleted. Only ever set after
    # the bytes are durably on disk and verified — see services/storage.py.
    source_deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class DiscordMessage(Base):
    """Audit of every message we send or act on.

    Needed for matching (resolving a reply's referenced message back to a
    transaction) and for debugging why something did or did not match.
    """

    __tablename__ = "discord_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(32), unique=True)
    channel_id: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(8))  # "out" | "in"
    kind: Mapped[str] = mapped_column(String(32), default="")  # notify|confirm|digest|receipt
    transaction_id: Mapped[Optional[int]] = mapped_column(ForeignKey("transactions.id"))
    author_id: Mapped[Optional[str]] = mapped_column(String(32))
    content: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)


class Job(Base):
    """Durable outbox. Intent is persisted before any side effect is attempted,
    so a crash mid-send loses nothing and a retry is always safe.
    """

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "next_run_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Second insert with the same key is a no-op — makes enqueue itself idempotent.
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255), unique=True)

    status: Mapped[JobStatus] = mapped_column(
        EnumString(JobStatus, 16), default=JobStatus.pending
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8)
    next_run_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[Optional[str]] = mapped_column(String(64))
    entity_id: Mapped[Optional[str]] = mapped_column(String(64))
    detail: Mapped[Optional[str]] = mapped_column(Text)
