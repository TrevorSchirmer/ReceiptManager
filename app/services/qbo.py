"""QuickBooks Online client.

Unlike Graph, which uses app-only client credentials, QuickBooks needs a human to
authorise the app against a specific company. That makes this the only place in
the codebase running an OAuth **authorisation-code** flow.

Two properties of Intuit's tokens will silently kill the integration if handled
carelessly, so both are handled deliberately here:

* **The refresh token rotates on every use.** The response to a refresh contains
  a *new* refresh token and invalidates the old one. Fail to persist it and the
  connection is dead at the next refresh, with nothing to indicate why. Every
  token response is therefore committed before the access token it carries is
  used for anything.
* **The refresh token expires after roughly 100 days of non-use.** Its expiry is
  stored so the health page can warn ahead of time rather than the integration
  simply stopping one day.

The redirect URI is worth understanding too: it is a *browser* redirect, not a
callback Intuit connects to. An internal-only hostname works fine, provided the
browser doing the authorisation can reach it.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app import settings_keys as sk
from app.db import session_scope
from app.models import utcnow

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

SCOPE = "com.intuit.quickbooks.accounting"

_API_BASE = {
    "production": "https://quickbooks.api.intuit.com",
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
}

# Refresh a little early rather than racing the expiry on a slow request.
_ACCESS_SKEW = dt.timedelta(minutes=5)


class QboError(RuntimeError):
    """Any non-retryable QuickBooks failure."""


class QboAuthError(QboError):
    """Credentials or consent problem. Retrying will not help."""


class QboNotConnected(QboError):
    """No company has authorised the app yet."""


class QboConflict(QboError):
    """Stale SyncToken — the record changed in QuickBooks since we read it."""


@dataclass(slots=True)
class TokenSet:
    access_token: str
    refresh_token: str
    access_expires_at: dt.datetime
    refresh_expires_at: dt.datetime


def _parse_dt(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


# --------------------------------------------------------------------------- #
# Authorisation URL
# --------------------------------------------------------------------------- #

def build_authorize_url(db: Session, state: str) -> str:
    """The URL to send the browser to for consent."""
    client_id = sk.get_str(db, sk.QBO_CLIENT_ID)
    redirect_uri = sk.get_str(db, sk.QBO_REDIRECT_URI)
    if not client_id or not redirect_uri:
        raise QboError("Set the QuickBooks client ID and redirect URI first.")
    return f"{AUTHORIZE_URL}?" + urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )


def new_state() -> str:
    return secrets.token_urlsafe(24)


# --------------------------------------------------------------------------- #
# Token exchange and refresh
# --------------------------------------------------------------------------- #

def _basic_auth(db: Session) -> str:
    client_id = sk.get_str(db, sk.QBO_CLIENT_ID)
    client_secret = sk.get_str(db, sk.QBO_CLIENT_SECRET)
    if not client_id or not client_secret:
        raise QboAuthError("QuickBooks client ID and secret are not configured.")
    raw = f"{client_id}:{client_secret}".encode()
    return base64.b64encode(raw).decode()


def _token_set_from(payload: dict[str, Any]) -> TokenSet:
    now = utcnow()
    return TokenSet(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload["refresh_token"]),
        access_expires_at=now + dt.timedelta(seconds=int(payload.get("expires_in", 3600))),
        refresh_expires_at=now
        + dt.timedelta(seconds=int(payload.get("x_refresh_token_expires_in", 8726400))),
    )


def store_tokens(db: Session, tokens: TokenSet) -> None:
    """Persist a token set.

    Called before the access token is used for anything. The refresh token that
    arrived with it has already invalidated its predecessor, so losing it here
    means losing the connection.
    """
    sk.put(db, sk.QBO_ACCESS_TOKEN, tokens.access_token)
    sk.put(db, sk.QBO_REFRESH_TOKEN, tokens.refresh_token)
    sk.put(db, sk.QBO_ACCESS_EXPIRES_AT, tokens.access_expires_at.isoformat())
    sk.put(db, sk.QBO_REFRESH_EXPIRES_AT, tokens.refresh_expires_at.isoformat())


def clear_tokens(db: Session) -> None:
    for key in (
        sk.QBO_ACCESS_TOKEN, sk.QBO_REFRESH_TOKEN, sk.QBO_ACCESS_EXPIRES_AT,
        sk.QBO_REFRESH_EXPIRES_AT, sk.QBO_REALM_ID, sk.QBO_COMPANY_NAME,
    ):
        sk.put(db, key, "")


async def _post_token(auth: str, form: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            TOKEN_URL,
            data=form,
            headers={
                "Authorization": f"Basic {auth}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    if response.status_code >= 400:
        raise QboAuthError(
            f"QuickBooks rejected the token request ({response.status_code}): "
            f"{response.text[:400]}"
        )
    return dict(response.json())


async def exchange_code(code: str, realm_id: str) -> None:
    """Trade an authorisation code for tokens and record the company."""
    from app.db import run_db

    def read() -> tuple[str, str]:
        with session_scope() as db:
            return _basic_auth(db), sk.get_str(db, sk.QBO_REDIRECT_URI)

    auth, redirect_uri = await run_db(read)
    payload = await _post_token(
        auth,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
    )
    tokens = _token_set_from(payload)

    def write() -> None:
        with session_scope() as db:
            store_tokens(db, tokens)
            sk.put(db, sk.QBO_REALM_ID, realm_id)

    await run_db(write)
    logger.info("QuickBooks connected to realm %s", realm_id)


async def refresh_tokens() -> TokenSet:
    """Refresh, persisting the rotated refresh token before returning."""
    from app.db import run_db

    def read() -> tuple[str, str]:
        with session_scope() as db:
            token = sk.get_str(db, sk.QBO_REFRESH_TOKEN)
            if not token:
                raise QboNotConnected("QuickBooks is not connected.")
            return _basic_auth(db), token

    auth, refresh_token = await run_db(read)
    payload = await _post_token(
        auth, {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )
    tokens = _token_set_from(payload)

    def write() -> None:
        with session_scope() as db:
            store_tokens(db, tokens)

    # Committed before the caller can use the access token: the old refresh
    # token is already dead by this point.
    await run_db(write)
    logger.info("QuickBooks tokens refreshed (rotated)")
    return tokens


async def revoke(token: str) -> None:
    """Best-effort revoke on disconnect."""
    from app.db import run_db

    def read() -> str:
        with session_scope() as db:
            return _basic_auth(db)

    try:
        auth = await run_db(read)
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                REVOKE_URL,
                json={"token": token},
                headers={"Authorization": f"Basic {auth}",
                         "Content-Type": "application/json"},
            )
    except Exception:
        # Disconnecting locally matters more than telling Intuit about it.
        logger.warning("Could not revoke the QuickBooks token", exc_info=True)


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #

class QboClient:
    """Thin async wrapper over the v3 API.

    Mirrors :class:`app.services.graph.GraphClient` — same context-manager shape,
    same retry and throttle handling — so there is one way to reason about
    outbound HTTP in this codebase.
    """

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._access_token: str | None = None
        self._realm_id: str = ""
        self._base: str = _API_BASE["production"]

    async def __aenter__(self) -> QboClient:
        from app.db import run_db

        def read() -> tuple[str, str, str, dt.datetime | None]:
            with session_scope() as db:
                return (
                    sk.get_str(db, sk.QBO_REALM_ID),
                    sk.get_str(db, sk.QBO_ACCESS_TOKEN),
                    sk.get_str(db, sk.QBO_ENVIRONMENT),
                    _parse_dt(sk.get_str(db, sk.QBO_ACCESS_EXPIRES_AT)),
                )

        realm_id, access_token, environment, expires_at = await run_db(read)
        if not realm_id:
            raise QboNotConnected("No QuickBooks company is connected.")

        self._realm_id = realm_id
        self._base = _API_BASE.get(environment.strip().lower(), _API_BASE["production"])

        if not access_token or expires_at is None or expires_at - _ACCESS_SKEW <= utcnow():
            access_token = (await refresh_tokens()).access_token
        self._access_token = access_token

        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def realm_id(self) -> str:
        return self._realm_id

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        attempts: int = 4,
    ) -> dict[str, Any]:
        if self._client is None:
            raise QboError("QboClient used outside its async context manager")

        url = f"{self._base}/v3/company/{self._realm_id}{path}"
        last: Exception | None = None

        for attempt in range(attempts):
            response = await self._client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code == 401 and attempt == 0:
                # Access token died early; one refresh, then retry once.
                self._access_token = (await refresh_tokens()).access_token
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                delay = min(2 ** attempt, 30)
                logger.warning(
                    "QuickBooks returned %s, retrying in %ss", response.status_code, delay
                )
                await asyncio.sleep(delay)
                last = QboError(f"QuickBooks returned {response.status_code}")
                continue
            if response.status_code in (401, 403):
                raise QboAuthError(
                    f"QuickBooks rejected the request ({response.status_code}). The "
                    f"connection may need re-authorising. {response.text[:300]}"
                )
            if response.status_code >= 400:
                body = response.text[:500]
                if "stale object" in body.lower() or "5010" in body:
                    raise QboConflict(
                        "This transaction changed in QuickBooks since it was read."
                    )
                raise QboError(f"QuickBooks {response.status_code}: {body}")
            return dict(response.json()) if response.content else {}

        raise QboError(f"QuickBooks request failed after {attempts} attempts: {last}")

    async def query(self, statement: str) -> list[dict[str, Any]]:
        """Run a QuickBooks query and return the rows, whatever entity they are."""
        payload = await self._request("GET", "/query", params={"query": statement})
        response = payload.get("QueryResponse") or {}
        for key, value in response.items():
            if isinstance(value, list):
                logger.debug("QuickBooks query returned %d %s", len(value), key)
                return value
        return []

    async def company_name(self) -> str:
        payload = await self._request("GET", f"/companyinfo/{self._realm_id}")
        info = payload.get("CompanyInfo") or {}
        return str(info.get("CompanyName") or "")

    async def expense_accounts(self) -> list[dict[str, Any]]:
        """Active accounts a card charge could reasonably be categorised to.

        Deliberately broader than 'Expense': card spend legitimately lands on
        cost of goods sold and on fixed-asset accounts too, and an account the
        user cannot select is worse than one they ignore.
        """
        wanted = ("Expense", "Other Expense", "Cost of Goods Sold", "Fixed Asset")
        types = ", ".join(f"'{t}'" for t in wanted)
        return await self.query(
            f"SELECT * FROM Account WHERE Active = true AND AccountType IN ({types}) "
            "MAXRESULTS 1000"
        )
