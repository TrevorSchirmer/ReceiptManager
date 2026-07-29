"""Receipt-transaction parsing for bank and credit-card alert emails.

User-configured regex rules (``ParseRule.body_regex``) always run against the
**normalized plain text** of a message — never against the raw HTML. HTML alert
emails are first converted to text with :func:`html_to_text` (BeautifulSoup +
lxml) and then collapsed/stripped by :func:`normalize_text`, so rules can target
stable, single-spaced lines instead of unpredictable markup. This is exactly why
the live rule tester (:func:`test_rule`) returns ``normalized_text`` plus ``span``
offsets: it shows the user the precise string their regex executed against.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

if TYPE_CHECKING:
    from app.models import ParseRule

__all__ = [
    "ParsedFields",
    "ParseOutcome",
    "html_to_text",
    "normalize_text",
    "parse_amount",
    "rule_matches_envelope",
    "apply_rule",
    "parse_email",
    "test_rule",
]

_RULE_FLAGS = re.IGNORECASE | re.MULTILINE | re.DOTALL

_NBSP_VARIANTS = ("\xa0", " ", " ", " ")
_INLINE_SPACES_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

_CURRENCY_CODES: tuple[str, ...] = (
    "USD", "EUR", "GBP", "CAD", "AUD", "NZD", "JPY", "CHF",
    "SEK", "NOK", "DKK", "ZAR", "INR", "MXN", "SGD", "HKD",
)
_CURRENCY_CODE_RE = re.compile(r"\b(" + "|".join(_CURRENCY_CODES) + r")\b", re.IGNORECASE)
_SYMBOL_TO_CODE = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "₹": "INR"}
_DOLLAR_CODES = {"USD", "CAD", "AUD", "NZD", "SGD", "HKD", "MXN"}
_NUMBER_RUN_RE = re.compile(r"[\d.,]*\d")
_BETWEEN_DIGITS_SPACE_RE = re.compile(r"(?<=\d)\s+(?=\d)")
_NON_DIGIT_RE = re.compile(r"\D")

# Visa/Mastercard show 4; Amex shows 5. The column allows 8 for headroom.
MAX_CARD_ENDING_DIGITS = 8


@dataclass(slots=True)
class ParsedFields:
    """Coerced transaction fields extracted from an alert email."""

    merchant: str
    amount_minor: int
    currency: str
    card_ending: str | None
    cardholder: str | None
    occurred_at: datetime | None


@dataclass(slots=True)
class ParseOutcome:
    """Result of attempting to parse a message; ``error`` is None on success."""

    rule: ParseRule | None
    fields: ParsedFields | None
    error: str | None


def html_to_text(html: str) -> str:
    """Convert an HTML email body to normalized plain text.

    Removes non-content elements and turns ``<br>``/block-level boundaries
    (``</p>``, ``</div>``, ``</tr>``, ``</li>``, headings) into newlines so
    table-based bank emails do not collapse into one blob.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # lxml missing or rejected the document
        soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title", "meta", "link"]):
        tag.decompose()
    # separator="\n" inserts a line break at every text-node boundary, which
    # covers <br> and block-level tags; normalize_text collapses the excess.
    return normalize_text(soup.get_text(separator="\n"))


def normalize_text(text: str) -> str:
    """Normalize line endings, spaces, and blank lines in plain text."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for ch in _NBSP_VARIANTS:
        text = text.replace(ch, " ")
    text = _INLINE_SPACES_RE.sub(" ", text)
    text = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def parse_amount(raw: str, default_currency: str = "USD") -> tuple[int, str]:
    """Parse a raw amount string into ``(amount_minor, currency_code)``.

    Handles both ``.`` and ``,`` decimal conventions, space/period/comma thousands
    separators, accounting-style negatives, and ISO codes or currency symbols.
    Raises :class:`ValueError` if no number is found.
    """
    if raw is None:
        raise ValueError("No amount text provided")
    text = str(raw).strip()
    if not text:
        raise ValueError("No amount text provided")
    for ch in _NBSP_VARIANTS:
        text = text.replace(ch, " ")

    negative = False
    if text.startswith("(") and text.endswith(")") and len(text) > 2:
        negative = True  # accounting-style parentheses denote refunds
        text = text[1:-1].strip()

    text, currency = _detect_currency(text, default_currency)

    if text.startswith("-"):
        negative = True
        text = text[1:].strip()
    elif text.startswith("+"):
        text = text[1:].strip()
    if text.endswith("-"):
        negative = True
        text = text[:-1].strip()

    # Spaces between digits are thousands separators ("1 234,56").
    text = _BETWEEN_DIGITS_SPACE_RE.sub("", text)
    num_match = _NUMBER_RUN_RE.search(text)
    if num_match is None:
        raise ValueError(f"Could not find a number in {raw!r}")
    if num_match.start() > 0 and text[num_match.start() - 1] in "-(":
        negative = True

    value = _parse_numeric(num_match.group(0), raw)

    # Storage contract: a flat 100 multiplier for EVERY currency, including
    # zero-decimal ones like JPY — the app stores "hundredths of the display
    # unit" (¥1,200 -> 120000). All math uses Decimal; never floats.
    minor = int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))
    return (-minor if negative else minor), currency


def rule_matches_envelope(rule: ParseRule, sender: str, subject: str) -> bool:
    """Check sender/subject against the rule's envelope matchers."""
    return _envelope_field_matches(
        rule.sender_match, rule.match_is_regex, sender
    ) and _envelope_field_matches(rule.subject_match, rule.match_is_regex, subject)


def apply_rule(rule: ParseRule, body_text: str) -> ParseOutcome:
    """Run one rule's ``body_regex`` against normalized text; never raises."""
    try:
        pattern = re.compile(rule.body_regex, _RULE_FLAGS)
    except re.error as exc:
        return ParseOutcome(rule=rule, fields=None, error=f"Invalid regex: {exc}")

    try:
        match = pattern.search(body_text)
        if match is None:
            return ParseOutcome(
                rule, None, f"Rule '{rule.name}' did not match the message body"
            )
        groups = match.groupdict()

        raw_amount = groups.get("amount")
        if raw_amount is None:
            return ParseOutcome(
                rule, None, f"Rule '{rule.name}' matched but captured no 'amount' group"
            )

        raw_currency = groups.get("currency")
        if raw_currency and raw_currency.strip():
            # The captured value may be a symbol ("$", "€"); reuse the same
            # detection logic as parse_amount, then feed it in as default.
            _, effective_default = _detect_currency(raw_currency, rule.default_currency)
        else:
            effective_default = (rule.default_currency or "USD").upper()
        try:
            amount_minor, currency = parse_amount(raw_amount, effective_default)
        except ValueError as exc:
            return ParseOutcome(
                rule, None,
                f"Rule '{rule.name}' captured an unparseable amount {raw_amount!r}: {exc}",
            )

        raw_merchant = groups.get("merchant")
        merchant = _INLINE_SPACES_RE.sub(" ", raw_merchant).strip() if raw_merchant else ""

        # Accept the legacy group name too, so rules written against the older
        # field keep working.
        card_ending = None
        raw_card = groups.get("card_ending") or groups.get("card_last4")
        if raw_card:
            digits = _NON_DIGIT_RE.sub("", raw_card)
            # Keep what the issuer actually showed rather than forcing four
            # digits: Amex prints a five-digit account ending, and truncating it
            # means the export no longer matches the statement it is checked
            # against. Trailing digits are kept if the capture is over-long.
            if len(digits) >= 4:
                card_ending = digits[-MAX_CARD_ENDING_DIGITS:]

        raw_cardholder = groups.get("cardholder")
        cardholder = (
            raw_cardholder.strip() if raw_cardholder and raw_cardholder.strip() else None
        )

        occurred_at = _parse_occurred_at(groups.get("occurred_at"), rule.date_format)

        return ParseOutcome(
            rule=rule,
            fields=ParsedFields(
                merchant=merchant,
                amount_minor=amount_minor,
                currency=currency,
                card_ending=card_ending,
                cardholder=cardholder,
                occurred_at=occurred_at,
            ),
            error=None,
        )
    except Exception as exc:  # convert unexpected failures into error outcomes
        return ParseOutcome(rule, None, f"Rule '{rule.name}' failed unexpectedly: {exc}")


def parse_email(
    rules: Iterable[ParseRule], sender: str, subject: str, body_text: str
) -> ParseOutcome:
    """Try enabled rules in priority order against normalized ``body_text``.

    Returns the first successful outcome; otherwise an error outcome. Never raises.
    """
    try:
        ordered = sorted(
            (rule for rule in rules if rule.enabled),
            key=lambda rule: (rule.priority, rule.id or 0),
        )
        envelope_matches: list[ParseRule] = []
        failures: list[str] = []
        for rule in ordered:
            if not rule_matches_envelope(rule, sender, subject):
                continue
            envelope_matches.append(rule)
            outcome = apply_rule(rule, body_text)
            if outcome.fields is not None:
                return outcome
            failures.append(f"'{rule.name}' ({outcome.error})")
        if envelope_matches:
            detail = "; ".join(failures) or "no attempts were made"
            return ParseOutcome(
                rule=envelope_matches[0],
                fields=None,
                error=(
                    f"{len(envelope_matches)} rule(s) matched sender/subject but "
                    f"none extracted an amount: {detail}"
                ),
            )
        return ParseOutcome(None, None, "No parse rule matched this sender/subject")
    except Exception as exc:  # parse_email must never raise
        return ParseOutcome(None, None, f"Unexpected parse error: {exc}")


def test_rule(
    rule: ParseRule,
    sender: str,
    subject: str,
    raw_body: str,
    is_html: bool = True,
) -> dict[str, Any]:
    """Live-test a rule for the settings page; returns a JSON-safe dict.

    Never raises — problems are reported via the ``error`` key. The regex is
    executed against the normalized text, which is echoed back so the UI can
    highlight the exact match span.
    """
    result: dict[str, Any] = {
        "envelope_matched": False,
        "normalized_text": "",
        "matched": False,
        "error": None,
        "groups": {},
        "span": None,
        "fields": None,
    }
    try:
        result["envelope_matched"] = rule_matches_envelope(rule, sender, subject)
        result["normalized_text"] = (
            html_to_text(raw_body) if is_html else normalize_text(raw_body)
        )
        try:
            pattern = re.compile(rule.body_regex, _RULE_FLAGS)
        except re.error as exc:
            result["error"] = f"Invalid regex: {exc}"
            return result
        match = pattern.search(result["normalized_text"])
        if match is None:
            result["error"] = f"Rule '{rule.name}' did not match the message body"
            return result
        result["matched"] = True
        result["groups"] = dict(match.groupdict())
        result["span"] = [match.start(), match.end()]

        outcome = apply_rule(rule, result["normalized_text"])
        if outcome.fields is None:
            result["error"] = outcome.error
            return result
        fields = outcome.fields
        amount_display = (Decimal(fields.amount_minor) / Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        result["fields"] = {
            "merchant": fields.merchant,
            "amount_minor": fields.amount_minor,
            "amount_display": str(amount_display),
            "currency": fields.currency,
            "card_ending": fields.card_ending,
            "cardholder": fields.cardholder,
            "occurred_at": fields.occurred_at.isoformat() if fields.occurred_at else None,
        }
    except Exception as exc:  # the tester must never raise
        result["error"] = f"Unexpected error: {exc}"
    return result


def _envelope_field_matches(pattern: str, is_regex: bool, value: str) -> bool:
    """Match one envelope field; empty pattern matches anything."""
    if not pattern:
        return True
    value = value or ""
    if is_regex:
        try:
            return re.search(pattern, value, re.IGNORECASE) is not None
        except re.error:
            return False
    return pattern.lower() in value.lower()


def _detect_currency(text: str, default_currency: str | None) -> tuple[str, str]:
    """Strip a currency token/symbol from ``text``; return ``(rest, code)``."""
    default = (default_currency or "USD").upper()
    code_match = _CURRENCY_CODE_RE.search(text)
    if code_match is not None:
        rest = (text[: code_match.start()] + text[code_match.end():]).strip()
        return rest, code_match.group(1).upper()
    for symbol, code in _SYMBOL_TO_CODE.items():
        if symbol in text:
            if symbol == "$":
                # A bare "$" belongs to the default currency when that default is
                # itself a dollar-denominated currency; otherwise USD.
                code = default if default in _DOLLAR_CODES else "USD"
            return text.replace(symbol, "").strip(), code
    return text.strip(), default


def _parse_numeric(num: str, raw: str) -> Decimal:
    """Convert a digit/``.``/``,`` run to a Decimal using separator heuristics."""
    dot = num.rfind(".")
    comma = num.rfind(",")
    if dot != -1 and comma != -1:
        # Whichever separator appears last is the decimal separator.
        cleaned = num.replace(",", "") if dot > comma else num.replace(".", "").replace(",", ".")
    elif dot != -1:
        cleaned = _collapse_single_separator(num, ".")
    elif comma != -1:
        cleaned = _collapse_single_separator(num, ",")
    else:
        cleaned = num
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Could not parse a number from {raw!r}") from exc


def _collapse_single_separator(num: str, sep: str) -> str:
    """Resolve a number containing only one kind of separator."""
    if num.count(sep) > 1:
        return num.replace(sep, "")  # repeated separator -> thousands
    int_part, _, frac = num.partition(sep)
    # A lone separator followed by exactly 3 digits with a 1-3 digit integer part
    # is a thousands separator ("1.234", "1,234"); otherwise it is a decimal point.
    #
    # A leading zero rules that out: "0.005" is five thousandths, never five —
    # no thousands separator ever follows a leading zero.
    if (
        len(frac) == 3
        and 1 <= len(int_part) <= 3
        and int_part.isdigit()
        and not int_part.startswith("0")
    ):
        return num.replace(sep, "")
    return num if sep == "." else num.replace(sep, ".")


def _parse_occurred_at(raw: str | None, date_format: str | None) -> datetime | None:
    """Parse the captured datetime to aware UTC; None on any failure."""
    if not raw or not raw.strip():
        return None
    value = raw.strip()
    try:
        if date_format:
            parsed = datetime.strptime(value, date_format)
        else:
            parsed = dateutil_parser.parse(value, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None  # caller falls back to the email's received time
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
