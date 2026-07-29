"""Slash commands.

Everything here replies **ephemerally** — only the invoker sees it — so querying
state does not clutter the channel that also serves as the receipt log.

The uploader allowlist is enforced on every mutating command. A slash command is
just as capable of altering a financial record as an upload is, so it gets the
same gate.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from sqlalchemy import or_, select

from app import settings_keys as sk
from app.db import run_db, session_scope
from app.formatting import money, when
from app.models import AuditLog, Transaction, TransactionStatus
from app.services import matching

logger = logging.getLogger(__name__)

MAX_LINES = 25


def _authorized(user_id: int) -> bool:
    with session_scope() as db:
        allowed = sk.allowed_uploader_ids(db)
    return not allowed or str(user_id) in allowed


def _tz() -> str:
    with session_scope() as db:
        return sk.get_str(db, sk.TIMEZONE)


def _render(tx: Transaction, tz: str) -> str:
    return (
        f"`#{tx.short_code}` · {money(tx.amount_minor, tx.currency)} · "
        f"{tx.merchant} · {when(tx.occurred_at, tz)}"
    )


# --------------------------------------------------------------------------- #
# Sync workers (always via run_db)
# --------------------------------------------------------------------------- #

def _list_pending() -> str:
    with session_scope() as db:
        tz = sk.get_str(db, sk.TIMEZONE)
        rows = matching.open_transactions(db, limit=MAX_LINES)
        if not rows:
            return "✅ Nothing is awaiting a receipt."
        lines = [f"**{len(rows)} awaiting a receipt**", ""]
        lines += [_render(tx, tz) for tx in rows]
        lines.append("")
        lines.append("_Reply to a charge's message with the receipt, or upload with_ `#code`.")
        return "\n".join(lines)


def _mutate(code: str, actor: str, **changes: Any) -> str:
    """Apply a field change to one charge. Returns the reply text."""
    with session_scope() as db:
        tz = sk.get_str(db, sk.TIMEZONE)
        tx = db.scalar(select(Transaction).where(Transaction.short_code == code.lstrip("#")))
        if tx is None:
            return f"⚠️ No charge `#{code.lstrip('#')}`."

        applied = []
        if (status := changes.get("status")) is not None:
            tx.status = status
            applied.append(f"status → {status.value}")
        if (note := changes.get("note")) is not None:
            tx.notes = f"{tx.notes}\n{note}" if tx.notes else note
            applied.append("note added")
        if (category := changes.get("category")) is not None:
            tx.category = category
            applied.append(f"category → {category}")

        db.add(tx)
        db.add(
            AuditLog(actor=f"discord:{actor}", action="transaction.updated",
                     entity="transaction", entity_id=tx.short_code,
                     detail="; ".join(applied))
        )
        return f"✅ {_render(tx, tz)}\n{'; '.join(applied)}"


def _search(query: str) -> str:
    with session_scope() as db:
        tz = sk.get_str(db, sk.TIMEZONE)
        like = f"%{query.strip()}%"
        conditions = [
            Transaction.merchant.ilike(like),
            Transaction.short_code.ilike(like),
            Transaction.notes.ilike(like),
            Transaction.category.ilike(like),
        ]
        try:
            conditions.append(
                Transaction.amount_minor == int(round(float(query.replace("$", "").strip()) * 100))
            )
        except ValueError:
            pass
        rows = list(
            db.scalars(
                select(Transaction)
                .where(or_(*conditions))
                .order_by(Transaction.occurred_at.desc())
                .limit(MAX_LINES)
            )
        )
        if not rows:
            return f"No charges match `{query}`."
        return "\n".join([f"**{len(rows)} match(es)**", ""] + [_render(tx, tz) for tx in rows])


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

def register(tree: app_commands.CommandTree) -> None:
    """Attach every command to the bot's tree. Called once, before syncing."""

    async def guard(interaction: discord.Interaction) -> bool:
        if await run_db(_authorized, interaction.user.id):
            return True
        await interaction.response.send_message(
            "⛔ You are not on the authorized uploader list.", ephemeral=True
        )
        return False

    @tree.command(
        name="whoami",
        description="Show your Discord user ID and this channel's ID, for Settings",
    )
    async def whoami(interaction: discord.Interaction) -> None:
        """Setup helper.

        Discord keeps relocating Developer Mode, and copying IDs is the one step
        of setup that cannot be done from inside this app. Deliberately not
        allowlist-gated: it reveals nothing the caller does not already know
        about themselves, and it is what you run *before* the allowlist exists.
        """
        await interaction.response.send_message(
            f"**Your user ID:** `{interaction.user.id}`\n"
            f"**This channel's ID:** `{interaction.channel_id}`\n\n"
            "Paste the user ID into _Settings → Discord → Allowed uploader IDs_ "
            "(comma-separated for several people), and the channel ID into "
            "_Channel ID_.",
            ephemeral=True,
        )

    @tree.command(name="pending", description="List charges still awaiting a receipt")
    async def pending(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await run_db(_list_pending), ephemeral=True)

    @tree.command(name="skip", description="Mark a charge as not needing a receipt")
    @app_commands.describe(code="The charge code, e.g. 1042")
    async def skip(interaction: discord.Interaction, code: str) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await run_db(
            _mutate, code, str(interaction.user), status=TransactionStatus.no_receipt_required
        )
        await interaction.followup.send(text, ephemeral=True)

    @tree.command(name="note", description="Append a note to a charge")
    @app_commands.describe(code="The charge code, e.g. 1042", text="Note to add")
    async def note(interaction: discord.Interaction, code: str, text: str) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        reply = await run_db(_mutate, code, str(interaction.user), note=text)
        await interaction.followup.send(reply, ephemeral=True)

    @tree.command(name="cat", description="Set a charge's accounting category")
    @app_commands.describe(code="The charge code, e.g. 1042", category="Category name")
    async def cat(interaction: discord.Interaction, code: str, category: str) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        reply = await run_db(_mutate, code, str(interaction.user), category=category)
        await interaction.followup.send(reply, ephemeral=True)

    @tree.command(name="search", description="Find charges by merchant, code, amount or note")
    @app_commands.describe(query="Merchant, #code, amount, or note text")
    async def search(interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await run_db(_search, query), ephemeral=True)
