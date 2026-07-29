"""End-to-end smoke test: setup -> login -> simulate email -> capture -> match.

Deliberately exercises the real pipeline rather than mocking it, because the
parts most worth testing here are the ordering guarantees (capture before
delete) and the matching rules, and neither survives being mocked.
"""

from __future__ import annotations

import datetime as dt
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    # Config and engine are cached per-process; drop them so each test is isolated.
    import app.config
    import app.db

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None

    from app.db import init_db
    from app.main import create_app

    init_db()
    with TestClient(create_app()) as c:
        yield c


def _setup(client: TestClient) -> None:
    r = client.post(
        "/setup",
        data={"password": PASSWORD, "password_confirm": PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


def test_setup_then_login_flow(client):
    # Before setup, everything bounces to /setup.
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/setup"

    _setup(client)
    assert client.get("/").status_code == 200

    # A second setup attempt must not create another admin.
    r = client.post(
        "/setup",
        data={"password": "another-password-here", "password_confirm": "another-password-here"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_short_password_rejected(client):
    r = client.post("/setup", data={"password": "short", "password_confirm": "short"})
    assert r.status_code == 400
    assert "at least 12 characters" in r.text.lower()


def test_wrong_password_rejected(client):
    _setup(client)
    client.post("/logout", data={"csrf_token": _csrf(client)}, follow_redirects=False)
    r = client.post("/login", data={"password": "wrong-password-entirely"})
    assert r.status_code == 401
    # Deliberately does not distinguish a bad username from a bad password.
    assert "incorrect username or password" in r.text.lower()


def _csrf(client: TestClient) -> str:
    """Pull the CSRF token out of any rendered page."""
    import re

    html = client.get("/").text
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token rendered"
    return match.group(1)


def test_parse_and_simulate_creates_transaction(client):
    _setup(client)
    token = _csrf(client)

    # A rule matching a typical card alert.
    r = client.post(
        "/rules",
        data={
            "csrf_token": token,
            "name": "Test alerts",
            "enabled": "on",
            "priority": "100",
            "sender_match": "alerts@",
            "subject_match": "",
            "body_regex": (
                r"charge of (?P<amount>[$\d,.]+)\s+was made at (?P<merchant>.+?)\s+"
                r"on your card ending (?P<card_ending>\d{4})"
            ),
            "default_currency": "USD",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    r = client.post(
        "/settings/simulate",
        data={
            "csrf_token": token,
            "sender": "alerts@example.com",
            "subject": "Card transaction alert",
            "body": "A charge of $43.21 was made at AMAZON MARKETPLACE on your card ending 4417.",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Transaction

    with session_scope() as db:
        tx = db.scalar(select(Transaction))
        assert tx is not None
        assert tx.merchant == "AMAZON MARKETPLACE"
        assert tx.amount_minor == 4321   # integer cents, never a float
        assert tx.currency == "USD"
        assert tx.card_ending == "4417"

    # It should be visible and searchable in the UI.
    assert "AMAZON MARKETPLACE" in client.get("/transactions").text
    assert "AMAZON MARKETPLACE" in client.get("/transactions?q=amazon").text
    assert "AMAZON" not in client.get("/transactions?q=zzzznope").text


def test_unparsed_email_still_creates_a_transaction(client):
    """A parse failure must never lose a charge."""
    _setup(client)
    token = _csrf(client)

    r = client.post(
        "/settings/simulate",
        data={
            "csrf_token": token,
            "sender": "alerts@example.com",
            "subject": "Something unrecognised",
            "body": "no rule will ever match this text",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import RawEmail, Transaction, TransactionStatus

    with session_scope() as db:
        tx = db.scalar(select(Transaction))
        assert tx is not None, "an unparsed charge must still be captured"
        assert tx.status == TransactionStatus.needs_attention
        email = db.scalar(select(RawEmail))
        assert email.parse_error  # retained so the rule can be fixed and replayed
        assert email.body_text    # raw body kept for reparse


def test_ingest_is_idempotent(client):
    """A delta replay must not create duplicate transactions."""
    _setup(client)

    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Transaction
    from app.services.graph import GraphMessage
    from app.services.ingest import ingest_message

    message = GraphMessage(
        graph_id="g1",
        internet_message_id="<dupe@example.com>",
        subject="Card transaction alert",
        sender="alerts@example.com",
        received_at=dt.datetime.now(dt.UTC),
        body_html="<p>A charge of $10.00 was made at TEST</p>",
        body_text=None,
    )
    with session_scope() as db:
        assert ingest_message(db, message) is not None
    with session_scope() as db:
        assert ingest_message(db, message) is None  # same message, second delivery
    with session_scope() as db:
        assert db.scalar(select(func.count(Transaction.id))) == 1


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (600, 800), (240, 240, 240)).save(buf, format="PNG")
    return buf.getvalue()


def test_capture_verifies_before_storing(client):
    """Storage must reject a truncated download rather than store it."""
    _setup(client)

    from app.services.storage import CaptureError, store_receipt_bytes

    data = _png_bytes()

    # Truncation is the exact failure that must never be followed by a delete.
    with pytest.raises(CaptureError, match="Truncated"):
        store_receipt_bytes(data, original_filename="r.png", expected_bytes=len(data) + 1)

    with pytest.raises(CaptureError):
        store_receipt_bytes(b"not an image at all", original_filename="evil.exe")

    stored = store_receipt_bytes(data, original_filename="r.png", expected_bytes=len(data))
    assert stored.mime == "image/png"
    assert stored.bytes == len(data)
    assert stored.thumb_rel_path, "thumbnail should have been generated"

    # assert_durable is the gate the delete job runs before touching Discord.
    from app.services.storage import assert_durable

    assert_durable(stored.rel_path, stored.sha256, stored.bytes)
    with pytest.raises(CaptureError, match="checksum"):
        assert_durable(stored.rel_path, "0" * 64, stored.bytes)


def test_path_traversal_is_refused(client):
    _setup(client)
    from app.services.storage import CaptureError, absolute_path

    with pytest.raises(CaptureError):
        absolute_path("../../etc/passwd")


def test_lapsed_is_excluded_from_implicit_but_not_explicit_matching(client):
    """The core rule of lapsing: it hides charges from the picker, not from #code."""
    _setup(client)

    from app.db import session_scope
    from app.models import Transaction, TransactionStatus, utcnow
    from app.services import matching

    with session_scope() as db:
        db.add(
            Transaction(
                short_code="9001", occurred_at=utcnow(), merchant="OLD CHARGE",
                amount_minor=500, currency="USD",
                status=TransactionStatus.lapsed, notified_at=utcnow(),
            )
        )

    with session_scope() as db:
        # Implicit: invisible.
        assert matching.open_transactions(db) == []
        result = matching.resolve(db, referenced_message_id=None, text="here you go")
        assert result.transaction is None

        # Explicit: still reachable, which is what lets you attach a late receipt.
        found = matching.find_by_code(db, "receipt for #9001")
        assert found is not None and found.merchant == "OLD CHARGE"


def test_ambiguous_upload_needs_a_prompt(client):
    _setup(client)

    from app.db import session_scope
    from app.models import Transaction, TransactionStatus, utcnow
    from app.services import matching
    from app.services.matching import MatchMethod

    with session_scope() as db:
        for code, merchant in (("9101", "ONE"), ("9102", "TWO")):
            db.add(
                Transaction(
                    short_code=code, occurred_at=utcnow(), merchant=merchant,
                    amount_minor=100, currency="USD",
                    status=TransactionStatus.notified, notified_at=utcnow(),
                )
            )

    with session_scope() as db:
        result = matching.resolve(db, referenced_message_id=None, text="")
        assert result.method is MatchMethod.ambiguous
        assert result.needs_prompt
        assert len(result.candidates) == 2

        # One outstanding charge auto-attaches instead of nagging.
        db.query(Transaction).filter(Transaction.short_code == "9102").delete()
        db.flush()
        result = matching.resolve(db, referenced_message_id=None, text="")
        assert result.method is MatchMethod.sole_open
        assert result.transaction.short_code == "9101"


def test_every_page_renders(client):
    """Renders each page with StrictUndefined active.

    Catches missing or misspelled context variables, which Jinja2 would otherwise
    render as an empty string — the exact failure mode that hid the status-badge
    bug.
    """
    _setup(client)
    token = _csrf(client)

    # Populate real data so loops and conditionals actually execute; an empty
    # database would leave most of the markup unrendered.
    client.post(
        "/rules",
        data={
            "csrf_token": token, "name": "R", "enabled": "on", "priority": "100",
            "sender_match": "alerts@", "body_regex": r"(?P<amount>[\d.]+)",
            "default_currency": "USD",
        },
        follow_redirects=False,
    )
    client.post(
        "/settings/simulate",
        data={
            "csrf_token": token, "sender": "alerts@example.com",
            "subject": "alert", "body": "amount 12.34 charged",
        },
        follow_redirects=False,
    )

    for path in ("/", "/transactions", "/orphans", "/settings", "/rules",
                 "/health", "/export"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}\n{r.text[:600]}"

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Transaction

    with session_scope() as db:
        code = db.scalar(select(Transaction)).short_code

    r = client.get(f"/transactions/{code}")
    assert r.status_code == 200, r.text[:600]

    # The regression guard: the badge must carry a real status, not "badge-".
    listing = client.get("/transactions").text
    assert 'class="badge badge-' in listing
    assert 'class="badge badge-"' not in listing, "status decayed to a bare string again"


def test_receipt_files_require_authentication(client):
    _setup(client)
    client.post("/logout", data={"csrf_token": _csrf(client)}, follow_redirects=False)

    r = client.get("/receipts/1", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/login"


def test_export_csv_round_trip(client):
    _setup(client)
    token = _csrf(client)

    from app.db import session_scope
    from app.models import Transaction, TransactionStatus, utcnow

    with session_scope() as db:
        db.add(
            Transaction(
                short_code="9200", occurred_at=utcnow(), merchant="CAFÉ MÜNCHEN",
                amount_minor=-1250, currency="EUR", status=TransactionStatus.verified,
            )
        )

    r = client.post("/export/csv", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 200
    body = r.content.decode("utf-8")
    assert body.startswith("﻿"), "CSV needs a BOM or Excel mangles accented names"
    assert "CAFÉ MÜNCHEN" in body
    assert "-12.50" in body   # signed plain decimal, no currency symbol
