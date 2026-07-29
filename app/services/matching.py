"""Resolving an uploaded receipt to the charge it belongs to.

Priority order, strongest signal first:

1. **Reply to the bot's notification** — unambiguous, no prompt.
2. **Explicit ``#1042`` in the message text** — unambiguous, no prompt. This is an
   *override*: it matches regardless of status, including ``lapsed``, which is
   what lets you attach a receipt you found in your wallet three days later.
3. **Exactly one open charge** — auto-attach and confirm, no prompt.
4. **Two or more open charges** — ask, via a Discord select menu.

Only steps 3 and 4 are "implicit", and only those are restricted to non-lapsed
charges. Lapsing exists to keep the picker short and the nudges quiet; it must
never make a charge unreachable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DiscordMessage, Transaction, TransactionStatus
from app.services import shortcode

# Discord hard-caps a string select at 25 options.
MAX_PICKER_OPTIONS = 25


class MatchMethod(str, enum.Enum):
    reply = "reply"
    code = "code"
    sole_open = "sole_open"
    ambiguous = "ambiguous"
    none_open = "none_open"


@dataclass(slots=True)
class MatchResult:
    method: MatchMethod
    transaction: Transaction | None = None
    candidates: list[Transaction] = field(default_factory=list)
    truncated: bool = False

    @property
    def is_resolved(self) -> bool:
        return self.transaction is not None

    @property
    def needs_prompt(self) -> bool:
        return self.method in (MatchMethod.ambiguous, MatchMethod.none_open)


def find_by_reply(db: Session, referenced_message_id: str | None) -> Transaction | None:
    """Resolve a Discord reply back to the charge its parent message announced."""
    if not referenced_message_id:
        return None
    record = db.scalar(
        select(DiscordMessage).where(DiscordMessage.message_id == str(referenced_message_id))
    )
    if record is None or record.transaction_id is None:
        return None
    return db.get(Transaction, record.transaction_id)


def find_by_code(db: Session, text: str | None) -> Transaction | None:
    """Resolve an explicit ``#1042`` from message text.

    Deliberately status-agnostic — an explicit code overrides lapsing.
    """
    for code in shortcode.extract_codes(text or ""):
        tx = db.scalar(select(Transaction).where(Transaction.short_code == code))
        if tx is not None:
            return tx
    return None


def open_transactions(db: Session, *, limit: int = MAX_PICKER_OPTIONS) -> list[Transaction]:
    """Charges still eligible for implicit matching, newest first."""
    return list(
        db.scalars(
            select(Transaction)
            .where(
                Transaction.status.in_(
                    [s.value for s in TransactionStatus.open_statuses()]
                    + [TransactionStatus.needs_attention.value]
                )
            )
            .order_by(Transaction.occurred_at.desc())
            .limit(limit)
        )
    )


def recent_transactions(db: Session, *, days: int = 7, limit: int = MAX_PICKER_OPTIONS
                        ) -> list[Transaction]:
    """Fallback picker when nothing is open — a receipt may be a second page."""
    import datetime as dt

    from app.models import utcnow

    cutoff = utcnow() - dt.timedelta(days=days)
    return list(
        db.scalars(
            select(Transaction)
            .where(Transaction.occurred_at >= cutoff)
            .order_by(Transaction.occurred_at.desc())
            .limit(limit)
        )
    )


def count_open(db: Session) -> int:
    from sqlalchemy import func

    return int(
        db.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.status.in_(
                    [s.value for s in TransactionStatus.open_statuses()]
                    + [TransactionStatus.needs_attention.value]
                )
            )
        )
        or 0
    )


def resolve(db: Session, *, referenced_message_id: str | None, text: str | None) -> MatchResult:
    """Decide what to do with an incoming receipt upload."""
    if (tx := find_by_reply(db, referenced_message_id)) is not None:
        return MatchResult(MatchMethod.reply, transaction=tx)

    if (tx := find_by_code(db, text)) is not None:
        return MatchResult(MatchMethod.code, transaction=tx)

    candidates = open_transactions(db, limit=MAX_PICKER_OPTIONS + 1)
    truncated = len(candidates) > MAX_PICKER_OPTIONS
    candidates = candidates[:MAX_PICKER_OPTIONS]

    if len(candidates) == 1 and not truncated:
        return MatchResult(MatchMethod.sole_open, transaction=candidates[0])
    if candidates:
        return MatchResult(MatchMethod.ambiguous, candidates=candidates, truncated=truncated)

    return MatchResult(
        MatchMethod.none_open,
        candidates=recent_transactions(db),
    )
