"""Application entrypoint.

Everything runs in **one process on one event loop**: the FastAPI web server, the
Discord gateway client, the Graph poller, the job worker, and the scheduler. That
is the whole reason for choosing an asyncio stack — the LXC deployment is a
single systemd unit with no broker, no worker pool, and no supervisor tree.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import settings_keys as sk
from app.config import get_config
from app.db import init_db, run_db, session_scope
from app.discordbot import get_service
from app.services import ingest, jobs, qbo_sync, scheduler  # noqa: F401 (registers handlers)
from app.web import (
    routes_account,
    routes_auth,
    routes_export,
    routes_qbo,
    routes_settings,
    routes_ui,
)

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    cfg = get_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # discord.py logs every gateway heartbeat at DEBUG; keep it at WARNING.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("msal").setLevel(logging.WARNING)


def _read_discord_token() -> str:
    with session_scope() as db:
        return sk.get_str(db, sk.DISCORD_BOT_TOKEN)


async def _discord_supervisor(stop: asyncio.Event) -> None:
    """Start the gateway once configured, and restart it if the token changes.

    Polls settings rather than requiring a restart, so the first-run wizard can
    bring the bot online without one.
    """
    service = get_service()
    current: str | None = None
    while not stop.is_set():
        try:
            token = await run_db(_read_discord_token)
            if token and token != current:
                logger.info("Starting Discord gateway")
                await service.start(token)
                current = token
            elif not token and current:
                await service.stop()
                current = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Discord supervisor error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except TimeoutError:
            pass
    await service.stop()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the background workers, and stop them cleanly.

    The stop Event and the task list are created *here*, per application, rather
    than at module scope. An ``asyncio.Event`` binds to the loop that first awaits
    it, so module-level state would be shared between two apps in one process —
    under ``uvicorn --reload``, or in tests — and the second one's workers would
    fail with "bound to a different event loop".
    """
    _configure_logging()
    cfg = get_config()
    cfg.ensure_dirs()
    init_db()
    logger.info("ReceiptManager starting — data dir %s", cfg.data_dir)

    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(jobs.work_forever(stop), name="jobs"),
        asyncio.create_task(ingest.poll_forever(stop), name="mail-poller"),
        asyncio.create_task(scheduler.run_forever(stop), name="scheduler"),
        asyncio.create_task(_discord_supervisor(stop), name="discord"),
    ]
    app.state.stop_event = stop
    app.state.background_tasks = tasks

    try:
        yield
    finally:
        logger.info("ReceiptManager shutting down")
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def create_app() -> FastAPI:
    app = FastAPI(title="ReceiptManager", lifespan=lifespan, docs_url=None, redoc_url=None)

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Receipt files are NOT mounted as static — they are served through an
    # authenticated route in routes_ui so login actually protects them.
    app.include_router(routes_auth.router)
    app.include_router(routes_ui.router)
    app.include_router(routes_settings.router)
    app.include_router(routes_export.router)
    app.include_router(routes_account.router)
    app.include_router(routes_qbo.router)
    return app


app = create_app()


def run() -> None:
    """Console-script entrypoint (``receiptmanager``)."""
    import uvicorn

    cfg = get_config()
    uvicorn.run(
        "app.main:app",
        host=cfg.host,
        port=cfg.port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips=cfg.forwarded_allow_ips,
    )


if __name__ == "__main__":
    run()
