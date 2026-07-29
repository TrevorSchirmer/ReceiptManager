"""First-run setup, login, logout.

No registration and no password reset by design — this is a single-tenant tool on
a private network. The first request to any page creates the admin account; after
that, the password is the only gate.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from app.config import get_config
from app.formatting import when_full
from app.models import AuditLog, User
from app.security import (
    SESSION_COOKIE,
    create_session,
    destroy_session,
    hash_password,
    is_locked_out,
    is_setup_complete,
    needs_rehash,
    register_failed_login,
    register_successful_login,
    validate_password_strength,
    verify_password,
    verify_totp,
)
from app.web.deps import base_context, current_session, get_db, templates, verify_csrf

logger = logging.getLogger(__name__)
router = APIRouter()

ADMIN_USERNAME = "admin"


def _set_session_cookie(response: RedirectResponse, token: str) -> None:
    cfg = get_config()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=cfg.secure_cookies,
        max_age=cfg.session_ttl_hours * 3600,
        path="/",
    )


@router.get("/setup")
async def setup_form(request: Request, db: OrmSession = Depends(get_db)):
    if is_setup_complete(db):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    ctx = base_context(request, db, None, None)
    ctx["error"] = None
    return templates.TemplateResponse(request, "setup.html", ctx)


@router.post("/setup")
async def setup_submit(
    request: Request,
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: OrmSession = Depends(get_db),
):
    # No CSRF check here: there is no session yet, and this endpoint is a no-op
    # the moment an account exists.
    if is_setup_complete(db):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    def fail(message: str):
        ctx = base_context(request, db, None, None)
        ctx["error"] = message
        return templates.TemplateResponse(
            request, "setup.html", ctx, status_code=status.HTTP_400_BAD_REQUEST
        )

    if password != password_confirm:
        return fail("The two passwords do not match.")
    if problem := validate_password_strength(password):
        return fail(problem)

    user = User(username=ADMIN_USERNAME, password_hash=hash_password(password), is_admin=True)
    db.add(user)
    db.flush()
    db.add(AuditLog(actor=ADMIN_USERNAME, action="setup.completed", entity="user",
                    entity_id=str(user.id)))

    session = create_session(
        db, user,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    db.flush()

    logger.info("Admin account created")
    response = RedirectResponse("/settings?success=Welcome!+Configure+Outlook+and+Discord+below.",
                                status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, session.id)
    return response


def _user_count(db: OrmSession) -> int:
    return int(db.scalar(select(func.count(User.id))) or 0)


@router.get("/login")
async def login_form(request: Request, db: OrmSession = Depends(get_db)):
    if not is_setup_complete(db):
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
    ctx = base_context(request, db, None, None)
    ctx["error"] = None
    ctx["locked_until"] = None
    # Asking for a username on a single-account install is pure friction, so the
    # field only appears once a second account exists.
    ctx["needs_username"] = _user_count(db) > 1
    return templates.TemplateResponse(request, "login.html", ctx)


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
    username: str = Form(""),
    db: OrmSession = Depends(get_db),
):
    if not is_setup_complete(db):
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)

    multi_user = _user_count(db) > 1
    if username.strip():
        user = db.scalar(select(User).where(User.username == username.strip()))
    elif not multi_user:
        user = db.scalar(select(User))
    else:
        user = None

    def fail(message: str, locked: str | None = None):
        ctx = base_context(request, db, None, None)
        ctx["error"] = message
        ctx["locked_until"] = locked
        ctx["needs_username"] = multi_user
        return templates.TemplateResponse(
            request, "login.html", ctx, status_code=status.HTTP_401_UNAUTHORIZED
        )

    if user is None:
        # Same message whether the account or the password was wrong — no
        # enumeration oracle.
        return fail("Incorrect username or password.")

    if is_locked_out(user):
        return fail(
            "Too many failed attempts.",
            locked=when_full(user.locked_until, "UTC") if user.locked_until else None,
        )

    if not verify_password(user.password_hash, password):
        register_failed_login(db, user)
        logger.warning(
            "Failed login from %s", request.client.host if request.client else "unknown"
        )
        return fail("Incorrect username or password.")

    # Transparently upgrade the hash if argon2 parameters have changed.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    session = create_session(
        db, user,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    # Only a fully-authenticated login resets the lockout counter; with TOTP
    # enrolled that happens after the code, not after the password.
    if not session.totp_pending:
        register_successful_login(db, user)
    db.flush()

    target = "/login/totp" if session.totp_pending else "/"
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, session.id)
    return response


@router.get("/login/totp")
async def totp_form(request: Request, db: OrmSession = Depends(get_db)):
    session = current_session(request, db)
    if session is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if not session.totp_pending:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    ctx = base_context(request, db, None, session)
    ctx["error"] = None
    return templates.TemplateResponse(request, "login_totp.html", ctx)


@router.post("/login/totp")
async def totp_submit(
    request: Request,
    code: str = Form(...),
    csrf_token: str = Form(""),
    db: OrmSession = Depends(get_db),
):
    session = current_session(request, db)
    if session is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    verify_csrf(session, csrf_token)
    if not session.totp_pending:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    user = db.get(User, session.user_id)
    if user is None or not user.totp_secret:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    if not verify_totp(user.totp_secret, code):
        # A wrong code counts toward lockout: without it the second factor could
        # be brute-forced freely once the password is known.
        register_failed_login(db, user)
        logger.warning("Failed TOTP for %s", user.username)
        ctx = base_context(request, db, None, session)
        ctx["error"] = "That code is not valid."
        return templates.TemplateResponse(
            request, "login_totp.html", ctx, status_code=status.HTTP_401_UNAUTHORIZED
        )

    session.totp_pending = False
    db.add(session)
    register_successful_login(db, user)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(
    request: Request,
    csrf_token: str = Form(""),
    db: OrmSession = Depends(get_db),
):
    session = current_session(request, db)
    if session is not None:
        verify_csrf(session, csrf_token)
        destroy_session(db, session.id)
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
