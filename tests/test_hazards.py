"""Adversarial tests for the places most likely to be subtly wrong.

These probe specific hazards rather than features:
* does a status survive a database round trip as an enum, or decay to a string?
* does a short-code collision destroy the surrounding transaction?
* does enqueueing a duplicate job poison the caller's transaction?
* do lapsing and money handling behave at the boundaries?
"""

from __future__ import annotations

import datetime as dt

import pytest


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    import app.config
    import app.db

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None
    from app.db import init_db

    init_db()
    yield


def _make_tx(db, code="5000", **kw):
    from app.models import Transaction, TransactionStatus, utcnow

    defaults = dict(
        short_code=code, occurred_at=utcnow(), merchant="TEST",
        amount_minor=1000, currency="USD", status=TransactionStatus.notified,
        notified_at=utcnow(),
    )
    defaults.update(kw)
    tx = Transaction(**defaults)
    db.add(tx)
    db.flush()
    return tx


def test_status_survives_db_roundtrip_as_enum(app_env):
    """Templates call `tx.status.value` — a plain str would 500 the page."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Transaction, TransactionStatus

    with session_scope() as db:
        _make_tx(db, status=TransactionStatus.receipt_attached)

    # Fresh session: no identity-map cache to hide a decayed type.
    with session_scope() as db:
        tx = db.scalar(select(Transaction))
        assert isinstance(tx.status, TransactionStatus), (
            f"status came back as {type(tx.status).__name__}; templates use .status.value"
        )
        assert tx.status.value == "receipt_attached"


def test_timestamps_survive_roundtrip_as_aware_utc(app_env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Transaction, utcnow

    with session_scope() as db:
        _make_tx(db)

    with session_scope() as db:
        tx = db.scalar(select(Transaction))
        assert tx.occurred_at.tzinfo is not None
        # The comparison that broke session expiry, lapsing and the heartbeat.
        assert tx.occurred_at <= utcnow()


def test_shortcode_collision_does_not_destroy_the_transaction(app_env):
    """A collision must roll back only the failed insert, not the whole unit of work.

    ingest_message flushes a RawEmail and *then* allocates a short code. If the
    allocation retry rolled back the outer transaction, the email would vanish
    and the ingest would silently lose a charge.
    """
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import RawEmail, Transaction, utcnow
    from app.services import shortcode

    with session_scope() as db:
        email = RawEmail(
            internet_message_id="<collide@example.com>",
            sender="a@b.c", subject="s", body_text="b", received_at=utcnow(),
        )
        db.add(email)
        db.flush()
        email_id = email.id

        # Occupy the code allocate() will try first.
        taken = shortcode.next_short_code(db)
        _make_tx(db, code=taken)

        # Force one collision, then succeed.
        codes = iter([taken, str(int(taken) + 1)])
        original = shortcode.next_short_code
        shortcode.next_short_code = lambda _db: next(codes)
        try:
            tx = shortcode.allocate(
                db,
                lambda code: Transaction(
                    short_code=code, occurred_at=utcnow(), merchant="AFTER COLLISION",
                    amount_minor=1, currency="USD", email_id=email_id,
                ),
            )
        finally:
            shortcode.next_short_code = original

        assert tx.merchant == "AFTER COLLISION"

    with session_scope() as db:
        assert db.scalar(
            select(func.count(RawEmail.id)).where(RawEmail.id == email_id)
        ) == 1, "the RawEmail was rolled back by the short-code retry"


def test_duplicate_job_does_not_poison_the_caller_transaction(app_env):
    """enqueue() must swallow a duplicate key without killing the outer unit of work."""
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Job, Transaction
    from app.services import jobs

    with session_scope() as db:
        jobs.enqueue(db, kind="discord.notify", payload={"transaction_id": 1},
                     idempotency_key="dupe-key")

    with session_scope() as db:
        tx = _make_tx(db, code="5555")
        # Same idempotency key -> must be a no-op, not an explosion.
        assert jobs.enqueue(db, kind="discord.notify", payload={"transaction_id": 2},
                            idempotency_key="dupe-key") is None
        # The surrounding work must still commit.
        tx.merchant = "STILL COMMITTED"
        db.add(tx)

    with session_scope() as db:
        assert db.scalar(select(func.count(Job.id))) == 1
        saved = db.scalar(select(Transaction).where(Transaction.short_code == "5555"))
        assert saved is not None and saved.merchant == "STILL COMMITTED"


def test_lapse_sweep_respects_the_window_boundary(app_env):
    from app.db import session_scope
    from app.models import Transaction, TransactionStatus, utcnow
    from app.services.ingest import sweep_lapsed

    with session_scope() as db:
        _make_tx(db, code="6001", notified_at=utcnow() - dt.timedelta(hours=25))
        _make_tx(db, code="6002", notified_at=utcnow() - dt.timedelta(hours=23))
        # Already has a receipt: must never lapse.
        _make_tx(db, code="6003", status=TransactionStatus.receipt_attached,
                 notified_at=utcnow() - dt.timedelta(hours=99))

    with session_scope() as db:
        assert sweep_lapsed(db) == 1

    with session_scope() as db:
        from sqlalchemy import select

        by_code = {t.short_code: t.status for t in db.scalars(select(Transaction))}
        assert by_code["6001"] == TransactionStatus.lapsed
        assert by_code["6002"] == TransactionStatus.notified
        assert by_code["6003"] == TransactionStatus.receipt_attached

    # Sweeping again must be a no-op, not re-stamp lapsed_at.
    with session_scope() as db:
        assert sweep_lapsed(db) == 0


@pytest.mark.parametrize(
    ("raw", "expected_minor", "expected_ccy"),
    [
        ("$1,234.56", 123456, "USD"),
        ("1.234,56 €", 123456, "EUR"),
        ("43.21", 4321, "USD"),
        ("1,234", 123400, "USD"),
        ("1.234", 123400, "USD"),
        ("12.5", 1250, "USD"),
        ("USD 43.21", 4321, "USD"),
        ("-43.21", -4321, "USD"),
        ("(43.21)", -4321, "USD"),
        ("1 234,56", 123456, "USD"),
        ("£0.99", 99, "GBP"),
        ("$0.005", 1, "USD"),          # rounds half-up, never truncates
    ],
)
def test_amount_parsing_boundaries(raw, expected_minor, expected_ccy):
    from app.services.parsing import parse_amount

    minor, ccy = parse_amount(raw, "USD")
    assert minor == expected_minor, f"{raw!r} -> {minor}, expected {expected_minor}"
    assert ccy == expected_ccy


def test_amount_parsing_rejects_garbage():
    from app.services.parsing import parse_amount

    with pytest.raises(ValueError):
        parse_amount("no digits here", "USD")
    with pytest.raises(ValueError):
        parse_amount("", "USD")


def test_money_formatting_is_signed_and_grouped():
    from app.formatting import money, money_plain

    assert money(123456, "USD") == "$1,234.56"
    assert money(-4321, "USD") == "-$43.21"
    assert money(4321, "EUR") == "€43.21"
    assert money(4321, "SEK") == "43.21 SEK"     # no symbol -> code suffix
    assert money_plain(-4321) == "-43.21"        # CSV form: no symbol at all


def test_explicit_code_beats_a_reply_to_a_different_charge(app_env):
    """Reply is the strongest signal; it must win over a stray code in the text."""
    from app.db import session_scope
    from app.models import DiscordMessage
    from app.services import matching

    with session_scope() as db:
        replied_to = _make_tx(db, code="7001", merchant="REPLIED")
        _make_tx(db, code="7002", merchant="MENTIONED")
        db.add(DiscordMessage(message_id="m1", channel_id="c1", direction="out",
                              kind="notify", transaction_id=replied_to.id))

    with session_scope() as db:
        result = matching.resolve(db, referenced_message_id="m1", text="oops #7002")
        assert result.transaction.merchant == "REPLIED"


def test_picker_is_capped_at_discord_option_limit(app_env):
    from app.db import session_scope
    from app.services import matching
    from app.services.matching import MAX_PICKER_OPTIONS, MatchMethod

    with session_scope() as db:
        for i in range(MAX_PICKER_OPTIONS + 5):
            _make_tx(db, code=str(8000 + i))

    with session_scope() as db:
        result = matching.resolve(db, referenced_message_id=None, text="")
        assert result.method is MatchMethod.ambiguous
        assert len(result.candidates) == MAX_PICKER_OPTIONS
        assert result.truncated is True


def test_needs_attention_charges_are_still_matchable(app_env):
    """A charge whose alert failed to parse must still be able to receive a receipt."""
    from app.db import session_scope
    from app.models import TransactionStatus
    from app.services import matching
    from app.services.matching import MatchMethod

    with session_scope() as db:
        _make_tx(db, code="8500", status=TransactionStatus.needs_attention)

    with session_scope() as db:
        result = matching.resolve(db, referenced_message_id=None, text="")
        assert result.method is MatchMethod.sole_open
        assert result.transaction.short_code == "8500"


def test_secrets_are_encrypted_at_rest(app_env):
    """A leaked DB file alone must not yield the bot token."""
    import sqlite3

    from app.config import get_config
    from app.db import session_scope
    from app.security import get_setting, set_setting

    token = "super-secret-discord-token-value"
    with session_scope() as db:
        set_setting(db, "discord.bot_token", token, is_secret=True)

    with session_scope() as db:
        assert get_setting(db, "discord.bot_token") == token

    raw = sqlite3.connect(get_config().db_path).execute(
        "SELECT value FROM settings WHERE key='discord.bot_token'"
    ).fetchone()[0]
    assert token not in raw, "secret stored in plaintext"


def test_losing_the_secret_key_is_recoverable(app_env):
    """A restore without secret.key must degrade, not take the app down.

    Raising here would 500 the Settings and Health pages — precisely the two
    pages needed to diagnose and fix it — so the credential reads as unset, the
    failure is logged, and Health reports it.
    """
    import app.config
    import app.security
    from app.config import get_config
    from app.db import session_scope
    from app.security import get_setting, secret_is_unreadable, set_setting

    with session_scope() as db:
        set_setting(db, "discord.bot_token", "a-real-token", is_secret=True)
        assert get_setting(db, "discord.bot_token") == "a-real-token"

    # Simulate a restore that brought the database but not the key.
    get_config().secret_key_path.unlink()
    app.config.get_config.cache_clear()
    app.security._fernet = None

    with session_scope() as db:
        assert get_setting(db, "discord.bot_token") is None
        assert secret_is_unreadable(db, "discord.bot_token") is True
        # Non-secret settings are untouched — they were never encrypted.
        set_setting(db, "workflow.timezone", "UTC")
        assert get_setting(db, "workflow.timezone") == "UTC"

    # Re-entering the credential re-encrypts it with the new key.
    with session_scope() as db:
        set_setting(db, "discord.bot_token", "a-fresh-token", is_secret=True)
    with session_scope() as db:
        assert get_setting(db, "discord.bot_token") == "a-fresh-token"
        assert secret_is_unreadable(db, "discord.bot_token") is False


def test_html_normalisation_keeps_table_rows_apart():
    """Bank alerts are table-based; collapsing them into one line breaks every regex."""
    from app.services.parsing import html_to_text

    html = "<table><tr><td>Merchant</td><td>AMAZON</td></tr><tr><td>Amount</td><td>$43.21</td></tr></table>"
    text = html_to_text(html)
    assert "AMAZON" in text and "$43.21" in text
    assert "AMAZON$43.21" not in text.replace("\n", "").replace(" ", "") or "\n" in text
    assert text.count("\n") >= 1, f"rows were collapsed: {text!r}"


def test_html_normalisation_strips_scripts_and_nbsp():
    from app.services.parsing import html_to_text

    text = html_to_text("<div>Total:&nbsp;$5.00</div><script>alert('x')</script>")
    assert "alert" not in text
    assert "\xa0" not in text
    assert "Total: $5.00" in text
