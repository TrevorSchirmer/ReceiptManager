"""Keeping the cached QuickBooks chart of accounts up to date.

The category picker reads from this cache rather than calling QuickBooks on every
page render. A chart of accounts changes about as often as a company reorganises
its bookkeeping, so a daily refresh plus a manual button is ample.

Accounts are **deactivated, never deleted**. An account removed from QuickBooks
may still be referenced by transactions this app categorised months ago, and
losing that reference would quietly rewrite history.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import settings_keys as sk
from app.db import run_db, session_scope
from app.models import AuditLog, QboAccount, utcnow
from app.services import jobs

logger = logging.getLogger(__name__)


def upsert_accounts(db: Session, rows: list[dict]) -> tuple[int, int, int]:
    """Merge fetched accounts into the cache. Returns (added, updated, deactivated)."""
    seen: set[str] = set()
    added = updated = 0

    for row in rows:
        qbo_id = str(row.get("Id") or "").strip()
        if not qbo_id:
            continue
        seen.add(qbo_id)

        account = db.scalar(select(QboAccount).where(QboAccount.qbo_id == qbo_id))
        name = str(row.get("Name") or "")
        fully_qualified = str(row.get("FullyQualifiedName") or name)
        account_type = str(row.get("AccountType") or "")
        sub_type = str(row.get("AccountSubType") or "") or None
        active = bool(row.get("Active", True))

        if account is None:
            db.add(
                QboAccount(
                    qbo_id=qbo_id, name=name, fully_qualified_name=fully_qualified,
                    account_type=account_type, account_sub_type=sub_type, active=active,
                )
            )
            added += 1
            continue

        changed = (
            account.name != name
            or account.fully_qualified_name != fully_qualified
            or account.account_type != account_type
            or account.account_sub_type != sub_type
            or account.active != active
        )
        account.name = name
        account.fully_qualified_name = fully_qualified
        account.account_type = account_type
        account.account_sub_type = sub_type
        account.active = active
        account.synced_at = utcnow()
        db.add(account)
        if changed:
            updated += 1

    # Anything QuickBooks no longer returns is marked inactive rather than
    # removed, so a transaction categorised against it keeps its reference.
    deactivated = 0
    for account in db.scalars(select(QboAccount).where(QboAccount.active.is_(True))):
        if account.qbo_id not in seen:
            account.active = False
            db.add(account)
            deactivated += 1

    return added, updated, deactivated


def active_accounts(db: Session) -> list[QboAccount]:
    """Accounts offered in the category picker, alphabetically by full path."""
    return list(
        db.scalars(
            select(QboAccount)
            .where(QboAccount.active.is_(True))
            .order_by(QboAccount.fully_qualified_name, QboAccount.name)
        )
    )


@jobs.handler("qbo.sync_accounts")
async def sync_accounts(_payload: dict) -> None:
    """Refresh the cached chart of accounts."""
    from app.services.qbo import QboClient, QboNotConnected

    try:
        async with QboClient() as client:
            rows = await client.expense_accounts()
            company = await client.company_name()
    except QboNotConnected:
        logger.info("QuickBooks not connected; skipping account sync")
        return

    def write() -> tuple[int, int, int]:
        with session_scope() as db:
            result = upsert_accounts(db, rows)
            sk.put(db, sk.QBO_ACCOUNTS_SYNCED_AT, utcnow().isoformat())
            if company:
                sk.put(db, sk.QBO_COMPANY_NAME, company)
            added, changed, deactivated = result
            if added or changed or deactivated:
                db.add(
                    AuditLog(
                        actor="quickbooks", action="accounts.synced",
                        detail=f"{added} added, {changed} updated, {deactivated} deactivated",
                    )
                )
            return result

    added, changed, deactivated = await run_db(write)
    logger.info(
        "QuickBooks accounts synced: %d added, %d updated, %d deactivated (of %d)",
        added, changed, deactivated, len(rows),
    )
