"""Shared test isolation.

The app caches three things at module scope for good reasons in production —
the config, the SQLAlchemy engine, and the Fernet instance — but each of them
would otherwise leak across tests and silently bind one test's state to another
test's data directory. Resetting all three before every test keeps each one
genuinely independent.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_module_caches():
    import app.config
    import app.db
    import app.security

    def reset() -> None:
        app.config.get_config.cache_clear()
        app.db._engine = None
        app.db._SessionFactory = None
        # Cached separately from the config, so clearing the config alone leaves a
        # Fernet bound to a previous test's secret.key.
        app.security._fernet = None

    reset()
    yield
    reset()
