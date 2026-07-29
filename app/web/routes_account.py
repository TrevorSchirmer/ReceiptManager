"""Account: password change, TOTP enrollment, and user management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.models import AuditLog, Session, User
from app.security import (
    hash_password,
    new_totp_secret,
    totp_qr_svg,
    totp_uri,
    validate_password_strength,
    verify_password,
    verify_totp,
)
from app.web.deps import base_context, get_db, redirect_with, require_user, templates, verify_csrf

logger = logging.getLogger(__name__)
router = APIRouter()

# Where a half-finished enrollment lives until the first code is confirmed.
# Keeping it off the User row means an abandoned enrollment cannot lock anyone out.
PENDING_SECRET_KEY = "account.pending_totp_secret"


@router.get("/account")
async def account_page(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    users = list(db.scalars(select(User).order_by(User.username)))
    sessions = list(
        db.scalars(
            select(Session).where(Session.user_id == user.id).order_by(Session.created_at.desc())
        )
    )

    ctx = base_context(request, db, user, session, "account")
    ctx.update({
        "users": users,
        "sessions": sessions,
        "current_session_id": session.id,
        "totp_enabled": bool(user.totp_secret),
        "pending_secret": None,
        "pending_qr": None,
        "pending_uri": None,
    })
    return templates.TemplateResponse(request, "account.html", ctx)


@router.post("/account/password")
async def change_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)

    if not verify_password(user.password_hash, current_password):
        return redirect_with("/account", error="Current password is incorrect.")
    if new_password != new_password_confirm:
        return redirect_with("/account", error="The two new passwords do not match.")
    if problem := validate_password_strength(new_password):
        return redirect_with("/account", error=problem)

    user.password_hash = hash_password(new_password)
    db.add(user)

    # Invalidate every other session. A password change usually means the old one
    # was compromised, so leaving other logins alive defeats the point.
    others = list(
        db.scalars(
            select(Session).where(Session.user_id == user.id, Session.id != session.id)
        )
    )
    for stale in others:
        db.delete(stale)

    db.add(AuditLog(actor=user.username, action="account.password_changed",
                    detail=f"{len(others)} other session(s) revoked"))
    return redirect_with(
        "/account",
        success=f"Password changed. {len(others)} other session(s) signed out.",
    )


@router.post("/account/totp/begin")
async def totp_begin(
    request: Request,
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    """Generate a secret and show the QR. Nothing is saved to the user yet."""
    user, session = auth
    verify_csrf(session, csrf_token)
    if user.totp_secret:
        return redirect_with("/account", warning="Two-factor is already enabled.")

    from app.security import set_setting

    secret = new_totp_secret()
    # Held in settings, not on the user, so an abandoned enrollment can never
    # leave an account requiring a code nobody has.
    set_setting(db, f"{PENDING_SECRET_KEY}.{user.id}", secret, is_secret=True)

    uri = totp_uri(secret, user.username)
    ctx = base_context(request, db, user, session, "account")
    ctx.update({
        "users": list(db.scalars(select(User).order_by(User.username))),
        "sessions": list(
            db.scalars(
                select(Session).where(Session.user_id == user.id)
                .order_by(Session.created_at.desc())
            )
        ),
        "current_session_id": session.id,
        "totp_enabled": False,
        "pending_secret": secret,
        "pending_qr": totp_qr_svg(uri),
        "pending_uri": uri,
    })
    return templates.TemplateResponse(request, "account.html", ctx)


@router.post("/account/totp/confirm")
async def totp_confirm(
    code: str = Form(...),
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)

    from app.security import get_setting, set_setting

    secret = get_setting(db, f"{PENDING_SECRET_KEY}.{user.id}")
    if not secret:
        return redirect_with("/account", error="Start the setup again — no pending secret.")

    # Requiring a working code before enabling is the whole point: it proves the
    # authenticator is actually provisioned before the account depends on it.
    if not verify_totp(secret, code):
        return redirect_with("/account", error="That code is not valid. Try again.")

    user.totp_secret = secret
    db.add(user)
    set_setting(db, f"{PENDING_SECRET_KEY}.{user.id}", None)
    db.add(AuditLog(actor=user.username, action="account.totp_enabled"))
    return redirect_with("/account", success="Two-factor authentication is on.")


@router.post("/account/totp/disable")
async def totp_disable(
    password: str = Form(...),
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)
    # Password-gated: a borrowed unlocked browser should not be able to strip a factor.
    if not verify_password(user.password_hash, password):
        return redirect_with("/account", error="Password is incorrect.")

    user.totp_secret = None
    db.add(user)
    db.add(AuditLog(actor=user.username, action="account.totp_disabled"))
    return redirect_with("/account", warning="Two-factor authentication is off.")


@router.post("/account/sessions/revoke")
async def revoke_sessions(
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)
    others = list(
        db.scalars(select(Session).where(Session.user_id == user.id, Session.id != session.id))
    )
    for stale in others:
        db.delete(stale)
    db.add(AuditLog(actor=user.username, action="account.sessions_revoked",
                    detail=str(len(others))))
    return redirect_with("/account", success=f"Signed out {len(others)} other session(s).")


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

@router.post("/account/users")
async def add_user(
    new_username: str = Form(...),
    new_user_password: str = Form(...),
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only")

    name = new_username.strip().lower()
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        return redirect_with("/account", error="Usernames must be alphanumeric.")
    if db.scalar(select(User).where(User.username == name)):
        return redirect_with("/account", error=f"User {name!r} already exists.")
    if problem := validate_password_strength(new_user_password):
        return redirect_with("/account", error=problem)

    db.add(User(username=name, password_hash=hash_password(new_user_password), is_admin=True))
    db.add(AuditLog(actor=user.username, action="user.created", entity="user", entity_id=name))
    return redirect_with("/account", success=f"Created user {name!r}.")


@router.post("/account/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="No such user")
    if target.id == user.id:
        return redirect_with("/account", error="You cannot delete your own account.")
    # There is no password reset and no registration; deleting the last account
    # would make the instance permanently unreachable.
    if int(db.scalar(select(func.count(User.id))) or 0) <= 1:
        return redirect_with("/account", error="Cannot delete the only account.")

    name = target.username
    db.delete(target)
    db.add(AuditLog(actor=user.username, action="user.deleted", entity="user", entity_id=name))
    return redirect_with("/account", success=f"Deleted user {name!r}.")
