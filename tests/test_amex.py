"""Parsing a real American Express "Large Purchase Approved" alert.

The fixture is a genuine Amex email with the cardholder name, account ending
and every tracking token replaced — real markup, redacted data. The tracking
parameters matter as much as the name: `comm_track_id` is per-send and ties the
message back to the recipient. It is 63 KB of nested
table HTML, which is exactly the shape that makes naive tag-stripping fail: get
the newlines wrong and the merchant, amount and date collapse onto one line and
no regex can separate them.

The rule below is the one to paste into Settings → Parse rules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "amex_large_purchase.html"

SENDER = "American Express <AmericanExpress@welcome.americanexpress.com>"
SUBJECT = "Large Purchase Approved"

# Anchored on structure rather than on Amex's marketing copy: an account-ending
# line, then a merchant line immediately followed by an amount line, then a date.
# The amount anchor is what disambiguates the merchant from the other
# capitalised lines earlier in the message.
#
# The currency group carries the prefix as well as the symbol, because Amex
# writes a foreign charge as "CA$300.00". Requiring a bare "$" made the rule fail
# to match those outright, and treating the prefix as decoration would have
# recorded Canadian dollars as US ones.
AMEX_REGEX = (
    r"Account Ending:\s*(?P<card_ending>\d+)"
    r".*?\n(?P<merchant>[^\n]{2,60})\n+"
    r"(?P<currency>[A-Z]{0,3}[$£€¥])\s*(?P<amount>[\d,]+\.\d{2})\*?\n+"
    r"(?P<occurred_at>[A-Za-z]{3},\s+[A-Za-z]{3}\s+\d{1,2},\s+\d{4})"
)


def _rule():
    from app.models import ParseRule

    return ParseRule(
        name="Amex large purchase",
        enabled=True,
        priority=100,
        sender_match="americanexpress.com",
        subject_match="Large Purchase",
        match_is_regex=False,
        body_regex=AMEX_REGEX,
        default_currency="USD",
        date_format=None,
    )


@pytest.fixture()
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_normalisation_keeps_the_fields_on_separate_lines(html):
    """63 KB of table markup must reduce to readable, line-separated text."""
    from app.services.parsing import html_to_text

    text = html_to_text(html)
    assert len(text) < 3000, "normalisation left far too much boilerplate"
    assert "BAMBULAB USA\n\n$792.97" in text, (
        "merchant and amount were not kept on separate lines:\n"
        f"{text[:600]}"
    )


def test_amex_rule_extracts_every_field(html):
    from app.services.parsing import test_rule

    result = test_rule(_rule(), sender=SENDER, subject=SUBJECT, raw_body=html, is_html=True)

    assert result["envelope_matched"] is True
    assert result["matched"] is True
    assert result["error"] is None

    fields = result["fields"]
    assert fields["merchant"] == "BAMBULAB USA"
    assert fields["amount_minor"] == 79297      # integer cents, never a float
    assert fields["amount_display"] == "792.97"
    assert fields["currency"] == "USD"
    assert fields["occurred_at"].startswith("2026-07-28")


def test_amex_five_digit_account_ending_is_not_truncated(html):
    """Amex prints five digits; keeping only four breaks statement matching."""
    from app.services.parsing import test_rule

    result = test_rule(_rule(), sender=SENDER, subject=SUBJECT, raw_body=html, is_html=True)
    assert result["groups"]["card_ending"] == "12345"
    assert result["fields"]["card_ending"] == "12345"


def test_card_last4_is_accepted_as_an_alias(html):
    """Rules written against the old group name must keep working."""
    from app.models import ParseRule
    from app.services.parsing import test_rule

    rule = _rule()
    rule.body_regex = AMEX_REGEX.replace("card_ending", "card_last4")
    assert isinstance(rule, ParseRule)

    result = test_rule(rule, sender=SENDER, subject=SUBJECT, raw_body=html, is_html=True)
    assert result["fields"]["card_ending"] == "12345"


def test_full_ingest_of_the_amex_alert(tmp_path, monkeypatch, html):
    """End to end: the email becomes a transaction ready to announce."""
    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    from app.db import init_db, session_scope
    from app.models import TransactionStatus

    init_db()

    with session_scope() as db:
        db.add(_rule())

    with session_scope() as db:
        from app.services.ingest import simulate_email

        tx = simulate_email(db, sender=SENDER, subject=SUBJECT, body=html, is_html=True)
        assert tx is not None
        assert tx.merchant == "BAMBULAB USA"
        assert tx.amount_minor == 79297
        assert tx.currency == "USD"
        assert tx.card_ending == "12345"
        assert tx.status == TransactionStatus.new
        assert not tx.is_refund

    # And it is queued for announcement rather than sent inline.
    with session_scope() as db:
        from sqlalchemy import func, select

        from app.models import Job

        assert db.scalar(
            select(func.count(Job.id)).where(Job.kind == "discord.notify")
        ) == 1


def test_rule_ignores_an_unrelated_sender(html):
    """The envelope filter must not fire on a lookalike from someone else."""
    from app.services.parsing import test_rule

    result = test_rule(
        _rule(), sender="phish@not-amex.example", subject=SUBJECT, raw_body=html, is_html=True
    )
    assert result["envelope_matched"] is False


# --------------------------------------------------------------------------- #
# Foreign-currency charges
# --------------------------------------------------------------------------- #

CAD_BODY = """Account Ending: 81000

There was a large purchase on your Card

Dear JANE Q EXAMPLE,

As you requested, we're letting you know that this purchase was more than $50.00.

You can change the dollar amount of these large purchase notifications online.

ENVOIE SIMPLE CANADA

CA$300.00*

Mon, Aug 3, 2026
"""


def test_a_prefixed_dollar_is_not_us_dollars():
    """"CA$300.00" is Canadian. Reading it as USD misstates the books silently."""
    from app.services.parsing import parse_amount

    assert parse_amount("CA$300.00", "USD") == (30000, "CAD")
    assert parse_amount("US$50.00", "USD") == (5000, "USD")
    assert parse_amount("A$25.00", "USD") == (2500, "AUD")
    assert parse_amount("AU$25.00", "USD") == (2500, "AUD")
    assert parse_amount("NZ$10.00", "USD") == (1000, "NZD")
    assert parse_amount("HK$88.00", "USD") == (8800, "HKD")
    assert parse_amount("R$45.50", "USD") == (4550, "BRL")
    # "C$" must not shadow "CA$" — longest prefix wins.
    assert parse_amount("C$15.00", "USD") == (1500, "CAD")
    # And the unprefixed forms are untouched.
    assert parse_amount("$792.97", "USD") == (79297, "USD")
    assert parse_amount("£0.99", "USD") == (99, "GBP")


def test_one_rule_handles_both_currencies():
    """The same rule must cover the domestic and foreign shapes of this alert."""
    from app.services.parsing import test_rule

    domestic = test_rule(_rule(), sender=SENDER, subject=SUBJECT,
                         raw_body=FIXTURE.read_text(encoding="utf-8"), is_html=True)
    assert domestic["fields"]["currency"] == "USD"
    assert domestic["fields"]["amount_minor"] == 79297

    foreign = test_rule(_rule(), sender=SENDER, subject=SUBJECT,
                        raw_body=CAD_BODY, is_html=False)
    assert foreign["matched"] is True, foreign["error"]
    fields = foreign["fields"]
    assert fields["merchant"] == "ENVOIE SIMPLE CANADA"
    assert fields["amount_minor"] == 30000
    assert fields["currency"] == "CAD", "a Canadian charge recorded in the wrong currency"
    assert fields["card_ending"] == "81000"
    assert fields["occurred_at"].startswith("2026-08-03")


def test_month_to_date_does_not_add_currencies_together(tmp_path, monkeypatch):
    """300 CAD plus 792.97 USD is not 1092.97 of anything."""
    import re

    from fastapi.testclient import TestClient

    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    import app.config
    import app.db
    import app.security

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None
    app.security._fernet = None
    from app.db import init_db, session_scope
    from app.main import create_app
    from app.models import Transaction, utcnow

    init_db()
    with session_scope() as db:
        now = utcnow()
        db.add(Transaction(short_code="1000", occurred_at=now, merchant="BAMBULAB USA",
                           amount_minor=79297, currency="USD"))
        db.add(Transaction(short_code="1001", occurred_at=now, merchant="ENVOIE SIMPLE CANADA",
                           amount_minor=30000, currency="CAD"))

    with TestClient(create_app()) as c:
        c.post("/setup", data={"password": "correct-horse-battery-staple",
                               "password_confirm": "correct-horse-battery-staple"},
               follow_redirects=False)
        page = c.get("/").text

    # The headline figure is the default currency alone, not a meaningless total.
    assert "$792.97" in page
    assert "1,092.97" not in page, "currencies were summed together"
    # The Canadian total is shown separately rather than hidden.
    assert re.search(r"plus[\s\S]{0,80}300\.00", page), "other currencies not surfaced"
