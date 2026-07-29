"""Durable job queue (transactional outbox).

Every side effect — posting to Discord, capturing a receipt, deleting a message,
sending the digest — is enqueued as a row *in the same transaction* as the state
change that caused it. That is what makes the system crash-safe: if the process
dies between "transaction created" and "notification sent", the job is still
sitting in the table when it comes back up.

Handlers must be idempotent. A job that succeeded but crashed before being marked
done will run again.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import run_db, session_scope
from app.models import Job, JobStatus, utcnow

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]
_handlers: dict[str, Handler] = {}

# Exponential, but capped — a permanently broken Discord token should retry every
# ~10 minutes, not every 8 hours.
_MAX_BACKOFF_SECONDS = 600


def handler(kind: str) -> Callable[[Handler], Handler]:
    def decorate(fn: Handler) -> Handler:
        _handlers[kind] = fn
        return fn

    return decorate


def enqueue(
    db: Session,
    *,
    kind: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    delay_seconds: float = 0.0,
    max_attempts: int = 8,
) -> Job | None:
    """Queue a job. Returns None if ``idempotency_key`` is already queued.

    Uses a SAVEPOINT so a duplicate key does not poison the caller's transaction
    (which usually also holds the state change that produced this job).
    """
    job = Job(
        kind=kind,
        payload=payload or {},
        idempotency_key=idempotency_key,
        next_run_at=utcnow() + dt.timedelta(seconds=delay_seconds),
        max_attempts=max_attempts,
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        logger.debug("Job %s already queued (%s)", kind, idempotency_key)
        return None
    return job


def reset_orphaned_jobs(db: Session) -> int:
    """Return jobs left ``running`` by a crash to the pending pool.

    Safe because handlers are required to be idempotent.
    """
    result = db.execute(
        update(Job)
        .where(Job.status == JobStatus.running)
        .values(status=JobStatus.pending, next_run_at=utcnow())
    )
    count = int(result.rowcount or 0)
    if count:
        logger.warning("Recovered %d job(s) left running by a previous crash", count)
    return count


def _claim(limit: int) -> list[tuple[int, str, dict[str, Any]]]:
    with session_scope() as db:
        rows = list(
            db.scalars(
                select(Job)
                .where(Job.status == JobStatus.pending, Job.next_run_at <= utcnow())
                .order_by(Job.next_run_at, Job.id)
                .limit(limit)
            )
        )
        claimed = []
        for job in rows:
            job.status = JobStatus.running
            job.attempts += 1
            db.add(job)
            claimed.append((job.id, job.kind, dict(job.payload or {})))
        return claimed


def _finish(job_id: int, error: str | None) -> None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        if error is None:
            job.status = JobStatus.done
            job.completed_at = utcnow()
            job.last_error = None
        else:
            job.last_error = error[:4000]
            if job.attempts >= job.max_attempts:
                job.status = JobStatus.dead
                logger.error(
                    "Job %s (%s) dead after %d attempts: %s",
                    job.id, job.kind, job.attempts, error.splitlines()[0] if error else "",
                )
            else:
                backoff = min(2 ** job.attempts, _MAX_BACKOFF_SECONDS)
                job.status = JobStatus.pending
                job.next_run_at = utcnow() + dt.timedelta(seconds=backoff)
        db.add(job)


async def _run_one(job_id: int, kind: str, payload: dict[str, Any]) -> None:
    fn = _handlers.get(kind)
    if fn is None:
        await run_db(_finish, job_id, f"No handler registered for job kind {kind!r}")
        return
    try:
        await fn(payload)
    except asyncio.CancelledError:
        # Shutting down — leave it pending so it runs again next boot.
        await run_db(_finish, job_id, "Cancelled during shutdown")
        raise
    except Exception:
        await run_db(_finish, job_id, traceback.format_exc())
        return
    await run_db(_finish, job_id, None)


async def work_forever(stop: asyncio.Event, *, batch: int = 5, idle_sleep: float = 2.0) -> None:
    """Drain the queue until ``stop`` is set."""
    await run_db(lambda: _with_session(reset_orphaned_jobs))

    while not stop.is_set():
        try:
            claimed = await run_db(_claim, batch)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to claim jobs")
            claimed = []

        if not claimed:
            try:
                await asyncio.wait_for(stop.wait(), timeout=idle_sleep)
            except TimeoutError:
                pass
            continue

        # Jobs in a batch are independent; run them concurrently.
        await asyncio.gather(
            *(_run_one(job_id, kind, payload) for job_id, kind, payload in claimed),
            return_exceptions=True,
        )


def _with_session(fn: Callable[[Session], Any]) -> Any:
    with session_scope() as db:
        return fn(db)


def queue_stats(db: Session) -> dict[str, int]:
    """Counts for the health page."""
    return {
        status.value: int(
            db.scalar(select(func.count(Job.id)).where(Job.status == status)) or 0
        )
        for status in JobStatus
    }
