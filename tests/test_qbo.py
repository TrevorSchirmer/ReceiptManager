"""QuickBooks phase 1: OAuth plumbing, token handling, and the account cache.

The two tests that matter most here guard silent failures:

* a rotated refresh token that is not persisted kills the connection at the next
  refresh, with nothing to indicate why;
* an account deleted in QuickBooks must not take a historical categorisation
  with it.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    import app.config
    import app.db
    import app.security

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None
    app.security._fernet = None
    from app.db import init_db

    init_db()
    yield


@pytest.fixture()
def client(env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        c.post("/setup", data={"password": PASSWORD, "password_confirm": PASSWORD},
               follow_redirects=False)
        yield c


def _csrf(c: TestClient, path: str = "/") -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(path).text).group(1)


def _configure(db):
    from app import settings_keys as sk

    sk.put(db, sk.QBO_CLIENT_ID, "client-abc")
    sk.put(db, sk.QBO_CLIENT_SECRET, "secret-xyz")
    sk.put(db, sk.QBO_REDIRECT_URI, "https://receipts.example.com/qbo/callback")


# --------------------------------------------------------------------------- #
# OAuth plumbing
# --------------------------------------------------------------------------- #

def test_authorize_url_carries_everything_intuit_needs(env):
    from urllib.parse import parse_qs, urlparse

    from app.db import session_scope
    from app.services import qbo

    with session_scope() as db:
        _configure(db)
        url = qbo.build_authorize_url(db, state="state-123")

    parts = urlparse(url)
    query = parse_qs(parts.query)
    assert parts.netloc == "appcenter.intuit.com"
    assert query["client_id"] == ["client-abc"]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["state-123"]
    assert query["redirect_uri"] == ["https://receipts.example.com/qbo/callback"]
    assert "accounting" in query["scope"][0]


def test_rotated_refresh_token_replaces_the_old_one(env):
    """Intuit invalidates the previous refresh token on every use.

    Failing to persist the replacement disconnects the integration at the next
    refresh, with no error until then.
    """
    from app import settings_keys as sk
    from app.db import session_scope
    from app.services import qbo

    first = qbo._token_set_from({
        "access_token": "at-1", "refresh_token": "rt-1",
        "expires_in": 3600, "x_refresh_token_expires_in": 8726400,
    })
    with session_scope() as db:
        qbo.store_tokens(db, first)
    with session_scope() as db:
        assert sk.get_str(db, sk.QBO_REFRESH_TOKEN) == "rt-1"

    second = qbo._token_set_from({
        "access_token": "at-2", "refresh_token": "rt-2",
        "expires_in": 3600, "x_refresh_token_expires_in": 8726400,
    })
    with session_scope() as db:
        qbo.store_tokens(db, second)
    with session_scope() as db:
        assert sk.get_str(db, sk.QBO_REFRESH_TOKEN) == "rt-2"
        assert sk.get_str(db, sk.QBO_ACCESS_TOKEN) == "at-2"


def test_tokens_are_encrypted_at_rest(env):
    import sqlite3

    from app.config import get_config
    from app.db import session_scope
    from app.services import qbo

    tokens = qbo._token_set_from({
        "access_token": "at-secret", "refresh_token": "rt-secret",
        "expires_in": 3600, "x_refresh_token_expires_in": 8726400,
    })
    with session_scope() as db:
        qbo.store_tokens(db, tokens)

    rows = sqlite3.connect(get_config().db_path).execute(
        "SELECT key, value FROM settings WHERE key LIKE 'qbo.%token'"
    ).fetchall()
    assert rows
    for key, value in rows:
        assert "secret" not in value, f"{key} stored in plaintext"


def test_disconnect_clears_every_token(env):
    from app import settings_keys as sk
    from app.db import session_scope
    from app.services import qbo

    with session_scope() as db:
        _configure(db)
        qbo.store_tokens(db, qbo._token_set_from({
            "access_token": "at", "refresh_token": "rt",
            "expires_in": 3600, "x_refresh_token_expires_in": 8726400,
        }))
        sk.put(db, sk.QBO_REALM_ID, "123456")

    with session_scope() as db:
        assert sk.is_connected_to_qbo(db)
        qbo.clear_tokens(db)

    with session_scope() as db:
        assert not sk.is_connected_to_qbo(db)
        for key in (sk.QBO_ACCESS_TOKEN, sk.QBO_REFRESH_TOKEN, sk.QBO_REALM_ID):
            assert sk.get_str(db, key) == ""
        # Credentials survive, so reconnecting does not mean re-entering them.
        assert sk.get_str(db, sk.QBO_CLIENT_ID) == "client-abc"


# --------------------------------------------------------------------------- #
# The callback
# --------------------------------------------------------------------------- #

def test_callback_rejects_a_mismatched_state(client):
    from app.db import session_scope

    with session_scope() as db:
        _configure(db)

    client.get("/qbo/connect", follow_redirects=False)
    r = client.get(
        "/qbo/callback?code=abc&state=not-the-issued-one&realmId=123",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "state+mismatch" in r.headers["location"].replace("%20", "+")


def test_callback_state_is_single_use(client):
    """A replayed callback must not be accepted a second time."""
    from app.db import session_scope
    from app.security import get_setting
    from app.web.routes_qbo import STATE_KEY

    with session_scope() as db:
        _configure(db)

    client.get("/qbo/connect", follow_redirects=False)
    with session_scope() as db:
        state = get_setting(db, STATE_KEY)
    assert state

    # Exchange will fail (no real Intuit), but the state must be consumed anyway.
    client.get(f"/qbo/callback?code=abc&state={state}&realmId=123", follow_redirects=False)
    with session_scope() as db:
        assert not get_setting(db, STATE_KEY), "state survived the first callback"

    r = client.get(f"/qbo/callback?code=abc&state={state}&realmId=123", follow_redirects=False)
    assert "state+mismatch" in r.headers["location"].replace("%20", "+")


def test_connect_refuses_before_credentials_are_entered(client):
    r = client.get("/qbo/connect", follow_redirects=False)
    assert r.status_code == 303
    assert "/settings" in r.headers["location"]
    assert "client+ID" in r.headers["location"].replace("%20", "+")


# --------------------------------------------------------------------------- #
# Account cache
# --------------------------------------------------------------------------- #

ACCOUNT_ROWS = [
    {"Id": "60", "Name": "Meals", "FullyQualifiedName": "Travel:Meals",
     "AccountType": "Expense", "AccountSubType": "Entertainment", "Active": True},
    {"Id": "61", "Name": "Software", "FullyQualifiedName": "Software",
     "AccountType": "Expense", "Active": True},
]


def test_accounts_upsert_and_are_offered_in_order(env):
    from app.db import session_scope
    from app.services.qbo_sync import active_accounts, upsert_accounts

    with session_scope() as db:
        added, updated, deactivated = upsert_accounts(db, ACCOUNT_ROWS)
        assert (added, updated, deactivated) == (2, 0, 0)

    with session_scope() as db:
        labels = [a.label for a in active_accounts(db)]
    assert labels == ["Software", "Travel:Meals"]

    # A second sync of the same data is a no-op, not a duplicate.
    with session_scope() as db:
        added, _updated, _deactivated = upsert_accounts(db, ACCOUNT_ROWS)
        assert added == 0
    with session_scope() as db:
        assert len(active_accounts(db)) == 2


def test_a_removed_account_is_deactivated_not_deleted(env):
    """Deleting it would strip the category from transactions already filed."""
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import QboAccount, Transaction, utcnow
    from app.services.qbo_sync import active_accounts, upsert_accounts

    with session_scope() as db:
        upsert_accounts(db, ACCOUNT_ROWS)
    with session_scope() as db:
        meals = db.scalar(select(QboAccount).where(QboAccount.qbo_id == "60"))
        db.add(Transaction(
            short_code="1000", occurred_at=utcnow(), merchant="CAFE",
            amount_minor=1234, currency="USD",
            category_account_id=meals.id, category=meals.label,
        ))

    # QuickBooks stops returning "Meals".
    with session_scope() as db:
        _added, _updated, deactivated = upsert_accounts(db, ACCOUNT_ROWS[1:])
        assert deactivated == 1

    with session_scope() as db:
        assert [a.label for a in active_accounts(db)] == ["Software"]
        # The row survives, and the transaction still points at it.
        assert db.scalar(select(func.count(QboAccount.id))) == 2
        tx = db.scalar(select(Transaction))
        assert tx.category_account_id is not None
        assert tx.category == "Travel:Meals"


def test_category_picker_appears_only_when_accounts_exist(client):
    from app.db import session_scope
    from app.models import Transaction, utcnow
    from app.services.qbo_sync import upsert_accounts

    with session_scope() as db:
        db.add(Transaction(short_code="1000", occurred_at=utcnow(), merchant="CAFE",
                           amount_minor=1234, currency="USD"))

    page = client.get("/transactions/1000").text
    assert 'name="category"' in page, "free-text category should be the fallback"
    assert 'name="category_account_id"' not in page

    with session_scope() as db:
        upsert_accounts(db, ACCOUNT_ROWS)

    page = client.get("/transactions/1000").text
    assert 'name="category_account_id"' in page
    assert 'name="category_mode"' in page
    assert "Travel:Meals" in page


def test_choosing_an_account_also_fills_the_text_category(client):
    """The CSV export and search read the text field, so it must stay in step."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import QboAccount, Transaction, utcnow
    from app.services.qbo_sync import upsert_accounts

    with session_scope() as db:
        db.add(Transaction(short_code="1000", occurred_at=utcnow(), merchant="CAFE",
                           amount_minor=1234, currency="USD"))
        upsert_accounts(db, ACCOUNT_ROWS)
    with session_scope() as db:
        meals_id = db.scalar(select(QboAccount.id).where(QboAccount.qbo_id == "60"))

    client.post(
        "/transactions/1000/update",
        data={
            "csrf_token": _csrf(client), "merchant": "CAFE", "amount": "12.34",
            "category_mode": "qbo", "category_account_id": str(meals_id),
            "status": "new", "notes": "",
        },
        follow_redirects=False,
    )

    with session_scope() as db:
        tx = db.scalar(select(Transaction))
        assert tx.category_account_id == meals_id
        assert tx.category == "Travel:Meals"

    # And clearing it clears both.
    client.post(
        "/transactions/1000/update",
        data={
            "csrf_token": _csrf(client), "merchant": "CAFE", "amount": "12.34",
            "category_mode": "qbo", "category_account_id": "",
            "status": "new", "notes": "",
        },
        follow_redirects=False,
    )
    with session_scope() as db:
        tx = db.scalar(select(Transaction))
        assert tx.category_account_id is None
        assert tx.category is None


def test_health_warns_before_the_authorisation_lapses(env):
    """~100 days of non-use kills the connection with no other outward sign."""
    from app import settings_keys as sk
    from app.db import session_scope
    from app.models import utcnow
    from app.web.deps import health_snapshot

    with session_scope() as db:
        _configure(db)
        sk.put(db, sk.QBO_REALM_ID, "123456")
        sk.put(db, sk.QBO_REFRESH_TOKEN, "rt")
        sk.put(db, sk.QBO_REFRESH_EXPIRES_AT, (utcnow() + dt.timedelta(days=60)).isoformat())

    with session_scope() as db:
        health = health_snapshot(db)
        assert health["qbo_connected"] is True
        assert health["qbo_expiring"] is False
        assert 59 <= health["qbo_days_left"] <= 60

    with session_scope() as db:
        sk.put(db, sk.QBO_REFRESH_EXPIRES_AT, (utcnow() + dt.timedelta(days=5)).isoformat())
    with session_scope() as db:
        health = health_snapshot(db)
        assert health["qbo_expiring"] is True
        assert health["ok"] is False, "an expiring authorisation must fail the health check"


def test_oauth_tokens_are_not_editable_from_the_settings_form(client):
    """A hand-edited token would break the connection in a confusing way."""
    from app import settings_keys as sk

    page = client.get("/settings").text
    for key in (sk.QBO_REFRESH_TOKEN, sk.QBO_ACCESS_TOKEN, sk.QBO_REALM_ID):
        assert f'name="{key.name}"' not in page, f"{key.name} is editable"
    # The credentials the user does own are present.
    assert f'name="{sk.QBO_CLIENT_ID.name}"' in page
    assert f'name="{sk.QBO_CLIENT_SECRET.name}"' in page
