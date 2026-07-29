"""Database engine and session helpers.

We use *synchronous* SQLAlchemy against SQLite. For a single-tenant app the queries
are microseconds, and the sync API is far simpler than the async one. The one real
hazard is blocking the event loop long enough to stall the Discord gateway
heartbeat, so anything potentially slow (exports, reparse sweeps, digests) must go
through :func:`run_db`, which hands the work to a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

import sqlalchemy as sa
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_config
from app.models import Base

if TYPE_CHECKING:
    from alembic.config import Config

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    # WAL lets the web UI read while the poller and job worker write.
    cur.execute("PRAGMA journal_mode=WAL")
    # NORMAL is safe under WAL and much faster than FULL.
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    # Wait rather than immediately raising "database is locked".
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        cfg = get_config()
        _engine = create_engine(
            cfg.db_url,
            future=True,
            # Sessions are short-lived and thread-confined, but run_db() hands them
            # to worker threads, so the default same-thread check must go.
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
        event.listen(_engine, "connect", _configure_sqlite)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def _alembic_config() -> "Config":
    from alembic.config import Config

    migrations_dir = Path(__file__).resolve().parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", get_config().db_url)
    return cfg


def run_migrations() -> None:
    """Bring the schema to head, tolerating a database created before Alembic.

    Early builds created tables with ``create_all``. Those databases have every
    table but no ``alembic_version`` row, so a plain ``upgrade head`` would try to
    re-create existing tables and fail. Detect that case and *stamp* instead —
    the schema already matches the initial revision.
    """
    from alembic import command
    from alembic.runtime.migration import MigrationContext

    engine = get_engine()
    with engine.connect() as conn:
        existing = set(sa.inspect(conn).get_table_names())
        current = MigrationContext.configure(conn).get_current_revision()

    cfg = _alembic_config()
    pre_alembic = existing - {"alembic_version"}

    if current is None and pre_alembic:
        logger.warning(
            "Database has %d table(s) but no Alembic version — stamping head "
            "(pre-Alembic schema created by create_all)",
            len(pre_alembic),
        )
        command.stamp(cfg, "head")
        return

    command.upgrade(cfg, "head")


def init_db() -> None:
    """Prepare the schema.

    Migrations are authoritative. If they cannot run at all (a corrupt or
    partially-migrated database), fall back to ``create_all`` rather than
    refusing to boot — a running app with a health page beats a silent service
    that never starts.
    """
    engine = get_engine()
    try:
        run_migrations()
    except Exception:
        logger.exception("Alembic migration failed; falling back to create_all")
        Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    get_engine()
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def run_db(fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run a blocking DB callable off the event loop.

    Use this from async code (Discord handlers, the poller, route handlers doing
    heavy work) so a slow query cannot stall the gateway heartbeat.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)
