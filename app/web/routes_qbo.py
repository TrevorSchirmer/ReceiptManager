"""QuickBooks connect, callback and disconnect.

The callback is behind the normal login guard. Intuit never calls it — the
browser does — so requiring a session here costs nothing and means a stray
request to the URL cannot start or finish an authorisation.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as OrmSession

from app import settings_keys as sk
from app.models import AuditLog, utcnow
from app.security import get_setting, set_setting
from app.services import jobs, qbo
from app.web.deps import get_db, redirect_with, require_user, verify_csrf

logger = logging.getLogger(__name__)
router = APIRouter()

# Single-use anti-forgery value for the OAuth round trip, held in settings rather
# than a new column because it lives for about a minute.
STATE_KEY = "runtime.qbo_oauth_state"
STATE_AT_KEY = "runtime.qbo_oauth_state_at"
STATE_TTL = dt.timedelta(minutes=15)


@router.get("/qbo/connect")
async def connect(
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    """Start the authorisation, sending the browser to Intuit."""
    _user, _session = auth
    if not sk.is_configured_for_qbo(db):
        return redirect_with(
            "/settings",
            error="Enter the QuickBooks client ID, secret and redirect URI first.",
        )

    state = qbo.new_state()
    set_setting(db, STATE_KEY, state)
    set_setting(db, STATE_AT_KEY, utcnow().isoformat())
    try:
        url = qbo.build_authorize_url(db, state)
    except qbo.QboError as exc:
        return redirect_with("/settings", error=str(exc))
    return RedirectResponse(url, status_code=303)


@router.get("/qbo/callback")
async def callback(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    """Where Intuit sends the browser back, carrying code / state / realmId."""
    user, _session = auth
    params = request.query_params

    if error := params.get("error"):
        return redirect_with(
            "/settings",
            error=f"QuickBooks authorisation was declined: {error}",
        )

    code = params.get("code") or ""
    state = params.get("state") or ""
    realm_id = params.get("realmId") or ""

    expected = get_setting(db, STATE_KEY) or ""
    issued_at = get_setting(db, STATE_AT_KEY) or ""
    # Single use, whatever happens next.
    set_setting(db, STATE_KEY, "")
    set_setting(db, STATE_AT_KEY, "")

    if not expected or state != expected:
        return redirect_with("/settings", error="QuickBooks state mismatch — start again.")
    try:
        started = dt.datetime.fromisoformat(issued_at)
    except ValueError:
        started = None
    if started is None or utcnow() - started > STATE_TTL:
        return redirect_with("/settings", error="That authorisation expired — start again.")
    if not code or not realm_id:
        return redirect_with("/settings", error="QuickBooks returned no code or company.")

    try:
        await qbo.exchange_code(code, realm_id)
    except qbo.QboError as exc:
        logger.exception("QuickBooks code exchange failed")
        return redirect_with("/settings", error=f"Could not complete the connection: {exc}")

    db.add(AuditLog(actor=user.username, action="qbo.connected", detail=f"realm {realm_id}"))
    # Pull the chart of accounts straight away — the connection is not useful
    # until categories exist to choose from.
    jobs.enqueue(
        db, kind="qbo.sync_accounts", payload={},
        idempotency_key=f"qbo-accounts:{utcnow().strftime('%Y%m%d%H%M%S')}",
    )
    return redirect_with(
        "/settings", success="QuickBooks connected. Fetching the chart of accounts…"
    )


@router.post("/qbo/disconnect")
async def disconnect(
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)

    token = sk.get_str(db, sk.QBO_REFRESH_TOKEN)
    qbo.clear_tokens(db)
    db.add(AuditLog(actor=user.username, action="qbo.disconnected"))
    db.commit()

    if token:
        await qbo.revoke(token)
    return redirect_with("/settings", warning="QuickBooks disconnected.")


@router.post("/qbo/sync-accounts")
async def sync_accounts_now(
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    _user, session = auth
    verify_csrf(session, csrf_token)
    if not sk.is_connected_to_qbo(db):
        return redirect_with("/settings", error="Connect QuickBooks first.")
    jobs.enqueue(
        db, kind="qbo.sync_accounts", payload={},
        idempotency_key=f"qbo-accounts:{utcnow().strftime('%Y%m%d%H%M%S')}",
    )
    return redirect_with("/settings", success="Refreshing the chart of accounts…")
