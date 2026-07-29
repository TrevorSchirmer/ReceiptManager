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
AMEX_REGEX = (
    r"Account Ending:\s*(?P<card_ending>\d+)"
    r".*?\n(?P<merchant>[^\n]{2,60})\n+"
    r"\$(?P<amount>[\d,]+\.\d{2})\*?\n+"
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
