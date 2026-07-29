"""Discord gateway client.

The gateway is an *outbound* WebSocket, so this app needs no inbound ports and no
tunnel — combined with Graph polling, the container sits entirely behind the
firewall.

One ordering rule governs this file: **bytes are captured and verified before
anything else happens, and the source message is deleted only afterwards.**
Discord attachment URLs are signed and expiring, and a deleted message's
attachment cannot be recovered. So capture never waits on a human, capture
failure never deletes anything, and deletion always runs as a queued job that
re-verifies the file on disk first.

Requires the ``MESSAGE_CONTENT`` privileged intent (Developer Portal → Bot →
Privileged Gateway Intents) to read ``#1042`` codes, and ``Manage Messages`` in
the channel to delete the uploader's message.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

import discord
from sqlalchemy import select

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
from app.services import jobs, matching, storage
from app.services.matching import MatchMethod

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_notification(tx: Transaction, tz: str) -> str:
    lines = [f"**#{tx.short_code}** · **{money(tx.amount_minor, tx.currency)}** · {tx.merchant}"]
    meta = []
    if tx.card_last4:
        meta.append(f"Card ••{tx.card_last4}")
    meta.append(when(tx.occurred_at, tz))
    lines.append(" · ".join(meta))
    if tx.status == TransactionStatus.needs_attention:
        lines.append("⚠️ _Could not parse this alert — check the raw email in the UI._")
    lines.append("")
    lines.append(
        f"_Reply to this message with the receipt, or upload it with_ "
        f"`#{tx.short_code}` _in the caption._"
    )
    return "\n".join(lines)


def render_option_label(tx: Transaction, tz: str) -> str:
    # Discord caps select option labels at 100 characters.
    return f"#{tx.short_code} · {money(tx.amount_minor, tx.currency)} · {tx.merchant}"[:100]


def render_summary(tx: Transaction) -> str:
    return f"#{tx.short_code} · {tx.merchant} · {money(tx.amount_minor, tx.currency)}"


# --------------------------------------------------------------------------- #
# Sync DB helpers — always invoked through run_db, never from the event loop
# --------------------------------------------------------------------------- #

def _persist_capture(
    *,
    rel_path: str,
    thumb_rel_path: str | None,
    mime: str,
    size: int,
    sha256: str,
    original_filename: str | None,
    message_id: str,
    channel_id: str,
    uploader_id: str,
    uploader_name: str,
) -> int:
    """Commit the row for an already-verified file. Returns the attachment id.

    ``transaction_id`` starts NULL on purpose: capture must never block on
    matching. An unmatched row *is* the orphan queue.
    """
    with session_scope() as db:
        att = Attachment(
            transaction_id=None,
            path=rel_path,
            thumb_path=thumb_rel_path,
            original_filename=original_filename,
            mime=mime,
            bytes=size,
            sha256=sha256,
            source_message_id=str(message_id),
            uploader_id=str(uploader_id),
            uploader_name=uploader_name[:128],
        )
        db.add(att)
        db.add(
            DiscordMessage(
                message_id=str(message_id),
                channel_id=str(channel_id),
                direction="in",
                kind="receipt",
                author_id=str(uploader_id),
                content=(original_filename or "")[:4000] or None,
            )
        )
        db.flush()
        return att.id


def _resolve_upload(referenced_message_id: str | None, text: str | None) -> dict[str, Any]:
    """Run matching, returning plain data — ORM objects must not cross threads."""
    with session_scope() as db:
        tz = sk.get_str(db, sk.TIMEZONE)
        result = matching.resolve(db, referenced_message_id=referenced_message_id, text=text)
        payload: dict[str, Any] = {
            "method": result.method.value,
            "truncated": result.truncated,
            "candidates": [
                (tx.short_code, render_option_label(tx, tz), when(tx.occurred_at, tz))
                for tx in result.candidates
            ],
            "short_code": result.transaction.short_code if result.transaction else None,
        }
        return payload


def _link_attachments(attachment_ids: list[int], short_code: str) -> dict[str, Any] | None:
    """Attach captured files to a charge. Idempotent; skips already-linked rows."""
    with session_scope() as db:
        tx = db.scalar(select(Transaction).where(Transaction.short_code == short_code))
        if tx is None:
            return None

        linked = 0
        for att_id in attachment_ids:
            att = db.get(Attachment, att_id)
            if att is None or att.transaction_id is not None:
                continue
            att.transaction_id = tx.id
            db.add(att)
            linked += 1

        if linked:
            tx.status = TransactionStatus.receipt_attached
            tx.matched_at = utcnow()
            db.add(tx)
            db.add(
                AuditLog(
                    actor="discord",
                    action="receipt.attached",
                    entity="transaction",
                    entity_id=tx.short_code,
                    detail=f"{linked} file(s) attached",
                )
            )
            jobs.enqueue(
                db,
                kind="discord.finalize",
                payload={"transaction_id": tx.id, "attachment_ids": attachment_ids},
                idempotency_key=f"finalize:{tx.id}:{min(attachment_ids)}",
            )
        return {"summary": render_summary(tx), "transaction_id": tx.id, "linked": linked}


def _read_channel_config() -> dict[str, Any]:
    with session_scope() as db:
        return {
            "channel_id": sk.get_str(db, sk.DISCORD_CHANNEL_ID),
            "allowed": sk.allowed_uploader_ids(db),
            "timeout_min": min(sk.get_int(db, sk.DISCORD_SELECT_TIMEOUT_MIN), 15),
        }


# --------------------------------------------------------------------------- #
# Picker
# --------------------------------------------------------------------------- #

class ReceiptPicker(discord.ui.View):
    """Asks which charge an ambiguous receipt belongs to.

    The files are *already stored* by the time this appears — answering only sets
    a foreign key. If nobody answers, the receipts stay in the orphan queue,
    visible and assignable from the web UI, and the source message is left alone
    so the human can still see what they sent.
    """

    def __init__(
        self,
        *,
        attachment_ids: list[int],
        candidates: list[tuple[str, str, str]],
        uploader_id: int,
        timeout_seconds: float,
        truncated: bool = False,
        prompt_recent: bool = False,
    ) -> None:
        super().__init__(timeout=timeout_seconds)
        self.attachment_ids = attachment_ids
        self.uploader_id = uploader_id
        self.message: discord.Message | None = None

        options = [
            discord.SelectOption(label=label, value=code, description=desc[:100] or None)
            for code, label, desc in candidates
        ]
        options.append(
            discord.SelectOption(
                label="None of these — hold for later",
                value="__orphan__",
                description="Keep it in the orphan queue and assign it from the web UI",
            )
        )

        if truncated:
            placeholder = "Which charge? (25 most recent shown)"
        elif prompt_recent:
            placeholder = "No charge is awaiting a receipt — pick a recent one?"
        else:
            placeholder = "Which charge is this receipt for?"

        self.select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder=placeholder, options=options[:25], min_values=1, max_values=1
        )
        self.select.callback = self._on_select  # type: ignore[method-assign]
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.uploader_id:
            return True
        await interaction.response.send_message(
            "Only the person who uploaded this receipt can assign it.", ephemeral=True
        )
        return False

    async def _on_select(self, interaction: discord.Interaction) -> None:
        choice = self.select.values[0]
        self.stop()

        if choice == "__orphan__":
            await interaction.response.edit_message(
                content="📥 Held in the orphan queue — assign it from the web UI.", view=None
            )
            return

        result = await run_db(_link_attachments, self.attachment_ids, choice)
        if result is None:
            await interaction.response.edit_message(
                content=f"⚠️ Could not find charge `#{choice}`.", view=None
            )
            return
        await interaction.response.edit_message(
            content=f"✅ {result['summary']} — receipt stored", view=None
        )

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(
                content=(
                    "⏳ No charge selected — the receipt is held in the orphan queue "
                    "and can be assigned from the web UI."
                ),
                view=None,
            )
        except discord.HTTPException:
            pass


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class ReceiptBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # Required to read "#1042" out of a caption. Must also be enabled in the
        # Developer Portal or the gateway silently delivers empty content.
        intents.message_content = True
        super().__init__(intents=intents)
        self.ready_at: dt.datetime | None = None
        self.tree = discord.app_commands.CommandTree(self)

        from app.discordbot import commands

        commands.register(self.tree)

    async def on_ready(self) -> None:
        self.ready_at = utcnow()
        logger.info("Discord connected as %s", self.user)
        await self._sync_commands()

    async def _sync_commands(self) -> None:
        """Publish slash commands to the configured channel's guild.

        Guild-scoped rather than global: a global sync can take up to an hour to
        propagate, while a guild sync is effectively immediate — which matters
        because the commands are useless until they appear.
        """
        channel_id = await run_db(_read_channel_config)
        if not channel_id["channel_id"]:
            return
        try:
            channel = self.get_channel(int(channel_id["channel_id"]))
            if channel is None:
                channel = await self.fetch_channel(int(channel_id["channel_id"]))
            guild = getattr(channel, "guild", None)
            if guild is None:
                logger.warning("Configured channel has no guild; skipping command sync")
                return
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d slash command(s) to guild %s", len(synced), guild.id)
        except discord.HTTPException:
            # Commands are a convenience; capture must not depend on them.
            logger.warning("Could not sync slash commands", exc_info=True)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user is None:
            return
        if not message.attachments:
            return

        config = await run_db(_read_channel_config)
        if not config["channel_id"] or str(message.channel.id) != config["channel_id"]:
            return

        allowed: set[str] = config["allowed"]
        if allowed and str(message.author.id) not in allowed:
            logger.warning(
                "Rejected receipt from unauthorized uploader %s (%s)",
                message.author.id, message.author,
            )
            await _safe_reply(
                message,
                "⛔ You are not on this server's authorized uploader list, so this "
                "receipt was not stored.",
            )
            return

        await self._capture_message(message, config)

    async def _capture_message(self, message: discord.Message, config: dict[str, Any]) -> None:
        attachment_ids: list[int] = []
        failures: list[str] = []

        for attachment in message.attachments:
            try:
                data = await attachment.read()
            except (discord.HTTPException, discord.NotFound) as exc:
                failures.append(f"{attachment.filename}: download failed ({exc})")
                continue

            try:
                # Blocking: hashing, transcoding and fsync all belong off the loop.
                stored = await asyncio.to_thread(
                    storage.store_receipt_bytes,
                    data,
                    original_filename=attachment.filename,
                    expected_bytes=attachment.size,
                )
            except storage.CaptureError as exc:
                failures.append(f"{attachment.filename}: {exc}")
                continue
            except Exception as exc:
                logger.exception("Unexpected capture failure for %s", attachment.filename)
                failures.append(f"{attachment.filename}: {exc}")
                continue

            att_id = await run_db(
                _persist_capture,
                rel_path=stored.rel_path,
                thumb_rel_path=stored.thumb_rel_path,
                mime=stored.mime,
                size=stored.bytes,
                sha256=stored.sha256,
                original_filename=attachment.filename,
                message_id=str(message.id),
                channel_id=str(message.channel.id),
                uploader_id=str(message.author.id),
                uploader_name=str(message.author),
            )
            attachment_ids.append(att_id)

        if failures:
            # Loud, and nothing is deleted. A failed capture that silently deleted
            # the message would destroy the only copy of the receipt.
            await _safe_reply(
                message,
                "⚠️ Could not store:\n"
                + "\n".join(f"• {f}" for f in failures)
                + "\n\nThe message has been left in place — please re-send.",
            )
        if not attachment_ids:
            return

        referenced = (
            str(message.reference.message_id)
            if message.reference and message.reference.message_id
            else None
        )
        outcome = await run_db(_resolve_upload, referenced, message.content)
        method = MatchMethod(outcome["method"])

        if method in (MatchMethod.reply, MatchMethod.code, MatchMethod.sole_open):
            result = await run_db(_link_attachments, attachment_ids, outcome["short_code"])
            if result is None:
                await _safe_reply(message, "⚠️ That charge no longer exists.")
                return
            note = ""
            if method is MatchMethod.sole_open:
                note = "\n_(only one charge was awaiting a receipt — reply with_ `#code` _to correct)_"
            await _safe_reply(message, f"✅ {result['summary']} — receipt stored{note}")
            return

        picker = ReceiptPicker(
            attachment_ids=attachment_ids,
            candidates=outcome["candidates"],
            uploader_id=message.author.id,
            timeout_seconds=config["timeout_min"] * 60,
            truncated=outcome["truncated"],
            prompt_recent=method is MatchMethod.none_open,
        )
        if not outcome["candidates"]:
            await _safe_reply(
                message,
                "📥 Stored, but there are no charges to match it to — it is in the "
                "orphan queue and can be assigned from the web UI.",
            )
            return
        sent = await _safe_reply(message, "Which charge is this receipt for?", view=picker)
        picker.message = sent


async def _safe_reply(
    message: discord.Message, content: str, *, view: discord.ui.View | None = None
) -> discord.Message | None:
    try:
        return await message.reply(content, view=view, mention_author=False)
    except discord.HTTPException:
        logger.warning("Could not reply in channel %s", message.channel.id, exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

class DiscordService:
    """Owns the client's lifetime and exposes its health to the UI."""

    def __init__(self) -> None:
        self.client: ReceiptBot | None = None
        self._task: asyncio.Task[None] | None = None
        self._token: str | None = None
        self.last_error: str | None = None

    @property
    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_ready() and not self.client.is_closed()

    @property
    def connected_since(self) -> dt.datetime | None:
        return self.client.ready_at if self.client is not None else None

    async def start(self, token: str) -> None:
        if self._task is not None and not self._task.done() and self._token == token:
            return
        await self.stop()
        self._token = token
        self.client = ReceiptBot()
        self.last_error = None
        self._task = asyncio.create_task(self._run(token), name="discord-gateway")

    async def _run(self, token: str) -> None:
        assert self.client is not None
        try:
            # discord.py handles gateway reconnects internally; this only exits on
            # a fatal error or an explicit close.
            await self.client.start(token, reconnect=True)
        except asyncio.CancelledError:
            raise
        except discord.LoginFailure as exc:
            self.last_error = f"Discord rejected the bot token: {exc}"
            logger.error(self.last_error)
        except discord.PrivilegedIntentsRequired:
            self.last_error = (
                "The MESSAGE_CONTENT privileged intent is not enabled for this bot. "
                "Enable it in the Discord Developer Portal → Bot → Privileged Gateway Intents."
            )
            logger.error(self.last_error)
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("Discord gateway stopped")

    async def stop(self) -> None:
        if self.client is not None and not self.client.is_closed():
            await self.client.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.client = None
        self._task = None


_service: DiscordService | None = None


def get_service() -> DiscordService:
    global _service
    if _service is None:
        _service = DiscordService()
    return _service
