"""Daily scheduling: digest, lapse sweep, heartbeat.

Ordering matters and is deliberate: **the digest runs before the lapse sweep.**
That is what makes the digest's "lapsing soon" warning actionable — it is the
last chance to attach a receipt before the charge drops out of implicit matching.
Sweeping first would silently age out the very charges the digest exists to
rescue.

Times are evaluated in the configured business timezone, and each task records
the local date it last ran so a restart cannot double-fire or skip a day.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from app import settings_keys as sk
from app.db import run_db, session_scope
from app.formatting import get_zone
from app.models import utcnow
from app.security import get_setting, set_setting
from app.services import jobs
from app.services.ingest import LAST_MAIL_KEY

logger = logging.getLogger(__name__)

LAST_DIGEST_KEY = "runtime.last_digest_date"
LAST_HEARTBEAT_KEY = "runtime.last_heartbeat_alert"

_TICK_SECONDS = 60


def _due_tasks() -> list[str]:
    """Return job kinds that should be enqueued right now."""
    due: list[str] = []
    with session_scope() as db:
        tz = get_zone(sk.get_str(db, sk.TIMEZONE))
        now_local = utcnow().astimezone(tz)
        today = now_local.date().isoformat()

        if now_local.hour >= sk.get_int(db, sk.DIGEST_HOUR):
            if get_setting(db, LAST_DIGEST_KEY, "") != today:
                set_setting(db, LAST_DIGEST_KEY, today)
                # Digest first, sweep second — see the module docstring.
                if sk.get_bool(db, sk.DIGEST_ENABLED):
                    due.append("discord.digest")
                due.append("workflow.lapse_sweep")
                # A chart of accounts changes rarely; daily is ample.
                if sk.is_connected_to_qbo(db):
                    due.append("qbo.sync_accounts")

    return due


def _enqueue(kinds: list[str]) -> None:
    if not kinds:
        return
    stamp = utcnow().strftime("%Y%m%d")
    with session_scope() as db:
        for index, kind in enumerate(kinds):
            jobs.enqueue(
                db,
                kind=kind,
                payload={},
                idempotency_key=f"{kind}:{stamp}",
                # Stagger so the sweep cannot beat the digest through the queue.
                delay_seconds=index * 20,
            )


def _heartbeat_alert() -> str | None:
    """Warn if ingest has gone quiet.

    Silent ingest failure is this system's worst-case bug: charges stop being
    captured and nothing looks wrong until the receipts are already lost.
    """
    with session_scope() as db:
        hours = sk.get_int(db, sk.HEARTBEAT_HOURS)
        if hours <= 0 or not sk.is_configured_for_graph(db):
            return None

        raw = get_setting(db, LAST_MAIL_KEY, "")
        if not raw:
            return None
        try:
            last_mail = dt.datetime.fromisoformat(raw)
        except ValueError:
            return None
        if last_mail.tzinfo is None:
            last_mail = last_mail.replace(tzinfo=dt.UTC)

        quiet_for = utcnow() - last_mail
        if quiet_for < dt.timedelta(hours=hours):
            return None

        today = utcnow().date().isoformat()
        if get_setting(db, LAST_HEARTBEAT_KEY, "") == today:
            return None  # one alert per day is enough
        set_setting(db, LAST_HEARTBEAT_KEY, today)

        return (
            f"🔴 **Ingest may be broken.** No email has been received in "
            f"{int(quiet_for.total_seconds() // 3600)} hours "
            f"(threshold {hours}h). Check Settings → Health — charges may not be "
            f"getting captured."
        )


def _enqueue_heartbeat(message: str) -> None:
    with session_scope() as db:
        jobs.enqueue(
            db,
            kind="discord.alert",
            payload={"content": message},
            idempotency_key=f"heartbeat:{utcnow().date().isoformat()}",
        )


async def run_forever(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            due = await run_db(_due_tasks)
            if due:
                await run_db(_enqueue, due)
                logger.info("Scheduled: %s", ", ".join(due))

            if alert := await run_db(_heartbeat_alert):
                await run_db(_enqueue_heartbeat, alert)
                logger.error("Heartbeat alert raised")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed")

        try:
            await asyncio.wait_for(stop.wait(), timeout=_TICK_SECONDS)
        except TimeoutError:
            pass
