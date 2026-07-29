"""Short, human-typable transaction handles.

These get typed on a phone keyboard into a Discord message, so they are plain
sequential integers rather than random tokens — ``#1042`` is far easier to read
off a notification and retype than ``#K7QF``. Volume leakage is a non-issue for a
single-tenant internal tool.

Allocation retries on collision, so two concurrent inserts (poller and a manual
UI create, say) cannot deadlock or produce a duplicate.
"""

from __future__ import annotations

import re

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Transaction

START_AT = 1000
_CODE_RE = re.compile(r"#\s*(\d{3,9})\b")


def next_short_code(db: Session) -> str:
    """Return the next unused short code."""
    highest = db.scalar(select(func.max(cast(Transaction.short_code, Integer))))
    nxt = max(int(highest or 0) + 1, START_AT)
    while db.scalar(select(Transaction.id).where(Transaction.short_code == str(nxt))):
        nxt += 1
    return str(nxt)


def allocate(db: Session, make_transaction, *, attempts: int = 5) -> Transaction:
    """Create a transaction with a fresh short code, retrying on collision.

    ``make_transaction`` is a callable taking the allocated code and returning an
    unsaved :class:`Transaction`.

    Each attempt runs inside a SAVEPOINT. A plain ``db.rollback()`` here would
    discard the caller's entire unit of work — in the ingest path that is the
    ``RawEmail`` flushed moments earlier, so a single code collision would
    silently drop a charge.
    """
    last_error: Exception | None = None
    for _ in range(attempts):
        code = next_short_code(db)
        tx = make_transaction(code)
        try:
            with db.begin_nested():
                db.add(tx)
                db.flush()
            return tx
        except IntegrityError as exc:  # another writer took this code
            # The savepoint rollback normally evicts the instance already; only
            # expunge if it somehow survived, so it cannot be re-flushed with the
            # surrounding transaction.
            if tx in db:
                db.expunge(tx)
            last_error = exc
    raise RuntimeError(f"Could not allocate a unique short code: {last_error}")


def extract_codes(text: str) -> list[str]:
    """Pull every ``#1042``-style code out of a Discord message.

    An explicit code is an override: it matches even a lapsed transaction, which
    is what lets you attach a receipt you found in your wallet days later.
    """
    if not text:
        return []
    return [m.group(1) for m in _CODE_RE.finditer(text)]
