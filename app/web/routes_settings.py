"""Settings, parse rules, the live rule tester, and the email simulator."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app import settings_keys as sk
from app.models import AuditLog, MerchantRule, ParseRule, RawEmail
from app.services import ingest, parsing
from app.web.deps import base_context, get_db, redirect_with, require_user, templates, verify_csrf

logger = logging.getLogger(__name__)
router = APIRouter()

MASK = "••••••••"


@router.get("/settings")
async def settings_page(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    values: dict[str, str] = {}
    for key in sk.ALL_KEYS:
        if key.name == sk.GRAPH_DELTA_LINK.name:
            continue
        # Secrets are never echoed back to the browser — only whether one is set.
        values[key.name] = MASK if (key.is_secret and sk.get_str(db, key)) else sk.get_str(db, key)

    ctx = base_context(request, db, user, session, "settings")
    ctx.update({
        "keys": [k for k in sk.ALL_KEYS if k.name != sk.GRAPH_DELTA_LINK.name],
        "values": values,
        "mask": MASK,
        "graph_ready": sk.is_configured_for_graph(db),
        "discord_ready": sk.is_configured_for_discord(db),
    })
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings")
async def settings_save(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    form = await request.form()
    verify_csrf(session, str(form.get("csrf_token") or ""))

    changed: list[str] = []
    for key in sk.ALL_KEYS:
        if key.name == sk.GRAPH_DELTA_LINK.name:
            continue
        if key.kind == "bool":
            new_value = "true" if form.get(key.name) else "false"
        elif key.name not in form:
            continue
        else:
            new_value = str(form.get(key.name) or "").strip()

        # An unchanged masked secret means "leave it alone" — never overwrite a
        # stored credential with the placeholder.
        if key.is_secret and new_value in ("", MASK):
            continue
        if new_value != sk.get_str(db, key):
            sk.put(db, key, new_value)
            changed.append(key.name)

    if changed:
        db.add(AuditLog(actor=user.username, action="settings.updated",
                        detail=", ".join(changed)))
        # Changing the mailbox or folder invalidates the delta cursor.
        if any(n.startswith("graph.") and n != sk.GRAPH_POLL_SECONDS.name for n in changed):
            sk.put(db, sk.GRAPH_DELTA_LINK, "")

    return redirect_with("/settings", success=f"Saved {len(changed)} setting(s)."
                         if changed else "No changes.")


@router.post("/settings/mail-folders")
async def list_mail_folders(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    """Populate the folder picker from the live mailbox."""
    _user, session = auth
    form = await request.form()
    verify_csrf(session, str(form.get("csrf_token") or ""))

    if not sk.is_configured_for_graph(db):
        return JSONResponse({"error": "Graph is not fully configured yet."}, status_code=400)

    from app.services.graph import GraphClient, GraphError

    creds = ingest._credentials(db)  # noqa: SLF001 - same package, intentional reuse
    try:
        async with GraphClient(creds) as client:
            folders = await client.list_folders()
    except GraphError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"folders": folders})


# --------------------------------------------------------------------------- #
# Parse rules
# --------------------------------------------------------------------------- #

@router.get("/rules")
async def rules_page(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    rules = list(db.scalars(select(ParseRule).order_by(ParseRule.priority, ParseRule.id)))
    merchant_rules = list(db.scalars(select(MerchantRule).order_by(MerchantRule.id)))
    sample = db.scalar(select(RawEmail).order_by(RawEmail.received_at.desc()))
    ctx = base_context(request, db, user, session, "rules")
    ctx.update({"rules": rules, "merchant_rules": merchant_rules, "sample": sample})
    return templates.TemplateResponse(request, "rules.html", ctx)


@router.post("/rules")
async def rule_save(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    form = await request.form()
    verify_csrf(session, str(form.get("csrf_token") or ""))

    rule_id = str(form.get("id") or "").strip()
    rule = db.get(ParseRule, int(rule_id)) if rule_id.isdigit() else None
    if rule is None:
        rule = ParseRule(name="", body_regex="")
        db.add(rule)

    rule.name = str(form.get("name") or "Untitled rule").strip()
    rule.enabled = bool(form.get("enabled"))
    rule.priority = int(str(form.get("priority") or "100") or 100)
    rule.sender_match = str(form.get("sender_match") or "").strip()
    rule.subject_match = str(form.get("subject_match") or "").strip()
    rule.match_is_regex = bool(form.get("match_is_regex"))
    rule.body_regex = str(form.get("body_regex") or "").strip()
    rule.default_currency = (str(form.get("default_currency") or "USD").strip() or "USD").upper()
    rule.date_format = str(form.get("date_format") or "").strip() or None

    db.add(AuditLog(actor=user.username, action="rule.saved", entity="parse_rule",
                    entity_id=str(rule.id or "new"), detail=rule.name))
    return redirect_with("/rules", success="Rule saved.")


@router.post("/rules/{rule_id}/delete")
async def rule_delete(
    rule_id: int,
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)
    rule = db.get(ParseRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="No such rule")
    db.delete(rule)
    db.add(AuditLog(actor=user.username, action="rule.deleted", entity="parse_rule",
                    entity_id=str(rule_id)))
    return redirect_with("/rules", success="Rule deleted.")


@router.post("/merchant-rules")
async def merchant_rule_save(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    form = await request.form()
    verify_csrf(session, str(form.get("csrf_token") or ""))

    pattern = str(form.get("pattern") or "").strip()
    if not pattern:
        return redirect_with("/rules", error="A merchant pattern is required.")

    rule_id = str(form.get("id") or "").strip()
    rule = db.get(MerchantRule, int(rule_id)) if rule_id.isdigit() else None
    if rule is None:
        rule = MerchantRule(pattern=pattern)
        db.add(rule)

    rule.pattern = pattern
    rule.is_regex = bool(form.get("is_regex"))
    rule.enabled = bool(form.get("enabled"))
    rule.skip_receipt = bool(form.get("skip_receipt"))
    rule.category = str(form.get("category") or "").strip() or None
    rule.note = str(form.get("note") or "").strip() or None

    db.add(AuditLog(actor=user.username, action="merchant_rule.saved",
                    entity="merchant_rule", entity_id=str(rule.id or "new"), detail=pattern))
    return redirect_with("/rules", success="Merchant rule saved.")


@router.post("/merchant-rules/{rule_id}/delete")
async def merchant_rule_delete(
    rule_id: int,
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    user, session = auth
    verify_csrf(session, csrf_token)
    rule = db.get(MerchantRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="No such rule")
    db.delete(rule)
    db.add(AuditLog(actor=user.username, action="merchant_rule.deleted",
                    entity="merchant_rule", entity_id=str(rule_id)))
    return redirect_with("/rules", success="Merchant rule deleted.")


@router.post("/rules/test")
async def rule_test(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    """Live tester. Returns JSON so the page can highlight the match in place."""
    _user, session = auth
    form = await request.form()
    verify_csrf(session, str(form.get("csrf_token") or ""))

    candidate = ParseRule(
        name=str(form.get("name") or "(draft)"),
        enabled=True,
        priority=100,
        sender_match=str(form.get("sender_match") or ""),
        subject_match=str(form.get("subject_match") or ""),
        match_is_regex=bool(form.get("match_is_regex")),
        body_regex=str(form.get("body_regex") or ""),
        default_currency=(str(form.get("default_currency") or "USD")).upper(),
        date_format=str(form.get("date_format") or "") or None,
    )
    result = parsing.test_rule(
        candidate,
        sender=str(form.get("sample_sender") or ""),
        subject=str(form.get("sample_subject") or ""),
        raw_body=str(form.get("sample_body") or ""),
        is_html=bool(form.get("sample_is_html")),
    )
    return JSONResponse(result)


@router.post("/rules/reparse")
async def reparse(
    csrf_token: str = Form(""),
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    """Re-run the current rules over stored emails that previously failed.

    You will not get the regex right on the first try, and this is what makes
    that recoverable.
    """
    user, session = auth
    verify_csrf(session, csrf_token)

    rules = list(db.scalars(select(ParseRule)))
    stalled = list(db.scalars(select(RawEmail).where(RawEmail.parse_error.is_not(None))))
    fixed = 0
    for email in stalled:
        outcome = parsing.parse_email(rules, email.sender, email.subject, email.body_text or "")
        if outcome.fields is None:
            continue
        for tx in email.transactions:
            fields = outcome.fields
            tx.merchant = fields.merchant or tx.merchant
            tx.amount_minor = fields.amount_minor
            tx.currency = fields.currency
            tx.card_last4 = fields.card_last4 or tx.card_last4
            tx.cardholder = fields.cardholder or tx.cardholder
            if fields.occurred_at:
                tx.occurred_at = fields.occurred_at
            from app.models import TransactionStatus

            if tx.status == TransactionStatus.needs_attention:
                tx.status = (
                    TransactionStatus.notified if tx.notified_at else TransactionStatus.new
                )
            db.add(tx)
        email.parse_error = None
        db.add(email)
        fixed += 1

    if fixed:
        db.add(AuditLog(actor=user.username, action="rules.reparsed",
                        detail=f"{fixed} email(s) re-parsed"))
    return redirect_with("/rules", success=f"Re-parsed {fixed} of {len(stalled)} stalled email(s).")


@router.post("/settings/simulate")
async def simulate(
    request: Request,
    auth=Depends(require_user),
    db: OrmSession = Depends(get_db),
):
    """Inject a fake email through the real pipeline, end to end.

    The only way to verify ingest → parse → Discord → match without spending
    money on a real charge.
    """
    user, session = auth
    form = await request.form()
    verify_csrf(session, str(form.get("csrf_token") or ""))

    tx = ingest.simulate_email(
        db,
        sender=str(form.get("sender") or "alerts@example.com"),
        subject=str(form.get("subject") or "Card transaction alert"),
        body=str(form.get("body") or ""),
        is_html=bool(form.get("is_html")),
    )
    if tx is None:
        return redirect_with(
            "/settings",
            warning="The simulated email was skipped — check the sender/subject filters.",
        )
    db.add(AuditLog(actor=user.username, action="email.simulated", entity="transaction",
                    entity_id=tx.short_code))
    return redirect_with(
        "/settings",
        success=f"Created #{tx.short_code} ({tx.status.value}) — it should appear in Discord.",
    )
