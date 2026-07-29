"""TOTP two-factor and multi-user login.

The load-bearing test here is that a session which has passed the password but
not the code cannot reach *anything* — including the receipt files. A half-open
session is worse than no second factor at all, because it looks secure.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct-horse-battery-staple"
OTHER_PASSWORD = "second-user-password-here"


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
               follow_redirects=False)
        yield c


def _csrf(client: TestClient, path: str = "/") -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert match, f"no CSRF token on {path}"
    return match.group(1)


def _enable_totp(client: TestClient) -> str:
    """Enroll TOTP through the real UI flow. Returns the secret."""
    import pyotp

    token = _csrf(client, "/account")
    page = client.post("/account/totp/begin", data={"csrf_token": token})
    assert page.status_code == 200
    secret = re.search(r'id="manual-secret" class="mono" value="([A-Z2-7]+)"', page.text)
    assert secret, "enrollment secret not rendered"
    secret_value = secret.group(1)

    r = client.post(
        "/account/totp/confirm",
        data={"csrf_token": token, "code": pyotp.TOTP(secret_value).now()},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return secret_value


def test_account_page_renders(client):
    r = client.get("/account")
    assert r.status_code == 200
    assert "Two-factor authentication" in r.text


def test_totp_is_not_enabled_until_a_code_is_confirmed(client):
    """An abandoned enrollment must never leave the account requiring a code."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import User

    token = _csrf(client, "/account")
    client.post("/account/totp/begin", data={"csrf_token": token})

    with session_scope() as db:
        assert db.scalar(select(User)).totp_secret is None, "enabled before confirmation"

    # A wrong code must not enable it either.
    r = client.post("/account/totp/confirm", data={"csrf_token": token, "code": "000000"},
                    follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as db:
        assert db.scalar(select(User)).totp_secret is None


def test_totp_login_flow(client):
    import pyotp

    secret = _enable_totp(client)
    client.post("/logout", data={"csrf_token": _csrf(client)}, follow_redirects=False)

    # Correct password alone lands on the code form, not the dashboard.
    r = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login/totp"

    # And that half-session can reach nothing else.
    for path in ("/", "/transactions", "/settings", "/export", "/receipts/1"):
        probe = client.get(path, follow_redirects=False)
        assert probe.status_code == 307, f"{path} was reachable pre-2FA"
        assert probe.headers["location"] == "/login/totp"

    assert client.get("/login/totp").status_code == 200

    r = client.post(
        "/login/totp",
        data={"csrf_token": _csrf(client, "/login/totp"), "code": pyotp.TOTP(secret).now()},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_wrong_totp_code_is_rejected(client):
    _enable_totp(client)
    client.post("/logout", data={"csrf_token": _csrf(client)}, follow_redirects=False)
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)

    r = client.post(
        "/login/totp",
        data={"csrf_token": _csrf(client, "/login/totp"), "code": "123456"},
    )
    assert r.status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/login/totp"


def test_disabling_totp_requires_the_password(client):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import User

    _enable_totp(client)
    token = _csrf(client, "/account")

    client.post("/account/totp/disable", data={"csrf_token": token, "password": "wrong-one"},
                follow_redirects=False)
    with session_scope() as db:
        assert db.scalar(select(User)).totp_secret is not None, "disabled with a wrong password"

    client.post("/account/totp/disable", data={"csrf_token": token, "password": PASSWORD},
                follow_redirects=False)
    with session_scope() as db:
        assert db.scalar(select(User)).totp_secret is None


def test_password_change_revokes_other_sessions(client):
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Session

    # A second, independent login for the same account.
    with TestClient(client.app) as other:
        other.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert other.get("/").status_code == 200

        with session_scope() as db:
            assert int(db.scalar(select(func.count(Session.id)))) >= 2

        new_password = "a-brand-new-long-password"
        client.post(
            "/account/password",
            data={
                "csrf_token": _csrf(client, "/account"),
                "current_password": PASSWORD,
                "new_password": new_password,
                "new_password_confirm": new_password,
            },
            follow_redirects=False,
        )

        # The other session is gone; the one that changed it survives.
        assert other.get("/", follow_redirects=False).status_code == 307
        assert client.get("/").status_code == 200


def test_multi_user_login_requires_a_username(client):
    r = client.post(
        "/account/users",
        data={
            "csrf_token": _csrf(client, "/account"),
            "new_username": "bookkeeper",
            "new_user_password": OTHER_PASSWORD,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    # With two accounts the login form must ask which one.
    client.post("/logout", data={"csrf_token": _csrf(client)}, follow_redirects=False)
    assert 'name="username"' in client.get("/login").text

    # A password with no username is now ambiguous and must fail.
    assert client.post("/login", data={"password": OTHER_PASSWORD}).status_code == 401

    r = client.post(
        "/login",
        data={"username": "bookkeeper", "password": OTHER_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get("/").status_code == 200


def test_cannot_delete_your_own_or_the_last_account(client):
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import User

    with session_scope() as db:
        own_id = db.scalar(select(User.id))

    client.post(f"/account/users/{own_id}/delete", data={"csrf_token": _csrf(client, "/account")},
                follow_redirects=False)

    with session_scope() as db:
        assert int(db.scalar(select(func.count(User.id)))) == 1, "deleted the only account"


def test_login_does_not_reveal_whether_a_username_exists(client):
    client.post(
        "/account/users",
        data={
            "csrf_token": _csrf(client, "/account"),
            "new_username": "bookkeeper",
            "new_user_password": OTHER_PASSWORD,
        },
        follow_redirects=False,
    )
    client.post("/logout", data={"csrf_token": _csrf(client)}, follow_redirects=False)

    real = client.post("/login", data={"username": "bookkeeper", "password": "wrong-password"})
    fake = client.post("/login", data={"username": "nobody-here", "password": "wrong-password"})
    assert real.status_code == fake.status_code == 401
    assert "Incorrect username or password" in real.text
    assert "Incorrect username or password" in fake.text
