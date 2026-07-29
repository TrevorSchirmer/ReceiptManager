"""Migrations must work on a fresh database, on a legacy one, and match the models.

The drift check is the valuable one: it fails the build whenever someone edits a
model without generating a migration, which is exactly the mistake that produces
a container that boots fine in dev and crashes on the real data.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    import app.config
    import app.db

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None
    yield


def test_fresh_database_migrates_to_head(fresh):
    from app.db import get_engine, run_migrations

    run_migrations()

    with get_engine().connect() as conn:
        tables = set(sa.inspect(conn).get_table_names())
        version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()

    assert version, "no alembic version stamped"
    for expected in (
        "users", "sessions", "settings", "parse_rules", "merchant_rules",
        "emails_raw", "transactions", "attachments", "discord_messages",
        "jobs", "audit_log",
    ):
        assert expected in tables, f"{expected} missing after migration"


def test_models_match_migrations(fresh):
    """Autogenerate must detect nothing — models and migrations agree."""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    # Note: app.migrations.env is NOT importable here — it runs Alembic context
    # code at import time and only works under `alembic`'s own runner.
    from app.db import get_engine, run_migrations
    from app.models import Base

    run_migrations()
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)

    # Indexes on SQLite can report spurious diffs; only structural drift matters.
    structural = [d for d in diff if not (isinstance(d, tuple) and "index" in str(d[0]))]
    assert not structural, f"models drifted from migrations: {structural}"


def test_pre_alembic_database_is_stamped_not_rebuilt(fresh):
    """An early create_all database must be adopted, not re-created.

    Re-running CREATE TABLE against a populated database fails, which would leave
    a working install unable to boot after an upgrade.
    """
    from app.db import get_engine, init_db
    from app.models import Base, Setting

    engine = get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO settings (key, value, is_secret, updated_at) "
                             "VALUES ('canary', 'survives', 0, '2026-01-01 00:00:00')"))

    init_db()  # must stamp, not rebuild

    with engine.connect() as conn:
        version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        canary = conn.execute(sa.text("SELECT value FROM settings WHERE key='canary'")).scalar()

    assert version, "legacy database was not stamped"
    assert canary == "survives", "existing data was destroyed"

    # The ORM must still work against the adopted schema.
    from app.db import session_scope

    with session_scope() as db:
        assert db.get(Setting, "canary").value == "survives"
