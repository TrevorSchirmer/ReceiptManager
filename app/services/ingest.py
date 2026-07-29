"""Email ingest: Graph → RawEmail → parsed Transaction → queued notification.

Two invariants:

* **Never drop a charge.** If no parse rule matches, the transaction is still
  created (as ``needs_attention``, with the raw body retained) and still posted to
  Discord. Over-notifying is recoverable; a silently missing charge is not.
* **Ingest is idempotent.** ``RawEmail.internet_message_id`` is unique, so a delta
  replay, a resync after an expired cursor, or a double poll all no-op.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import settings_keys as sk
from app.db import run_db, session_scope
from app.formatting import money_plain
from app.models import (
    AuditLog,
    MerchantRule,
    ParseRule,
    RawEmail,
    Transaction,
    TransactionStatus,
    utcnow,
)
from app.services import jobs, parsing, shortcode
from app.services.graph import GraphClient, GraphCredentials, GraphError, GraphMessage

logger = logging.getLogger(__name__)

LAST_POLL_KEY = "runtime.last_poll_at"
LAST_MAIL_KEY = "runtime.last_mail_at"
LAST_ERROR_KEY = "runtime.last_poll_error"


def apply_merchant_rules(db: Session, tx: Transaction) -> MerchantRule | None:
    """Auto-file a charge from a merchant you never need a receipt from.

    Recurring SaaS is the case that matters: without this the bot nags monthly
    for a receipt that will never arrive, and that noise trains you to ignore the
    channel — which is how genuine receipts start getting missed.
    """
    for rule in db.scalars(select(MerchantRule).order_by(MerchantRule.id)):
        if not rule.matches(tx.merchant):
            continue
        if rule.category and not tx.category:
            tx.category = rule.category
        if rule.skip_receipt:
            tx.status = TransactionStatus.no_receipt_required
            if rule.note:
                tx.notes = f"{tx.notes}\n{rule.note}" if tx.notes else rule.note
        db.add(tx)
        return rule
    return None


def silence_merchant(
    db: Session,
    pattern: str,
    *,
    category: str | None = None,
    note: str | None = None,
    is_regex: bool = False,
) -> tuple[MerchantRule, int]:
    """Stop chasing receipts from a merchant, now and in future.

    Returns the rule and how many outstanding charges it just filed.

    The retroactive half is the point. Silencing a subscription while three of
    its charges are already sitting in the channel asking for receipts would
    leave exactly the noise the rule exists to remove — so their notifications
    are withdrawn too.

    Charges that already have a receipt are left alone: "I don't need a receipt
    for this" is not a reason to discard one that arrived.
    """
    from app.services import jobs

    rule = db.scalar(select(MerchantRule).where(MerchantRule.pattern == pattern))
    if rule is None:
        rule = MerchantRule(pattern=pattern, is_regex=is_regex, skip_receipt=True)
        db.add(rule)
    rule.enabled = True
    rule.skip_receipt = True
    if category:
        rule.category = category
    if note:
        rule.note = note
    db.flush()

    stale_statuses = [
        TransactionStatus.new.value,
        TransactionStatus.notified.value,
        TransactionStatus.lapsed.value,
        TransactionStatus.needs_attention.value,
    ]
    affected = 0
    for tx in db.scalars(select(Transaction).where(Transaction.status.in_(stale_statuses))):
        if tx.attachments or not rule.matches(tx.merchant):
            continue

        tx.status = TransactionStatus.no_receipt_required
        if rule.category and not tx.category:
            tx.category = rule.category
        if rule.note:
            tx.notes = f"{tx.notes}\n{rule.note}" if tx.notes else rule.note
        db.add(tx)

        # Withdraw the ask: cancel anything queued, and take down the message
        # already sitting in the channel.
        jobs.discard_for_transaction(db, tx.id)
        if tx.notify_message_id:
            jobs.enqueue(
                db,
                kind="discord.delete_message",
                payload={"message_id": tx.notify_message_id},
                idempotency_key=f"delmsg:{tx.notify_message_id}",
            )
            tx.notify_message_id = None
            db.add(tx)
        affected += 1

    logger.info("Silenced %r — %d outstanding charge(s) filed", pattern, affected)
    return rule, affected


def link_refund(db: Session, tx: Transaction) -> Transaction | None:
    """Point a credit at the charge it most likely reverses.

    Matches on the same merchant and the same absolute amount, most recent first.
    A guess, so it is recorded as a link rather than a status change — the UI lets
    you correct it, and the export carries both rows either way.
    """
    if tx.amount_minor >= 0:
        return None

    # Charges already claimed by some other refund. Without this, two refunds of
    # the same amount both point at the same original and one charge silently
    # looks reversed twice.
    already_claimed = select(Transaction.refund_of_id).where(
        Transaction.refund_of_id.is_not(None)
    )

    original = db.scalar(
        select(Transaction)
        .where(
            Transaction.id != tx.id,
            Transaction.merchant == tx.merchant,
            Transaction.amount_minor == -tx.amount_minor,
            Transaction.occurred_at <= tx.occurred_at,
            Transaction.refund_of_id.is_(None),   # the candidate is not itself a refund
            Transaction.id.not_in(already_claimed),
        )
        .order_by(Transaction.occurred_at.desc())
    )
    if original is None:
        return None
    tx.refund_of_id = original.id
    db.add(tx)
    return original


def _envelope_allowed(db: Session, sender: str, subject: str) -> bool:
    """Coarse pre-filter before per-rule matching, configured in Settings."""
    want_sender = sk.get_str(db, sk.FILTER_SENDER).strip().lower()
    want_subject = sk.get_str(db, sk.FILTER_SUBJECT).strip().lower()
    if want_sender and want_sender not in (sender or "").lower():
        return False
    if want_subject and want_subject not in (subject or "").lower():
        return False
    return True


def ingest_message(db: Session, message: GraphMessage) -> Transaction | None:
    """Persist one email and derive a transaction. Returns None if skipped.

    Safe to call repeatedly with the same message.
    """
    existing = db.scalar(
        select(RawEmail).where(RawEmail.internet_message_id == message.internet_message_id)
    )
    if existing is not None:
        return None

    if not _envelope_allowed(db, message.sender, message.subject):
        logger.debug("Skipping %s — envelope filter", message.internet_message_id)
        return None

    body_text = message.body_text
    if message.body_html:
        body_text = parsing.html_to_text(message.body_html)
    elif body_text:
        body_text = parsing.normalize_text(body_text)

    email = RawEmail(
        internet_message_id=message.internet_message_id,
        graph_message_id=message.graph_id,
        sender=message.sender,
        subject=message.subject,
        body_html=message.body_html,
        body_text=body_text,
        received_at=message.received_at,
    )
    db.add(email)
    try:
        db.flush()
    except IntegrityError:
        # Lost a race with a concurrent poll — the other writer has it.
        db.rollback()
        return None

    rules = list(db.scalars(select(ParseRule)))
    outcome = parsing.parse_email(rules, message.sender, message.subject, body_text or "")

    if outcome.fields is not None:
        fields = outcome.fields
        status = TransactionStatus.new
        email.parse_error = None
    else:
        # Parse failure is not a reason to lose the charge.
        fields = parsing.ParsedFields(
            merchant="(unparsed)",
            amount_minor=0,
            currency=sk.get_str(db, sk.DEFAULT_CURRENCY),
            card_ending=None,
            cardholder=None,
            occurred_at=None,
        )
        status = TransactionStatus.needs_attention
        email.parse_error = outcome.error
        logger.warning("Parse failed for %s: %s", message.internet_message_id, outcome.error)

    email.processed_at = utcnow()

    tx = shortcode.allocate(
        db,
        lambda code: Transaction(
            short_code=code,
            email_id=email.id,
            occurred_at=fields.occurred_at or message.received_at,
            merchant=fields.merchant or "(unknown)",
            amount_minor=fields.amount_minor,
            currency=fields.currency,
            card_ending=fields.card_ending,
            cardholder=fields.cardholder,
            status=status,
        ),
    )

    matched_rule = apply_merchant_rules(db, tx)
    refunded = link_refund(db, tx)

    detail = f"{tx.merchant} {money_plain(tx.amount_minor)} {tx.currency} ({tx.status.value})"
    if matched_rule is not None:
        detail += f" [merchant rule: {matched_rule.pattern}]"
    if refunded is not None:
        detail += f" [refund of #{refunded.short_code}]"
    db.add(
        AuditLog(
            actor="ingest",
            action="transaction.created",
            entity="transaction",
            entity_id=tx.short_code,
            detail=detail,
        )
    )

    if tx.status == TransactionStatus.no_receipt_required:
        # A merchant rule already filed it. Announcing would be pure noise, which
        # is precisely what the rule exists to prevent.
        logger.info("Auto-filed #%s (%s) — no receipt required", tx.short_code, tx.merchant)
        return tx

    # Persisted intent, not a direct send: if the process dies here the job
    # survives and the notification still goes out.
    jobs.enqueue(
        db,
        kind="discord.notify",
        payload={"transaction_id": tx.id},
        idempotency_key=f"notify:{tx.id}",
    )
    return tx


def ingest_batch(messages: list[GraphMessage]) -> int:
    """Ingest a batch in its own session. Returns the number of new transactions."""
    created = 0
    for message in messages:
        try:
            with session_scope() as db:
                if ingest_message(db, message) is not None:
                    created += 1
        except Exception:
            # One malformed email must not stall the whole batch.
            logger.exception("Failed to ingest %s", message.internet_message_id)
    return created


def _credentials(db: Session) -> GraphCredentials:
    return GraphCredentials(
        tenant_id=sk.get_str(db, sk.GRAPH_TENANT_ID),
        client_id=sk.get_str(db, sk.GRAPH_CLIENT_ID),
        client_secret=sk.get_str(db, sk.GRAPH_CLIENT_SECRET),
        mailbox=sk.get_str(db, sk.GRAPH_MAILBOX),
    )


def _read_poll_config() -> tuple[GraphCredentials, str, str | None, int] | None:
    with session_scope() as db:
        if not sk.is_configured_for_graph(db):
            return None
        folder_id = sk.get_str(db, sk.GRAPH_FOLDER_ID)
        if not folder_id:
            return None
        return (
            _credentials(db),
            folder_id,
            sk.get_str(db, sk.GRAPH_DELTA_LINK) or None,
            sk.get_int(db, sk.GRAPH_POLL_SECONDS),
        )


def _save_poll_result(delta_link: str | None, saw_mail: bool, error: str | None) -> None:
    from app.security import set_setting

    with session_scope() as db:
        if delta_link:
            sk.put(db, sk.GRAPH_DELTA_LINK, delta_link)
        set_setting(db, LAST_POLL_KEY, utcnow().isoformat())
        set_setting(db, LAST_ERROR_KEY, error or "")
        if saw_mail:
            set_setting(db, LAST_MAIL_KEY, utcnow().isoformat())


async def poll_once() -> int:
    """One poll cycle. Returns the number of transactions created."""
    config = await run_db(_read_poll_config)
    if config is None:
        return 0
    creds, folder_id, delta_link, _interval = config

    try:
        async with GraphClient(creds) as client:
            result = await client.delta(folder_id, delta_link)
    except GraphError as exc:
        logger.error("Graph poll failed: %s", exc)
        await run_db(_save_poll_result, None, False, str(exc))
        return 0

    created = await run_db(ingest_batch, result.messages) if result.messages else 0
    await run_db(_save_poll_result, result.delta_link, bool(result.messages), None)
    if created:
        logger.info("Ingested %d new transaction(s)", created)
    return created


async def poll_forever(stop: asyncio.Event) -> None:
    """Poll loop. Backs off on repeated failure but never gives up."""
    consecutive_failures = 0
    while not stop.is_set():
        interval = 15
        try:
            config = await run_db(_read_poll_config)
            if config is None:
                # Not configured yet — check back without hammering.
                interval = 30
            else:
                interval = max(5, config[3])
                await poll_once()
                consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            logger.exception("Unhandled error in poll loop")
            interval = min(interval * (2 ** min(consecutive_failures, 5)), 300)

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


def simulate_email(
    db: Session, *, sender: str, subject: str, body: str, is_html: bool = True
) -> Transaction | None:
    """Inject a synthetic email through the real pipeline.

    Powers the Settings "Simulate email" button — the only way to exercise
    ingest → parse → notify → match without waiting for a real charge.
    """
    now = utcnow()
    message = GraphMessage(
        graph_id=f"simulated-{now.timestamp()}",
        internet_message_id=f"<simulated-{now.timestamp()}@receiptmanager.local>",
        subject=subject,
        sender=sender,
        received_at=now,
        body_html=body if is_html else None,
        body_text=None if is_html else body,
    )
    return ingest_message(db, message)


def sweep_lapsed(db: Session) -> int:
    """Age out charges that never got a receipt.

    Lapsing suppresses *implicit* matching only — a lapsed charge stays fully
    visible in the UI, stays exportable, and can still be claimed by an explicit
    ``#code`` in Discord. See :func:`app.services.matching.find_by_code`.
    """
    hours = sk.get_int(db, sk.LAPSE_HOURS)
    cutoff = utcnow() - dt.timedelta(hours=hours)
    stale = list(
        db.scalars(
            select(Transaction).where(
                Transaction.status.in_([s.value for s in TransactionStatus.open_statuses()]),
                Transaction.notified_at.is_not(None),
                Transaction.notified_at < cutoff,
            )
        )
    )
    for tx in stale:
        tx.status = TransactionStatus.lapsed
        tx.lapsed_at = utcnow()
        db.add(tx)
    if stale:
        db.add(
            AuditLog(
                actor="scheduler",
                action="transactions.lapsed",
                detail=f"{len(stale)} charge(s) lapsed after {hours}h",
            )
        )
        logger.info("Lapsed %d transaction(s)", len(stale))
    return len(stale)
