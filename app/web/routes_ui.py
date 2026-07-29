"""Dashboard, transactions, orphan queue, receipt serving, health."""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as OrmSession, selectinload

from app import settings_keys as sk
from app.formatting import money
from app.models import (
    Attachment,
    AuditLog,
    RawEmail,
    Transaction,
    TransactionStatus,
    utcnow,
)
from app.services import storage
from app.web.deps import (
    base_context,
    get_db,
    health_snapshot,
    redirect_with,
    require_user,
    templates,
    verify_csrf,
)

logger = logging.getLogger(__name__)
router = APIRouter()

PAGE_SIZE = 50


def _open_values() -> list[str]:
    return [s.value for s in TransactionStatus.open_statuses()] + [
        TransactionStatus.needs_attention.value
    ]


@router.get("/")
async def dashboard(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    tz = sk.get_str(db, sk.TIMEZONE)
    now = utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    mtd_total = int(
        db.scalar(
            select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(
                Transaction.occurred_at >= month_start,
                Transaction.status != TransactionStatus.ignored,
            )
        )
        or 0
    )
    outstanding = int(
        db.scalar(
            select(func.count(Transaction.id)).where(Transaction.status.in_(_open_values()))
        )
        or 0
    )
    lapsed = int(
        db.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.status == TransactionStatus.lapsed
            )
        )
        or 0
    )
    orphans = int(
        db.scalar(select(func.count(Attachment.id)).where(Attachment.transaction_id.is_(None)))
        or 0
    )

    recent = list(
        db.scalars(
            select(Transaction)
            .options(selectinload(Transaction.attachments))
            .order_by(Transaction.occurred_at.desc())
            .limit(12)
        )
    )
    awaiting = list(
        db.scalars(
            select(Transaction)
            .where(Transaction.status.in_(_open_values()))
            .order_by(Transaction.occurred_at)
            .limit(12)
        )
    )

    ctx = base_context(request, db, user, session, "dashboard")
    ctx.update({
        "mtd_total": mtd_total,
        "mtd_display": money(mtd_total, sk.get_str(db, sk.DEFAULT_CURRENCY)),
        "outstanding": outstanding,
        "lapsed": lapsed,
        "orphans": orphans,
        "recent": recent,
        "awaiting": awaiting,
        "health": health_snapshot(db),
        "tz": tz,
    })
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/transactions")
async def transactions(
    request: Request,
    q: str = Query(""),
    status_filter: str = Query("", alias="status"),
    start: str = Query(""),
    end: str = Query(""),
    page: int = Query(1, ge=1),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    stmt = select(Transaction).options(selectinload(Transaction.attachments))

    if q:
        like = f"%{q.strip()}%"
        conditions = [
            Transaction.merchant.ilike(like),
            Transaction.short_code.ilike(like),
            Transaction.notes.ilike(like),
            Transaction.category.ilike(like),
            Transaction.card_ending.ilike(like),
        ]
        # Amount search: "43.21" or "4321" both find the same charge.
        digits = q.replace(",", "").replace("$", "").strip()
        try:
            conditions.append(Transaction.amount_minor == int(round(float(digits) * 100)))
        except ValueError:
            pass
        stmt = stmt.where(or_(*conditions))

    if status_filter:
        stmt = stmt.where(Transaction.status == status_filter)

    def _parse_date(value: str) -> dt.date | None:
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return None

    if (start_date := _parse_date(start)) is not None:
        stmt = stmt.where(
            Transaction.occurred_at >= dt.datetime.combine(start_date, dt.time.min, dt.UTC)
        )
    if (end_date := _parse_date(end)) is not None:
        stmt = stmt.where(
            Transaction.occurred_at <= dt.datetime.combine(end_date, dt.time.max, dt.UTC)
        )

    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(
        db.scalars(
            stmt.order_by(Transaction.occurred_at.desc(), Transaction.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    )

    ctx = base_context(request, db, user, session, "transactions")
    ctx.update({
        "rows": rows,
        "total": total,
        "page": page,
        "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        "q": q,
        "status_filter": status_filter,
        "start": start,
        "end": end,
        "statuses": [s.value for s in TransactionStatus],
    })
    return templates.TemplateResponse(request, "transactions.html", ctx)


@router.get("/transactions/{code}")
async def transaction_detail(
    request: Request,
    code: str,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    tx = db.scalar(
        select(Transaction)
        .options(selectinload(Transaction.attachments))
        .where(Transaction.short_code == code)
    )
    if tx is None:
        raise HTTPException(status_code=404, detail="No such transaction")

    email = db.get(RawEmail, tx.email_id) if tx.email_id else None
    orphans = list(
        db.scalars(
            select(Attachment)
            .where(Attachment.transaction_id.is_(None))
            .order_by(Attachment.received_at.desc())
            .limit(50)
        )
    )

    # Credits that point back at this charge (the inverse of tx.refund_of).
    refunds = list(
        db.scalars(select(Transaction).where(Transaction.refund_of_id == tx.id))
    )

    ctx = base_context(request, db, user, session, "transactions")
    ctx.update({
        "tx": tx,
        "email": email,
        "orphans": orphans,
        "refunds": refunds,
        "statuses": [s.value for s in TransactionStatus],
    })
    return templates.TemplateResponse(request, "transaction_detail.html", ctx)


@router.post("/transactions/{code}/update")
async def update_transaction(
    code: str,
    csrf_token: str = Form(""),
    merchant: str = Form(""),
    amount: str = Form(""),
    category: str = Form(""),
    notes: str = Form(""),
    status_value: str = Form("", alias="status"),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)

    tx = db.scalar(select(Transaction).where(Transaction.short_code == code))
    if tx is None:
        raise HTTPException(status_code=404, detail="No such transaction")

    changes: list[str] = []
    if merchant and merchant != tx.merchant:
        changes.append(f"merchant {tx.merchant!r}->{merchant!r}")
        tx.merchant = merchant
    if amount:
        from app.services.parsing import parse_amount

        try:
            minor, _currency = parse_amount(amount, tx.currency)
            if minor != tx.amount_minor:
                changes.append(f"amount {tx.amount_minor}->{minor}")
                tx.amount_minor = minor
        except ValueError:
            return redirect_with(f"/transactions/{code}", error=f"Could not parse {amount!r}.")
    if category != (tx.category or ""):
        tx.category = category or None
        changes.append("category")
    if notes != (tx.notes or ""):
        tx.notes = notes or None
        changes.append("notes")
    if status_value and status_value != tx.status.value:
        try:
            tx.status = TransactionStatus(status_value)
            changes.append(f"status->{status_value}")
        except ValueError:
            return redirect_with(f"/transactions/{code}", error="Unknown status.")

    if changes:
        db.add(tx)
        db.add(AuditLog(actor=user.username, action="transaction.updated", entity="transaction",
                        entity_id=code, detail="; ".join(changes)))
    return redirect_with(f"/transactions/{code}", success="Saved." if changes else "No changes.")


@router.post("/transactions/{code}/attach")
async def attach_orphan(
    code: str,
    attachment_id: int = Form(...),
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    """Manual attach — the escape hatch that always works, including for lapsed."""
    user, session = auth
    verify_csrf(session, csrf_token)

    tx = db.scalar(select(Transaction).where(Transaction.short_code == code))
    att = db.get(Attachment, attachment_id)
    if tx is None or att is None:
        raise HTTPException(status_code=404, detail="Not found")

    att.transaction_id = tx.id
    if tx.status in (
        TransactionStatus.new, TransactionStatus.notified,
        TransactionStatus.lapsed, TransactionStatus.needs_attention,
    ):
        tx.status = TransactionStatus.receipt_attached
        tx.matched_at = utcnow()
    db.add_all([att, tx])
    db.add(AuditLog(actor=user.username, action="receipt.attached", entity="transaction",
                    entity_id=code, detail=f"attachment {attachment_id} (manual)"))
    return redirect_with(f"/transactions/{code}", success="Receipt attached.")


@router.post("/attachments/{attachment_id}/detach")
async def detach(
    attachment_id: int,
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)

    att = db.get(Attachment, attachment_id)
    if att is None:
        raise HTTPException(status_code=404, detail="Not found")
    tx = db.get(Transaction, att.transaction_id) if att.transaction_id else None
    att.transaction_id = None
    db.add(att)
    if tx is not None:
        remaining = int(
            db.scalar(
                select(func.count(Attachment.id)).where(
                    Attachment.transaction_id == tx.id, Attachment.id != attachment_id
                )
            )
            or 0
        )
        if remaining == 0 and tx.status == TransactionStatus.receipt_attached:
            tx.status = TransactionStatus.notified
            tx.matched_at = None
            db.add(tx)
    db.add(AuditLog(actor=user.username, action="receipt.detached", entity="attachment",
                    entity_id=str(attachment_id)))
    target = f"/transactions/{tx.short_code}" if tx else "/orphans"
    return redirect_with(target, success="Receipt moved to the orphan queue.")


@router.get("/orphans")
async def orphans(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    rows = list(
        db.scalars(
            select(Attachment)
            .where(Attachment.transaction_id.is_(None))
            .order_by(Attachment.received_at.desc())
        )
    )
    candidates = list(
        db.scalars(
            select(Transaction).order_by(Transaction.occurred_at.desc()).limit(200)
        )
    )
    ctx = base_context(request, db, user, session, "orphans")
    ctx.update({"rows": rows, "candidates": candidates})
    return templates.TemplateResponse(request, "orphans.html", ctx)


@router.post("/orphans/assign")
async def assign_orphan(
    attachment_id: int = Form(...),
    short_code: str = Form(...),
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)

    att = db.get(Attachment, attachment_id)
    tx = db.scalar(select(Transaction).where(Transaction.short_code == short_code))
    if att is None or tx is None:
        raise HTTPException(status_code=404, detail="Not found")
    if att.transaction_id is not None:
        return redirect_with("/orphans", warning="That receipt was already assigned.")

    att.transaction_id = tx.id
    if tx.status in (
        TransactionStatus.new, TransactionStatus.notified,
        TransactionStatus.lapsed, TransactionStatus.needs_attention,
    ):
        tx.status = TransactionStatus.receipt_attached
        tx.matched_at = utcnow()
    db.add_all([att, tx])
    db.add(AuditLog(actor=user.username, action="receipt.attached", entity="transaction",
                    entity_id=short_code, detail=f"attachment {attachment_id} (orphan queue)"))
    return redirect_with("/orphans", success=f"Assigned to #{short_code}.")


# --------------------------------------------------------------------------- #
# Receipt files — authenticated, never served as static
# --------------------------------------------------------------------------- #

@router.get("/receipts/{attachment_id}")
async def receipt_file(
    attachment_id: int,
    download: bool = Query(False),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    att = db.get(Attachment, attachment_id)
    if att is None:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        path = storage.absolute_path(att.path)
    except storage.CaptureError:
        raise HTTPException(status_code=404, detail="Not found") from None
    if not path.is_file():
        raise HTTPException(status_code=410, detail="The stored file is missing on disk")

    filename = att.original_filename or path.name
    return FileResponse(
        path,
        media_type=att.mime,
        filename=filename if download else None,
        headers={
            # These are financial records; never let a proxy or the browser cache them.
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            # Defence in depth against a crafted SVG/HTML upload running script
            # in the app's origin.
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
        },
    )


@router.get("/receipts/{attachment_id}/thumb")
async def receipt_thumb(
    attachment_id: int,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    att = db.get(Attachment, attachment_id)
    if att is None or not att.thumb_path:
        raise HTTPException(status_code=404, detail="No thumbnail")
    try:
        path = storage.thumb_absolute_path(att.thumb_path)
    except storage.CaptureError:
        raise HTTPException(status_code=404, detail="Not found") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No thumbnail")
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"}
    )


@router.get("/health")
async def health(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    ctx = base_context(request, db, user, session, "health")
    ctx["health"] = health_snapshot(db)
    ctx["recent_errors"] = list(
        db.scalars(
            select(AuditLog).order_by(AuditLog.at.desc()).limit(25)
        )
    )
    return templates.TemplateResponse(request, "health.html", ctx)
