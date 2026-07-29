"""Export: CSV + ZIP of receipts for the accountant."""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session as OrmSession

from app import settings_keys as sk
from app.config import get_config
from app.db import run_db
from app.models import AuditLog, TransactionStatus
from app.services import export
from app.web.deps import base_context, get_db, redirect_with, require_user, templates, verify_csrf

logger = logging.getLogger(__name__)
router = APIRouter()


def _parse_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


@router.get("/export")
async def export_page(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    today = dt.date.today()
    ctx = base_context(request, db, user, session, "export")
    ctx.update({
        "statuses": [s.value for s in TransactionStatus],
        "default_start": today.replace(day=1).isoformat(),
        "default_end": today.isoformat(),
    })
    return templates.TemplateResponse(request, "export.html", ctx)


@router.post("/export/csv")
async def export_csv(
    csrf_token: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    _user, session = auth
    verify_csrf(session, csrf_token)

    tz = sk.get_str(db, sk.TIMEZONE)
    rows = export.iter_transactions(
        db, start=_parse_date(start), end=_parse_date(end), tz_name=tz
    )
    body = export.build_csv(rows, tz_name=tz)
    stamp = dt.date.today().isoformat()
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="transactions-{stamp}.csv"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/export/zip")
async def export_zip(
    csrf_token: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)

    cfg = get_config()
    tz = sk.get_str(db, sk.TIMEZONE)
    start_date, end_date = _parse_date(start), _parse_date(end)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    dest = cfg.tmp_dir / f"receipts-export-{stamp}.zip"

    # Zipping hundreds of receipts is genuinely slow; keep it off the event loop
    # so the Discord gateway heartbeat is not delayed.
    try:
        stats = await run_db(
            export.build_zip, db, dest, start=start_date, end=end_date, tz_name=tz
        )
    except Exception as exc:
        logger.exception("Export failed")
        return redirect_with("/export", error=f"Export failed: {exc}")

    db.add(
        AuditLog(
            actor=user.username,
            action="export.created",
            detail=(
                f"{stats.transactions} transactions, {stats.receipts} receipts, "
                f"{stats.missing_files} missing"
            ),
        )
    )
    db.commit()

    if stats.missing_files:
        logger.warning("Export completed with %d missing file(s)", stats.missing_files)

    return FileResponse(
        dest,
        media_type="application/zip",
        filename=f"receipts-{start or 'all'}-to-{end or 'all'}.zip",
        # background cleanup would race the download; the tmp dir is swept by the
        # install script's cron instead.
        headers={"Cache-Control": "private, no-store"},
    )
