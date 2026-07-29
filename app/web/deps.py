"""Shared web plumbing: templates, auth guard, CSRF, base context."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app import settings_keys as sk
from app.config import get_config
from app.db import session_scope
from app.formatting import money, when, when_full
from app.models import Attachment, Session, Transaction, TransactionStatus, User
from app.security import SESSION_COOKIE, check_csrf, is_setup_complete, load_session

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Fail loudly on a missing or misspelled context variable.
#
# Jinja2's default Undefined renders as an empty string, which turned a real bug
# (`tx.status` decaying to a plain str, so `.status.value` raised AttributeError)
# into every status badge silently rendering blank with class "badge-" and no
# error anywhere. A 500 in development beats a page that looks subtly wrong in
# production.
templates.env.undefined = StrictUndefined
templates.env.globals["money"] = money
templates.env.filters["money"] = money
templates.env.filters["when"] = when
templates.env.filters["when_full"] = when_full


class LoginRequired(Exception):
    """Raised to bounce an unauthenticated request to /login."""


def get_db():  # noqa: ANN201 - FastAPI dependency
    with session_scope() as db:
        yield db


def current_session(request: Request, db: OrmSession = Depends(get_db)) -> Session | None:
    return load_session(db, request.cookies.get(SESSION_COOKIE))


def require_user(
    request: Request,
    db: OrmSession = Depends(get_db),
) -> tuple[User, Session]:
    """Guard for every authenticated route.

    Redirects rather than 401s so the browser lands somewhere useful, preserving
    the original path so login can bounce back to it.
    """
    session = load_session(db, request.cookies.get(SESSION_COOKIE))
    if session is None:
        target = "/setup" if not is_setup_complete(db) else "/login"
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": target},
        )
    user = db.get(User, session.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/login"}
        )
    if session.totp_pending:
        # Password accepted but the second factor is outstanding. Everything —
        # including the receipt-file route — stays closed until it is supplied.
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login/totp"},
        )
    return user, session


def verify_csrf(session: Session, submitted: str | None) -> None:
    if not check_csrf(session, submitted):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")


def parse_flash(request: Request) -> list[tuple[str, str]]:
    """Read flash messages out of the query string.

    Deliberately stateless — no server-side flash store to expire or leak.
    """
    out: list[tuple[str, str]] = []
    for category in ("success", "error", "warning", "info"):
        for message in request.query_params.getlist(category):
            if message:
                out.append((category, message[:300]))
    return out


def redirect_with(path: str, *, success: str | None = None, error: str | None = None,
                  warning: str | None = None) -> RedirectResponse:
    from urllib.parse import quote

    params = []
    if success:
        params.append(f"success={quote(success)}")
    if error:
        params.append(f"error={quote(error)}")
    if warning:
        params.append(f"warning={quote(warning)}")
    joiner = "&" if "?" in path else "?"
    url = f"{path}{joiner}{'&'.join(params)}" if params else path
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _open_status_values() -> list[str]:
    return [s.value for s in TransactionStatus.open_statuses()] + [
        TransactionStatus.needs_attention.value
    ]


def health_snapshot(db: OrmSession) -> dict[str, Any]:
    """Everything the health page and the nav dot need."""
    from app.discordbot import get_service
    from app.security import get_setting, secret_is_unreadable
    from app.services import jobs
    from app.services.ingest import LAST_ERROR_KEY, LAST_MAIL_KEY, LAST_POLL_KEY

    def _parse(value: str | None) -> dt.datetime | None:
        if not value:
            return None
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)

    service = get_service()
    last_poll = _parse(get_setting(db, LAST_POLL_KEY))
    last_mail = _parse(get_setting(db, LAST_MAIL_KEY))
    poll_error = get_setting(db, LAST_ERROR_KEY) or None
    stats = jobs.queue_stats(db)
    dead_jobs = jobs.dead_jobs(db)

    # A restore without the matching secret.key leaves credentials unreadable.
    unreadable = [
        k.label for k in (sk.GRAPH_CLIENT_SECRET, sk.DISCORD_BOT_TOKEN)
        if secret_is_unreadable(db, k.name)
    ]

    graph_configured = sk.is_configured_for_graph(db)
    discord_configured = sk.is_configured_for_discord(db)
    heartbeat_hours = sk.get_int(db, sk.HEARTBEAT_HOURS)

    now = dt.datetime.now(dt.UTC)
    poll_stale = bool(
        graph_configured
        and (last_poll is None or (now - last_poll) > dt.timedelta(minutes=5))
    )
    mail_stale = bool(
        graph_configured
        and last_mail is not None
        and (now - last_mail) > dt.timedelta(hours=heartbeat_hours)
    )

    ok = (
        not unreadable
        and not poll_error
        and not poll_stale
        and not mail_stale
        and stats.get("dead", 0) == 0
        and (not discord_configured or service.is_connected)
    )

    return {
        "ok": ok,
        "unreadable_secrets": unreadable,
        "graph_configured": graph_configured,
        "discord_configured": discord_configured,
        "discord_connected": service.is_connected,
        "discord_since": service.connected_since,
        "discord_error": service.last_error,
        "last_poll": last_poll,
        "last_mail": last_mail,
        "poll_error": poll_error,
        "poll_stale": poll_stale,
        "mail_stale": mail_stale,
        "heartbeat_hours": heartbeat_hours,
        "jobs": stats,
        "dead_jobs": dead_jobs,
    }


def base_context(
    request: Request,
    db: OrmSession,
    user: User | None,
    session: Session | None,
    nav_active: str = "",
) -> dict[str, Any]:
    outstanding = 0
    orphans = 0
    health_ok = True
    if user is not None:
        outstanding = int(
            db.scalar(
                select(func.count(Transaction.id)).where(
                    Transaction.status.in_(_open_status_values())
                )
            )
            or 0
        )
        orphans = int(
            db.scalar(
                select(func.count(Attachment.id)).where(Attachment.transaction_id.is_(None))
            )
            or 0
        )
        health_ok = bool(health_snapshot(db)["ok"])

    return {
        "request": request,
        "user": user,
        "nav_active": nav_active,
        "flash": parse_flash(request),
        "outstanding_count": outstanding,
        "orphan_count": orphans,
        "health_ok": health_ok,
        "csrf_token": session.csrf_token if session else "",
        "tz": sk.get_str(db, sk.TIMEZONE),
        "config": get_config(),
    }
