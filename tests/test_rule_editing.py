"""Editing a rule instead of retyping it.

The save route always supported an ``id``, but the form never sent one and the
table offered only Delete — so changing a regex meant deleting the rule and
retyping every field, with the old one gone if you got the new one wrong.

The failure mode worth guarding against is subtler than "editing doesn't work":
a form that posts a blank id *silently creates a second rule*. Two rules then
match the same email, the lower priority wins, and the edit appears to have had
no effect at all.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

PASSWORD = "correct-horse-battery-staple"

# Deliberately full of the characters an HTML attribute would mangle.
OLD_REGEX = r"(?P<merchant>[^\n]{2,60})\n+\$(?P<amount>[\d,]+\.\d{2})"
NEW_REGEX = (
    r"(?P<merchant>[^\n]{2,60})\n+"
    r"(?P<currency>[A-Z]{0,3}[$£€¥])\s*(?P<amount>[\d,]+\.\d{2})\*?"
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_DATA_DIR", str(tmp_path))
    import app.config
    import app.db

    app.config.get_config.cache_clear()
    app.db._engine = None
    app.db._SessionFactory = None

    from app.db import init_db
    from app.main import create_app

    init_db()
    with TestClient(create_app()) as c:
        c.post("/setup", data={"password": PASSWORD, "password_confirm": PASSWORD},
               follow_redirects=True)
        yield c


def _csrf(client: TestClient) -> str:
    import re

    page = client.get("/rules").text
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, "no CSRF token on the rules page"
    return match.group(1)


def _make_rule(client: TestClient) -> int:
    from app.db import session_scope
    from app.models import ParseRule

    client.post("/rules", data={
        "csrf_token": _csrf(client),
        "name": "Amex alerts",
        "priority": "100",
        "sender_match": "americanexpress.com",
        "subject_match": "Large Purchase",
        "body_regex": OLD_REGEX,
        "default_currency": "USD",
        "enabled": "on",
    }, follow_redirects=True)

    with session_scope() as db:
        rules = list(db.scalars(select(ParseRule)))
        assert len(rules) == 1
        return int(rules[0].id)


def test_the_table_offers_a_way_into_the_form(client):
    rule_id = _make_rule(client)
    page = client.get("/rules").text
    assert f"/rules?edit={rule_id}" in page, "no Edit link — delete-and-retype is the only path"


def test_editing_loads_the_regex_back_into_the_textarea(client):
    """A textarea holds its value as content; getting this wrong loses the regex."""
    rule_id = _make_rule(client)
    page = client.get(f"/rules?edit={rule_id}").text

    # HTML-escaped in the page, so compare against the escaped form.
    import html

    assert html.escape(OLD_REGEX, quote=False) in page or OLD_REGEX in page, (
        "the regex being edited was not loaded into the form"
    )
    assert 'value="americanexpress.com"' in page
    assert f'name="id" value="{rule_id}"' in page, "the form would post a blank id"


def test_saving_an_edit_updates_rather_than_duplicating(client):
    """The dangerous failure: a second rule that shadows the one you edited."""
    from app.db import session_scope
    from app.models import ParseRule

    rule_id = _make_rule(client)
    client.post("/rules", data={
        "csrf_token": _csrf(client),
        "id": str(rule_id),
        "name": "Amex alerts",
        "priority": "100",
        "sender_match": "americanexpress.com",
        "subject_match": "Large Purchase",
        "body_regex": NEW_REGEX,
        "default_currency": "USD",
        "enabled": "on",
    }, follow_redirects=True)

    with session_scope() as db:
        rules = list(db.scalars(select(ParseRule)))
        assert len(rules) == 1, "the edit created a second, competing rule"
        assert rules[0].body_regex == NEW_REGEX
        assert rules[0].id == rule_id


def test_the_regex_survives_the_round_trip_byte_for_byte(client):
    """Whitespace picked up around a textarea's content would corrupt the pattern."""
    import re

    from app.db import session_scope
    from app.models import ParseRule

    rule_id = _make_rule(client)
    with session_scope() as db:
        db.get(ParseRule, rule_id).body_regex = NEW_REGEX

    page = client.get(f"/rules?edit={rule_id}").text
    match = re.search(r'name="body_regex"[^>]*>(.*?)</textarea>', page, re.DOTALL)
    assert match, "textarea not found"

    import html

    loaded = html.unescape(match.group(1))
    assert loaded == NEW_REGEX, f"regex changed in the round trip: {loaded!r}"
    # And it still compiles — a corrupted pattern must not reach the parser.
    re.compile(loaded)


def test_unchecking_enabled_while_editing_actually_disables(client):
    """An absent checkbox means false; treating it as 'unchanged' strands the rule on."""
    from app.db import session_scope
    from app.models import ParseRule

    rule_id = _make_rule(client)
    client.post("/rules", data={
        "csrf_token": _csrf(client),
        "id": str(rule_id),
        "name": "Amex alerts",
        "priority": "100",
        "body_regex": OLD_REGEX,
        "default_currency": "USD",
        # "enabled" deliberately absent
    }, follow_redirects=True)

    with session_scope() as db:
        assert db.get(ParseRule, rule_id).enabled is False


def test_a_merchant_rule_can_be_edited_too(client):
    from app.db import session_scope
    from app.models import MerchantRule

    client.post("/merchant-rules", data={
        "csrf_token": _csrf(client),
        "pattern": "GITHUB",
        "category": "Software",
        "enabled": "on",
        "skip_receipt": "on",
    }, follow_redirects=True)

    with session_scope() as db:
        rule_id = int(next(iter(db.scalars(select(MerchantRule)))).id)

    page = client.get(f"/rules?edit_merchant={rule_id}").text
    assert 'value="GITHUB"' in page
    assert 'value="Software"' in page

    client.post("/merchant-rules", data={
        "csrf_token": _csrf(client),
        "id": str(rule_id),
        "pattern": "GITHUB",
        "category": "Dev tools",
        "enabled": "on",
        "skip_receipt": "on",
    }, follow_redirects=True)

    with session_scope() as db:
        rules = list(db.scalars(select(MerchantRule)))
        assert len(rules) == 1, "editing a merchant rule created a duplicate"
        assert rules[0].category == "Dev tools"


def test_a_bogus_edit_id_falls_back_to_the_add_form(client):
    """A stale link must not 500 — the rule may have been deleted since."""
    for bad in ("999999", "abc", "", "-1"):
        r = client.get(f"/rules?edit={bad}")
        assert r.status_code == 200, f"?edit={bad} broke the page"
        assert 'name="id" value=""' in r.text
