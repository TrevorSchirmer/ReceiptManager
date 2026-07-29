"""Microsoft Graph mail client.

Auth is **app-only** (client credentials). The app registration should hold the
``Mail.Read`` *application* permission, scoped to a single mailbox with an
Exchange Online Application Access Policy:

    New-ApplicationAccessPolicy -AppId <client-id> -PolicyScopeGroupId <mailbox> \\
        -AccessRight RestrictAccess -Description "ReceiptManager"

Without that policy the credential can read every mailbox in the tenant, which is
far more authority than this app needs.

Change *polling* is used rather than change notifications: webhooks would demand a
publicly reachable HTTPS endpoint with a valid certificate plus a subscription
renewal job (they expire in under three days), to shave a handful of seconds off a
workflow measured in minutes.

Delta tokens can expire (HTTP 410). That is handled by discarding the cursor and
resyncing — harmless here, because ingest deduplicates on ``internetMessageId``,
so a full replay produces no duplicate transactions.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
import msal

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
_SCOPES = ["https://graph.microsoft.com/.default"]

# Delta returns only these; full bodies are fetched per-message so we never
# depend on $select semantics that differ between the delta and normal endpoints.
_DELTA_SELECT = "id,internetMessageId,subject,receivedDateTime,from"
_MESSAGE_SELECT = "id,internetMessageId,subject,receivedDateTime,from,body,bodyPreview"


class GraphError(RuntimeError):
    """Any non-retryable Graph failure."""


class GraphAuthError(GraphError):
    """Credential or consent problem — retrying will not help."""


@dataclass(slots=True)
class GraphMessage:
    graph_id: str
    internet_message_id: str
    subject: str
    sender: str
    received_at: dt.datetime
    body_html: str | None
    body_text: str | None


@dataclass(slots=True)
class DeltaResult:
    messages: list[GraphMessage] = field(default_factory=list)
    delta_link: str | None = None
    resynced: bool = False


@dataclass(slots=True)
class GraphCredentials:
    tenant_id: str
    client_id: str
    client_secret: str
    mailbox: str

    def validate(self) -> None:
        missing = [
            name for name in ("tenant_id", "client_id", "client_secret", "mailbox")
            if not getattr(self, name)
        ]
        if missing:
            raise GraphAuthError(f"Graph is not configured — missing: {', '.join(missing)}")


class GraphClient:
    """Thin async Graph wrapper.

    One instance per credential set; MSAL keeps its own token cache and refreshes
    a few minutes before expiry, so ``_token`` costs nothing on the hot path.
    """

    def __init__(self, creds: GraphCredentials, *, timeout: float = 30.0) -> None:
        creds.validate()
        self._creds = creds
        self._timeout = timeout
        self._app = msal.ConfidentialClientApplication(
            client_id=creds.client_id,
            client_credential=creds.client_secret,
            authority=f"https://login.microsoftonline.com/{creds.tenant_id}",
        )
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> GraphClient:
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ auth #

    async def _token(self) -> str:
        # MSAL's calls are blocking; keep them off the event loop so a slow token
        # endpoint cannot stall the Discord gateway heartbeat.
        result = await asyncio.to_thread(
            self._app.acquire_token_for_client, scopes=_SCOPES
        )
        if not isinstance(result, dict) or "access_token" not in result:
            desc = (result or {}).get("error_description", "no access_token returned")
            raise GraphAuthError(f"Graph authentication failed: {desc}")
        return str(result["access_token"])

    # ------------------------------------------------------------------ http #

    async def _request(
        self, method: str, url: str, *, params: dict[str, str] | None = None,
        attempts: int = 4,
    ) -> httpx.Response:
        if self._client is None:
            raise GraphError("GraphClient used outside its async context manager")

        full = url if url.startswith("http") else f"{GRAPH_ROOT}{url}"
        last: Exception | None = None

        for attempt in range(attempts):
            token = await self._token()
            try:
                response = await self._client.request(
                    method, full, params=params,
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                last = exc
                await asyncio.sleep(min(2 ** attempt, 30))
                continue

            if response.status_code in (429, 503, 504):
                # Graph tells us exactly how long to wait; respect it.
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                logger.warning("Graph throttled (%s), retrying in %.0fs", response.status_code, delay)
                await asyncio.sleep(min(delay, 60))
                last = GraphError(f"Graph returned {response.status_code}")
                continue

            if response.status_code in (401, 403):
                raise GraphAuthError(
                    f"Graph rejected the credential ({response.status_code}). Check the "
                    f"client secret, admin consent for Mail.Read, and that the "
                    f"Application Access Policy includes {self._creds.mailbox}. "
                    f"Response: {response.text[:400]}"
                )
            if response.status_code == 410:
                raise _DeltaExpired()
            if response.status_code >= 400:
                raise GraphError(f"Graph {response.status_code}: {response.text[:400]}")
            return response

        raise GraphError(f"Graph request failed after {attempts} attempts: {last}")

    # ----------------------------------------------------------------- calls #

    async def list_folders(self) -> list[dict[str, str]]:
        """Flat list of mail folders (including children) for the settings UI."""
        out: list[dict[str, str]] = []
        url = f"/users/{self._creds.mailbox}/mailFolders"
        params = {"$top": "200", "$select": "id,displayName,childFolderCount"}

        async def walk(folder_url: str, prefix: str) -> None:
            next_url: str | None = folder_url
            first = True
            while next_url:
                resp = await self._request("GET", next_url, params=params if first else None)
                first = False
                data = resp.json()
                for item in data.get("value", []):
                    name = f"{prefix}{item.get('displayName', '')}"
                    out.append({"id": item["id"], "name": name})
                    if int(item.get("childFolderCount") or 0) > 0:
                        await walk(
                            f"/users/{self._creds.mailbox}/mailFolders/{item['id']}/childFolders",
                            f"{name}/",
                        )
                next_url = data.get("@odata.nextLink")

        await walk(url, "")
        return sorted(out, key=lambda f: f["name"].lower())

    async def get_message(self, message_id: str) -> GraphMessage | None:
        url = f"/users/{self._creds.mailbox}/messages/{message_id}"
        try:
            resp = await self._request("GET", url, params={"$select": _MESSAGE_SELECT})
        except _DeltaExpired:  # not meaningful here
            return None
        except GraphError as exc:
            logger.warning("Could not fetch message %s: %s", message_id, exc)
            return None
        return _to_message(resp.json())

    async def delta(self, folder_id: str, delta_link: str | None) -> DeltaResult:
        """Fetch changes since ``delta_link``.

        Passing ``None`` performs a full sync of the folder. On a 410 the cursor
        is dropped and a full sync runs instead — safe, because ingest is
        idempotent on ``internetMessageId``.
        """
        resynced = False
        if delta_link:
            url: str = delta_link
            params: dict[str, str] | None = None
        else:
            url = f"/users/{self._creds.mailbox}/mailFolders/{folder_id}/messages/delta"
            params = {"$select": _DELTA_SELECT, "$top": "50"}

        ids: list[str] = []
        next_delta: str | None = None

        try:
            while True:
                resp = await self._request("GET", url, params=params)
                params = None
                data = resp.json()
                for item in data.get("value", []):
                    # Deletions arrive as @removed; we keep our own record.
                    if "@removed" in item:
                        continue
                    if item.get("id"):
                        ids.append(item["id"])
                if nxt := data.get("@odata.nextLink"):
                    url = nxt
                    continue
                next_delta = data.get("@odata.deltaLink")
                break
        except _DeltaExpired:
            if delta_link is None:
                raise GraphError(
                "Graph reported an expired delta on a full sync"
            ) from None
            logger.info("Graph delta cursor expired; resyncing folder %s", folder_id)
            result = await self.delta(folder_id, None)
            result.resynced = True
            return result

        messages: list[GraphMessage] = []
        for message_id in ids:
            message = await self.get_message(message_id)
            if message is not None:
                messages.append(message)

        messages.sort(key=lambda m: m.received_at)
        return DeltaResult(messages=messages, delta_link=next_delta, resynced=resynced)


class _DeltaExpired(Exception):
    """Internal: Graph returned 410 Gone for a delta cursor."""


def _to_message(data: dict[str, Any]) -> GraphMessage:
    body = data.get("body") or {}
    content = body.get("content")
    is_html = (body.get("contentType") or "").lower() == "html"

    sender = ""
    from_field = data.get("from") or {}
    if address := (from_field.get("emailAddress") or {}).get("address"):
        sender = address

    received_raw = data.get("receivedDateTime") or ""
    try:
        received = dt.datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
    except ValueError:
        received = dt.datetime.now(dt.UTC)
    if received.tzinfo is None:
        received = received.replace(tzinfo=dt.UTC)

    return GraphMessage(
        graph_id=data.get("id", ""),
        # Fall back to the Graph id so a message lacking a MIME id still has a
        # stable dedupe key.
        internet_message_id=data.get("internetMessageId") or f"graph:{data.get('id', '')}",
        subject=data.get("subject") or "",
        sender=sender,
        received_at=received.astimezone(dt.UTC),
        body_html=content if is_html else None,
        body_text=None if is_html else content,
    )
