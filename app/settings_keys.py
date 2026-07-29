"""Typed accessors for runtime settings stored in the `settings` table.

Anything the user can change without a restart lives here. Secrets are flagged so
:mod:`app.security` encrypts them at rest and the UI knows to render a masked
placeholder instead of the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.security import get_setting, set_setting

Kind = Literal["str", "int", "bool", "secret", "text"]


@dataclass(frozen=True, slots=True)
class Key:
    name: str
    kind: Kind
    default: str
    label: str
    help: str = ""

    @property
    def is_secret(self) -> bool:
        return self.kind == "secret"


# --- Microsoft Graph ------------------------------------------------------- #
GRAPH_TENANT_ID = Key("graph.tenant_id", "str", "", "Directory (tenant) ID")
GRAPH_CLIENT_ID = Key("graph.client_id", "str", "", "Application (client) ID")
GRAPH_CLIENT_SECRET = Key("graph.client_secret", "secret", "", "Client secret")
GRAPH_MAILBOX = Key(
    "graph.mailbox", "str", "", "Mailbox address",
    "The mailbox to monitor, e.g. alerts@example.com. Scope the app registration "
    "to this mailbox with an Exchange Application Access Policy.",
)
GRAPH_FOLDER_ID = Key("graph.folder_id", "str", "", "Mail folder ID")
GRAPH_FOLDER_NAME = Key("graph.folder_name", "str", "", "Mail folder")
GRAPH_POLL_SECONDS = Key("graph.poll_seconds", "int", "15", "Poll interval (seconds)")
GRAPH_DELTA_LINK = Key("graph.delta_link", "text", "", "Delta cursor")  # internal

# --- Envelope filters ------------------------------------------------------ #
FILTER_SENDER = Key(
    "filter.sender", "str", "", "Only process mail from",
    "Substring match on the sender address. Leave blank to accept any sender.",
)
FILTER_SUBJECT = Key(
    "filter.subject", "str", "", "Only process subjects containing",
    "Substring match on the subject. Leave blank to accept any subject.",
)

# --- Discord --------------------------------------------------------------- #
DISCORD_BOT_TOKEN = Key("discord.bot_token", "secret", "", "Bot token")
DISCORD_CHANNEL_ID = Key("discord.channel_id", "str", "", "Channel ID")
DISCORD_ALLOWED_UPLOADERS = Key(
    "discord.allowed_uploaders", "text", "", "Allowed uploader IDs",
    "Comma-separated Discord user IDs permitted to submit receipts. Leave blank "
    "to accept anyone who can see the channel — the right choice when channel "
    "access is already restricted, since the channel is then the control. Run "
    "/whoami in Discord to find an ID. Every upload records who sent it either "
    "way.",
)
DISCORD_DELETE_RECEIPT = Key(
    "discord.delete_receipt", "bool", "true", "Delete the receipt message",
    "After the file is durably stored and verified, delete the uploaded message.",
)
DISCORD_DELETE_NOTIFY = Key(
    "discord.delete_notify", "bool", "true", "Delete the notification message",
    "Also delete the bot's original charge notification once matched.",
)
DISCORD_KEEP_CONFIRMATION = Key(
    "discord.keep_confirmation", "bool", "true", "Keep the confirmation line",
    "Leave a one-line '✅ #1042 stored' message so the channel stays a readable log.",
)
DISCORD_SELECT_TIMEOUT_MIN = Key(
    "discord.select_timeout_min", "int", "15", "Receipt picker timeout (minutes)",
    "Must be 15 or less — Discord interaction tokens expire after 15 minutes.",
)

# --- Workflow -------------------------------------------------------------- #
LAPSE_HOURS = Key(
    "workflow.lapse_hours", "int", "24", "Lapse after (hours)",
    "A charge with no receipt after this long stops appearing in the picker and "
    "stops being nudged. It stays searchable and can still be matched by #code.",
)
DIGEST_HOUR = Key(
    "workflow.digest_hour", "int", "9", "Daily digest hour (local)",
    "The digest runs before the lapse sweep so it can warn what lapses tonight.",
)
DIGEST_ENABLED = Key("workflow.digest_enabled", "bool", "true", "Send a daily digest")
TIMEZONE = Key("workflow.timezone", "str", "UTC", "Business timezone")
DEFAULT_CURRENCY = Key("workflow.default_currency", "str", "USD", "Default currency")
HEARTBEAT_HOURS = Key(
    "workflow.heartbeat_hours", "int", "26", "Alert if no mail for (hours)",
    "Dead-man's switch. Silent ingest failure is the worst-case bug — if nothing "
    "has arrived in this long, something is probably broken.",
)

ALL_KEYS: tuple[Key, ...] = (
    GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_MAILBOX,
    GRAPH_FOLDER_ID, GRAPH_FOLDER_NAME, GRAPH_POLL_SECONDS,
    FILTER_SENDER, FILTER_SUBJECT,
    DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, DISCORD_ALLOWED_UPLOADERS,
    DISCORD_DELETE_RECEIPT, DISCORD_DELETE_NOTIFY, DISCORD_KEEP_CONFIRMATION,
    DISCORD_SELECT_TIMEOUT_MIN,
    LAPSE_HOURS, DIGEST_HOUR, DIGEST_ENABLED, TIMEZONE, DEFAULT_CURRENCY,
    HEARTBEAT_HOURS,
)

BY_NAME: dict[str, Key] = {k.name: k for k in ALL_KEYS}


def get_str(db: Session, key: Key) -> str:
    return get_setting(db, key.name, key.default) or key.default


def get_int(db: Session, key: Key) -> int:
    try:
        return int(get_str(db, key))
    except (TypeError, ValueError):
        return int(key.default)


def get_bool(db: Session, key: Key) -> bool:
    return get_str(db, key).strip().lower() in {"1", "true", "yes", "on"}


def put(db: Session, key: Key, value: str | None) -> None:
    set_setting(db, key.name, value, is_secret=key.is_secret)


def allowed_uploader_ids(db: Session) -> set[str]:
    raw = get_str(db, DISCORD_ALLOWED_UPLOADERS)
    return {part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()}


def is_configured_for_discord(db: Session) -> bool:
    return bool(get_str(db, DISCORD_BOT_TOKEN) and get_str(db, DISCORD_CHANNEL_ID))


def is_configured_for_graph(db: Session) -> bool:
    return all(
        get_str(db, k)
        for k in (GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_MAILBOX)
    )
