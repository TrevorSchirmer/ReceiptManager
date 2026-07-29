"""Queued Discord side effects.

Everything here is a job handler, which means two things: it may be retried, so
it must be idempotent; and it runs *after* the state change that scheduled it has
already been committed, so it can never leave the database inconsistent by
failing.

``discord.finalize`` is the one that deletes messages. It re-runs
:func:`app.services.storage.assert_durable` immediately beforehand — not because
capture did not already verify, but because time passes between capture and
deletion, and in that window a disk can fill, a mount can disappear, or a restore
can roll the filesystem back. Deleting on a stale assumption is unrecoverable;
keeping a message we meant to delete is merely untidy.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from sqlalchemy import func, select

from app import settings_keys as sk
from app.db import run_db, session_scope
from app.formatting import money, when
from app.models import (
    Attachment,
    AuditLog,
    DiscordMessage,
    Transaction,
    TransactionStatus,
    utcnow,
)
from app.services import jobs
from app.discordbot.bot import get_service, render_notification, render_summary

logger = logging.getLogger(__name__)


class NotConnected(RuntimeError):
    """Raised so the job retries once the gateway is back."""


async def _channel() -> discord.abc.Messageable:
    service = get_service()
    if not service.is_connected or service.client is None:
        raise NotConnected("Discord gateway is not connected")
    channel_id = await run_db(_read_channel_id)
    if not channel_id:
        raise NotConnected("No Discord channel configured")
    channel = service.client.get_channel(int(channel_id))
    if channel is None:
        channel = await service.client.fetch_channel(int(channel_id))
    if not isinstance(channel, discord.abc.Messageable):
        raise NotConnected(f"Channel {channel_id} is not a text channel")
    return channel


def _read_channel_id() -> str:
    with session_scope() as db:
        return sk.get_str(db, sk.DISCORD_CHANNEL_ID)


# --------------------------------------------------------------------------- #
# Notify
# --------------------------------------------------------------------------- #

def _load_notification(tx_id: int) -> dict[str, Any] | None:
    with session_scope() as db:
        tx = db.get(Transaction, tx_id)
        if tx is None:
            return None
        if tx.notify_message_id:
            return None  # already announced; retry after a partial failure
        tz = sk.get_str(db, sk.TIMEZONE)
        return {"content": render_notification(tx, tz), "short_code": tx.short_code}


def _record_notification(tx_id: int, message_id: str, channel_id: str) -> None:
    with session_scope() as db:
        tx = db.get(Transaction, tx_id)
        if tx is None:
            return
        tx.notify_message_id = str(message_id)
        tx.notified_at = utcnow()
        if tx.status == TransactionStatus.new:
            tx.status = TransactionStatus.notified
        db.add(tx)
        db.add(
            DiscordMessage(
                message_id=str(message_id),
                channel_id=str(channel_id),
                direction="out",
                kind="notify",
                transaction_id=tx.id,
            )
        )


@jobs.handler("discord.notify")
async def notify(payload: dict[str, Any]) -> None:
    tx_id = int(payload["transaction_id"])
    data = await run_db(_load_notification, tx_id)
    if data is None:
        return
    channel = await _channel()
    message = await channel.send(data["content"])
    await run_db(_record_notification, tx_id, str(message.id), str(message.channel.id))
    logger.info("Announced #%s", data["short_code"])


# --------------------------------------------------------------------------- #
# Finalize: confirm, then delete
# --------------------------------------------------------------------------- #

def _load_finalize(tx_id: int, attachment_ids: list[int]) -> dict[str, Any] | None:
    """Gather what to delete, verifying every file is durably on disk first."""
    from app.services import storage

    with session_scope() as db:
        tx = db.get(Transaction, tx_id)
        if tx is None:
            return None

        source_message_ids: set[str] = set()
        for att_id in attachment_ids:
            att = db.get(Attachment, att_id)
            if att is None or att.transaction_id != tx.id:
                continue
            # Raises CaptureError -> the job retries and nothing is deleted.
            storage.assert_durable(att.path, att.sha256, att.bytes)
            if att.source_message_id:
                source_message_ids.add(att.source_message_id)

        return {
            "summary": render_summary(tx),
            "notify_message_id": tx.notify_message_id,
            "receipt_message_ids": sorted(source_message_ids),
            "delete_receipt": sk.get_bool(db, sk.DISCORD_DELETE_RECEIPT),
            "delete_notify": sk.get_bool(db, sk.DISCORD_DELETE_NOTIFY),
            "keep_confirmation": sk.get_bool(db, sk.DISCORD_KEEP_CONFIRMATION),
        }


def _mark_deleted(message_ids: list[str], attachment_ids: list[int]) -> None:
    with session_scope() as db:
        for mid in message_ids:
            record = db.scalar(
                select(DiscordMessage).where(DiscordMessage.message_id == str(mid))
            )
            if record is not None:
                record.deleted_at = utcnow()
                db.add(record)
        for att_id in attachment_ids:
            att = db.get(Attachment, att_id)
            if att is not None:
                att.source_deleted = True
                db.add(att)


def _record_confirmation(tx_id: int, message_id: str, channel_id: str) -> None:
    with session_scope() as db:
        db.add(
            DiscordMessage(
                message_id=str(message_id),
                channel_id=str(channel_id),
                direction="out",
                kind="confirm",
                transaction_id=tx_id,
            )
        )
        db.add(
            AuditLog(
                actor="discord",
                action="receipt.finalized",
                entity="transaction",
                entity_id=str(tx_id),
            )
        )


@jobs.handler("discord.finalize")
async def finalize(payload: dict[str, Any]) -> None:
    tx_id = int(payload["transaction_id"])
    attachment_ids = [int(i) for i in payload.get("attachment_ids", [])]

    # Raises if any file fails verification — the job retries, nothing is deleted.
    data = await run_db(_load_finalize, tx_id, attachment_ids)
    if data is None:
        return

    channel = await _channel()

    if data["keep_confirmation"]:
        confirmation = await channel.send(f"✅ {data['summary']} — receipt stored")
        await run_db(
            _record_confirmation, tx_id, str(confirmation.id), str(confirmation.channel.id)
        )

    to_delete: list[str] = []
    if data["delete_receipt"]:
        to_delete.extend(data["receipt_message_ids"])
    if data["delete_notify"] and data["notify_message_id"]:
        to_delete.append(data["notify_message_id"])

    deleted: list[str] = []
    for message_id in to_delete:
        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
            deleted.append(message_id)
        except discord.NotFound:
            deleted.append(message_id)  # already gone; the goal is met
        except discord.Forbidden:
            # Missing Manage Messages. Cosmetic — the receipt is safely stored, so
            # do not fail the job and retry forever.
            logger.error(
                "Cannot delete message %s — the bot lacks Manage Messages in this channel",
                message_id,
            )
        except discord.HTTPException:
            logger.warning("Failed to delete message %s", message_id, exc_info=True)

    if deleted:
        await run_db(_mark_deleted, deleted, attachment_ids)


# --------------------------------------------------------------------------- #
# Daily digest
# --------------------------------------------------------------------------- #

def _build_digest() -> str | None:
    """Outstanding receipts, warning about anything lapsing tonight.

    Runs *before* the lapse sweep so the warning is actionable — that ordering is
    the whole reason the digest is worth sending.
    """
    import datetime as dt

    with session_scope() as db:
        if not sk.get_bool(db, sk.DIGEST_ENABLED):
            return None
        tz = sk.get_str(db, sk.TIMEZONE)
        lapse_hours = sk.get_int(db, sk.LAPSE_HOURS)
        now = utcnow()
        warn_cutoff = now - dt.timedelta(hours=max(lapse_hours - 24, 0))

        open_rows = list(
            db.scalars(
                select(Transaction)
                .where(
                    Transaction.status.in_(
                        [s.value for s in TransactionStatus.open_statuses()]
                        + [TransactionStatus.needs_attention.value]
                    )
                )
                .order_by(Transaction.occurred_at)
            )
        )
        recently_lapsed = int(
            db.scalar(
                select(func.count(Transaction.id)).where(
                    Transaction.status == TransactionStatus.lapsed,
                    Transaction.lapsed_at >= now - dt.timedelta(days=7),
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

        if not open_rows and not orphans and not recently_lapsed:
            return None

        lines = [f"📋 **Outstanding receipts — {len(open_rows)}**", ""]
        lapsing = [tx for tx in open_rows if tx.notified_at and tx.notified_at <= warn_cutoff]
        lapsing_ids = {tx.id for tx in lapsing}
        normal = [tx for tx in open_rows if tx.id not in lapsing_ids]

        if lapsing:
            lines.append("⚠️ **Lapsing soon**")
            for tx in lapsing:
                lines.append(
                    f"　`#{tx.short_code}` · {money(tx.amount_minor, tx.currency)} · "
                    f"{tx.merchant} · {when(tx.occurred_at, tz)}"
                )
            lines.append("")
        for tx in normal:
            lines.append(
                f"　`#{tx.short_code}` · {money(tx.amount_minor, tx.currency)} · "
                f"{tx.merchant} · {when(tx.occurred_at, tz)}"
            )

        footer = []
        if recently_lapsed:
            footer.append(f"Lapsed in the last 7 days: **{recently_lapsed}**")
        if orphans:
            footer.append(f"Unassigned receipts: **{orphans}**")
        if footer:
            lines.extend(["", " · ".join(footer)])
        lines.append("")
        lines.append("_Reply to any charge above with its receipt, or use_ `#code`.")
        return "\n".join(lines)


@jobs.handler("discord.digest")
async def digest(_payload: dict[str, Any]) -> None:
    content = await run_db(_build_digest)
    if not content:
        return
    channel = await _channel()
    # Discord hard-caps a message at 2000 characters.
    for chunk in _chunk(content, 1900):
        await channel.send(chunk)


@jobs.handler("discord.delete_message")
async def delete_message(payload: dict[str, Any]) -> None:
    """Remove one message, e.g. after its charge was deleted from the UI.

    Purely cosmetic, so a missing permission is logged rather than retried
    forever — the database is already in the state the user asked for.
    """
    message_id = str(payload.get("message_id") or "")
    if not message_id:
        return
    channel = await _channel()
    try:
        message = await channel.fetch_message(int(message_id))
        await message.delete()
    except discord.NotFound:
        pass  # already gone; the goal is met
    except discord.Forbidden:
        logger.error("Cannot delete message %s — the bot lacks Manage Messages", message_id)


@jobs.handler("discord.alert")
async def alert(payload: dict[str, Any]) -> None:
    """Operational alert (heartbeat, connection loss). Loud on purpose."""
    content = str(payload.get("content") or "").strip()
    if not content:
        return
    channel = await _channel()
    for chunk in _chunk(content, 1900):
        await channel.send(chunk)


@jobs.handler("workflow.lapse_sweep")
async def lapse_sweep(_payload: dict[str, Any]) -> None:
    from app.services.ingest import sweep_lapsed

    def run() -> int:
        with session_scope() as db:
            return sweep_lapsed(db)

    count = await run_db(run)
    if count:
        logger.info("Lapse sweep marked %d transaction(s)", count)


def _chunk(text: str, limit: int) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if size + len(line) + 1 > limit and current:
            out.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        out.append("\n".join(current))
    return out
