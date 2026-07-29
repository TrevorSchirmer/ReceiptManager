# Receipt Manager — Scope

Self-hosted tool to capture credit-card charge alerts from Outlook, chase receipts
in Discord, and store them as a searchable, exportable archive.

Status: **scope draft** — open decisions in §15.

---

## 1. Problem statement

Business credit card charges generate an email alert. Someone must supply a receipt image
for each charge. Today that pairing is manual and receipts go missing. This app automates
the chase and becomes the system of record for "charge → receipt".

The real deliverable is not the notification pipeline — it is **an auditable archive you can
hand to an accountant at year end with zero missing receipts.**

---

## 2. Core flow

```
Outlook folder
     │ Graph /delta poll, 15s
     ▼
Ingest ──► Parse (regex rules) ──► Transaction (status=new, code=#1042)
                                        │
                                        ▼
                          Post to Discord channel ──► message id recorded
                                        │
                              human uploads receipt image
                                        ▼
                                   Match reply
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
          reply-to bot message    #1042 in content    exactly 1 outstanding
                    └───────────────────┼───────────────────┘
                                        │  ambiguous (2+ outstanding)
                                        ▼
                          Bot posts select menu → user picks
                                        ▼
              ┌── DOWNLOAD ─► WRITE DISK ─► fsync ─► sha256 ─► COMMIT DB ──┐
              │                                                            │
              └──────────────── only after all succeed ────────────────────┘
                                        ▼
                     Delete receipt message + bot notification
                                        ▼
                     Post one-line confirmation (persists)
```

Every stage is retryable and idempotent. **Deletion is a separate queued job that runs only
after the file is durably stored and verified** — see §5.

---

## 3. Email ingest

### 3.1 Transport

**Graph `/delta` polling at 15s.** "Near instant" for a receipt workflow means seconds.
Polling avoids a public HTTPS endpoint, a valid cert, and the ≤3-day subscription renewal
cron that change-notifications require. Webhooks can be added later behind the same interface
if 15s ever proves too slow (it won't).

### 3.2 Auth

**App-only / client credentials** with `Mail.Read` application permission, scoped down with an
**Exchange Online Application Access Policy** so the app can read *only* the one mailbox.
Requires tenant admin consent — you have it. No refresh-token expiry to babysit.

Alternative (delegated + refresh token) avoids admin consent but dies on password change,
90-day inactivity, or a CA policy change, and needs a re-auth flow in the UI.

### 3.3 Filtering & dedupe

- Match on folder + sender + subject (configurable, substring or regex).
- Dedupe on `internetMessageId`, UNIQUE index. Delta queries redeliver.
- **Store the raw email always** (html, text, headers), even when parsing fails.

### 3.4 Parsing

- Ordered list of **parse rules**: name, sender match, subject match, regex with named capture
  groups `merchant`, `amount`, `currency`, `card_last4`, `cardholder`, `occurred_at`.
- HTML normalised to text first — strip tags, decode entities, collapse whitespace. Bank
  alerts are essentially always HTML.
- **Settings UI must have a live rule tester** — paste a sample email, see captures highlighted.
  Without it, tuning regexes is miserable.
- **Reparse** stored raw emails after a rule change. You'll need this the first week.
- Parse failure is not a dead end: create the transaction as `needs_attention` with the raw
  body and **still post to Discord**. Over-notify beats silently dropping a charge.
- Money as **integer minor units** + ISO currency code. Never floats.
- Timestamps stored UTC, displayed in a configured business timezone.

---

## 4. Discord

### 4.1 Why it's the right call

| | |
|---|---|
| Transport | Gateway WebSocket, dialled **outbound** — instant, no inbound ports, no tunnel |
| Groups | Channels, native |
| Interactivity | Select menus, buttons, modals, slash commands — solves ambiguous matching properly |
| Deletion | Bot can delete its own and (with `Manage Messages`) others' messages |
| Risk | Official API. No ToS grey area, no ban risk, no dedicated SIM |

### 4.2 Setup requirements

- Bot application + token (**encrypted at rest**, write-only in the UI).
- **`MESSAGE_CONTENT` privileged intent must be enabled** in the Developer Portal — required to
  read `#1042` codes from message text. Self-serve toggle under 100 servers, no verification.
- Channel permissions: `View Channel`, `Send Messages`, `Read Message History`,
  `Attach Files`, **`Manage Messages`** (required to delete the human's receipt message).
- **Authorized-uploader allowlist** (Discord user IDs). Without it, anyone in the channel can
  inject receipts into your tax records.

### 4.3 Outbound notification

```
#1042 · $43.21 USD · AMAZON MARKETPLACE
Card ••4417 · Jul 28, 2:14 PM

Reply to this message with the receipt, or upload with #1042 in the caption.
```

### 4.4 Inbound matching — priority order

1. **Reply to the bot's notification** (`message_reference`) → exact, no prompt.
2. **`#1042` in the message content** → exact, no prompt. *Works even for lapsed transactions
   (§7) — an explicit code is always an override.*
3. **Exactly one outstanding transaction** → auto-attach, confirm, no prompt.
4. **Two or more outstanding** → bot replies with a **string select menu**:

```
Which charge is this receipt for?
┌─────────────────────────────────────────┐
│ #1042 · AMAZON MARKETPLACE · $43.21     │
│ #1043 · UBER TRIP · $18.40              │
│ #1044 · STAPLES · $126.99               │
│ ── None of these / hold for later ──    │
└─────────────────────────────────────────┘
```

- Only outstanding, non-lapsed transactions listed. **Discord caps select menus at 25 options**
  — lapsing (§7) conveniently keeps this list short. If >25, paginate or fall back to `#code`.
- Only the original uploader (or an allowlisted user) may answer the menu.
- **Timeout** (default 15 min) → falls to the orphan queue with a nudge.
- Multiple images in one message → one prompt, all attached to the same transaction
  (multi-page receipt).
- Zero outstanding → orphan queue, bot offers a select of recent transactions (last 7 days,
  including already-receipted ones — could be a second page).

### 4.5 Slash commands

| Command | Effect |
|---|---|
| `/pending` | list outstanding transactions (ephemeral) |
| `/skip <code>` | mark `no_receipt_required` |
| `/note <code> <text>` | append a note |
| `/cat <code> <category>` | set GL category |
| `/search <query>` | find a transaction |

Ephemeral replies keep the channel clean.

### 4.6 Limits to design around

- **Attachment size**: ~10 MB on the free tier. Normal phone photos (2–4 MB) are fine; 48 MP
  ProRAW is not. Discord rejects client-side — surface guidance in the digest.
- **Delete rate limits** are per-channel. Queue deletions, don't fire them inline.
- **Bulk delete** only works on messages <14 days old. Irrelevant here (deletion is prompt) but
  matters for any cleanup tooling.
- **Interaction tokens expire after 15 minutes** — the select-menu timeout must be ≤ that.

---

## 5. Deletion — capture-then-delete ordering

> **The single most dangerous operation in this app.** Discord CDN attachment URLs are signed
> and expire, and a deleted message's attachment is gone permanently. Deleting before the file
> is durably stored destroys a tax record with no recovery path.

Hard-ordered sequence. **Deletion is a separate queued job, never inline in the message handler.**

1. Receive message with attachment
2. Download bytes to a temp path
3. Verify byte count matches the reported attachment size
4. Write to final path, **`fsync`**
5. Compute sha256, **read back and verify**
6. Commit `attachments` row + transaction link in a single DB transaction
7. **Enqueue** delete job (receipt message + bot notification)
8. Post the persistent one-line confirmation

Any failure before step 6 → retry with backoff, **nothing is deleted**, error surfaced in the
UI health page. A failed delete at step 7 is cosmetic and retried separately; a failed
*download* is data loss and must be loud.

**What stays in the channel:** a single confirmation line, so the channel remains a readable log:

```
✅ #1042 · AMAZON MARKETPLACE · $43.21 · receipt stored
```

Configurable: keep confirmations forever (default), or auto-delete after N minutes for a
fully clean channel. Note that deleting from Discord makes **the app DB the only record** —
which makes §10 backups non-optional.

---

## 6. Storage

- Files on disk, not in the DB: `/data/receipts/YYYY/MM/<uuid>.<ext>`
- **sha256 per file** — dedupe re-sends of the same photo.
- **HEIC → JPEG conversion.** iPhones send HEIC, browsers can't render it. This *will* bite you.
- **EXIF auto-orient**, or half your receipts display sideways.
- **Thumbnails** for the grid; PDFs get a first-page render (`pdftoppm`).
- **PDF receipts**, not just images — they arrive constantly.
- MIME sniffing (don't trust the declared type), size cap, extension allowlist.
- **OCR (tesseract)** → searchable text + cross-check the OCR'd total against the charge
  amount and flag mismatches. Phase 3.
- **Retention: never auto-delete.** Tax records are 7 years.

---

## 7. Transaction lifecycle & lapsing

```
new ──► notified ──► receipt_attached ──► verified
             │
             ├──► lapsed            (>24h, no receipt — swept nightly)
             ├──► no_receipt_required  (/skip, or merchant allowlist)
             └──► needs_attention   (parse failed)
```

### Lapsing rules

A transaction with no receipt after **24 h** (configurable) from `notified_at` becomes `lapsed`:

- ❌ Excluded from the ambiguity select menu
- ❌ Excluded from "exactly one outstanding" auto-match
- ❌ No further nudges or digest chasing
- ✅ **Still fully visible, searchable, and exportable in the UI**
- ✅ **Still manually attachable** from the UI, always
- ✅ **Still matchable in Discord by explicit `#1042`** — you will find a receipt in your wallet
  three days later, and the system must accept it. Lapsing suppresses *implicit* matching only.

### Daily digest

Runs at a configured local time, posted to the channel (or DM):

```
📋 Outstanding receipts — 3

⚠️  Lapsing tonight
    #1042 · AMAZON MARKETPLACE · $43.21 · Jul 27

    #1051 · UBER TRIP · $18.40 · Jul 28
    #1052 · STAPLES · $126.99 · Jul 28

Lapsed in the last 7 days: 4  ·  view: https://receipts.lan/lapsed
```

**Digest runs before the nightly lapse sweep**, so it can warn about imminent lapses — that
warning is the last chance to act, and it's the whole reason the ordering matters. The lapsed
count keeps the failure mode visible instead of letting items vanish silently.

---

## 8. Data model (SQLite)

| Table | Notes |
|---|---|
| `users` | single admin initially; still a table |
| `sessions` | server-side, revocable |
| `settings` | key/value; secret values encrypted at rest |
| `mail_accounts` | tenant/client id, token blob, folder id |
| `parse_rules` | ordered, enable/disable, regex + field map |
| `emails_raw` | `internet_message_id` UNIQUE, headers, html, text, received_at, processed_at |
| `transactions` | `short_code` UNIQUE, email_id, occurred_at, merchant, `amount_minor`, currency, `card_last4`, cardholder, status, category, notes, notified_at, lapsed_at |
| `attachments` | `transaction_id` **NULLABLE** (= orphan queue), path, sha256, mime, bytes, thumb_path, ocr_text, discord_message_id, uploader_id, received_at |
| `discord_messages` | outbound + inbound audit — required for matching *and* debugging |
| `jobs` | outbox/queue: type, payload, attempts, next_run_at, last_error |
| `audit_log` | who changed what, when |

---

## 9. Reliability

- **Outbox pattern** — persist intent-to-send before sending; a crash never loses a notification.
- **Idempotency keys** on email ids and Discord message ids.
- **Retry with backoff** + dead-letter visible in the UI. Failures must be *loud*.
- **Heartbeat / dead-man's switch** — no email in X hours, or gateway disconnected → alert
  (banner + Discord ping). The dangerous failure is silent: ingest dies, you notice three weeks
  and one tax season later.
- **Gateway reconnect** with backoff; log disconnects; surface uptime on the dashboard.
- **Connection health** on the dashboard: Outlook ✓ (polled 8s ago), Discord ✓ (gateway up 4d).
- **"Simulate email" button** — inject a fake message through the whole pipeline. Invaluable
  during development and after every config change.

---

## 10. Security

"Local only" is not a security boundary — you're storing OAuth tokens, a Discord bot token, and
financial records.

- Admin password → **argon2id**. First-boot setup, then login.
- Session cookies `httpOnly` / `SameSite=Lax` / `Secure`; server-side, revocable.
- Login rate limiting + lockout. **CSRF tokens** on state-changing requests.
- **Secrets encrypted at rest** with a key outside the DB (env/file, `chmod 600`). Write-only
  in the UI — render `••••••`, never echo back.
- **Receipt files served through an authenticated route.** Never expose `/data` as static.
- **HTTPS** via Caddy in the container, or front with Tailscale.
- **Discord uploader allowlist** (§4.2) — the app's weakest trust boundary.
- Optional TOTP 2FA.
- **Backups**: nightly `sqlite3 .backup` + files tarball, retained off-box. Non-negotiable —
  once Discord messages are deleted, this DB is the only copy.

---

## 11. UI

**First-run wizard** → admin password → connect Outlook → pick folder → first parse rule (with
tester) → Discord bot token + channel + uploader allowlist → done.

- **Dashboard** — outstanding count, MTD spend, recent activity, connection health.
- **Transactions** — search (merchant, amount range, dates, card, status, OCR text), filter,
  sort, paginate, bulk actions.
- **Transaction detail** — parsed fields, raw email, receipts (lightbox), activity timeline,
  manual attach/detach, edit, notes, category.
- **Lapsed** — dedicated view; manual attach always available.
- **Orphan receipts** — unmatched uploads awaiting assignment.
- **Settings** — mail, parse rules + tester, Discord, lapse window, digest time, timezone/
  currency, backup, users.
- **Health & logs** — queue depth, last poll, gateway uptime, recent errors, retry buttons.
- **Export** — date range → CSV + ZIP of receipts, filenames keyed to CSV rows. Optional
  per-month PDF contact sheet. **This is what you hand your accountant.**

Must be **mobile-responsive**.

---

## 12. Deployment (Proxmox LXC)

- Debian 12 **unprivileged** LXC. 2 vCPU / 2 GB RAM / 20 GB+ disk (grows with receipts).
  No Chromium needed — Discord is a plain WebSocket.
- **Zero inbound ports required.** Graph polling is outbound; the Discord gateway is outbound.
  Only the web UI listens, on the LAN.
- Single process + SQLite = trivial deploy. No Postgres, no Redis, no Docker-in-LXC nesting.
- `systemd` unit, `Restart=always`, journald + logrotate.
- `/data` on a dedicated mount so it survives a container rebuild.
- Proxmox snapshot + PBS backup.
- Container timezone + NTP correct — timestamps matter here.
- **One-line install script**: create user, install runtime, fetch release, write unit, start,
  print the setup URL.

---

## 13. Stack

Dropping WhatsApp removes the constraint that forced Node — `discord.py` is first class, so
**Python is now fully viable and matches your existing Apollo Hub stack.**

| Layer | Python (recommended) | Node alternative |
|---|---|---|
| Runtime | Python 3.12 | Node 22 + TypeScript |
| Web | FastAPI + uvicorn | Fastify |
| Discord | discord.py | discord.js |
| Graph | msal + httpx | @azure/msal-node |
| DB | SQLite (WAL) + SQLAlchemy/Alembic | SQLite + Drizzle |
| Images | Pillow + pillow-heif, pytesseract, poppler | sharp, tesseract.js |

Both discord.py and FastAPI are asyncio-native, so the gateway client and the web server run in
**one event loop, one process, one systemd unit**. Jobs run on the `jobs` table in-process — no
Redis, survives restart.

Frontend: server-rendered Jinja + HTMX is meaningfully less code at this size than a React SPA.

---

## 14. Remaining gaps worth building

1. **Statement reconciliation (v2)** — import the card issuer's CSV, diff against captured
   transactions. The only thing that catches a charge whose alert email never arrived.
2. **Refunds / negative amounts** — link a refund to its original charge.
3. **`no_receipt_required` merchant rules** — allowlist recurring SaaS so it stops nagging.
4. **Amount drift** — the email alert is the *authorization*; the posted amount differs with
   tips and FX. Keep `amount_final` for reconciliation.
5. **Categories / GL codes** — one field, large accounting payoff.
6. **Multiple cardholders** — scope says "my card"; cheap to model now, painful to retrofit.
7. **Audit log** — it's a financial record.

---

## 15. Phasing

**Phase 1 — pipeline, usable end to end**
Auth + first-run wizard · Graph polling + dedupe · parse rules + tester · transactions ·
Discord gateway + notify · matching (reply / `#code` / single-outstanding / **select menu**) ·
**capture-then-delete** · transaction list, detail, search

**Phase 2 — trustworthy**
Lapsing + nightly sweep · daily digest · heartbeat + health page · orphan queue ·
retries/dead-letter · **export CSV + ZIP** · backups · HEIC/PDF/thumbs · reparse

**Phase 3 — polish** ✅ *(delivered, minus two items dropped by decision)*
slash commands · merchant auto-rules · categories · refund linking · multi-user · TOTP

**Dropped deliberately.** *OCR* — its value was cross-checking an OCR'd total against
the charge amount, which at this volume is faster to eyeball than to tune, and it puts a
slow dependency in the capture path. *Statement reconciliation* — earns its keep at
hundreds of charges a month, not tens.

---

## 16. Open decisions

| # | Decision | Recommendation |
|---|---|---|
| 1 | Stack: **Python/FastAPI** or Node/TS? | Python — matches Apollo Hub, discord.py is excellent |
| 2 | Graph **app-only** (admin consent + access policy) or delegated refresh token? | App-only |
| 3 | Frontend: **Jinja + HTMX** or React SPA? | HTMX at this size |
| 4 | Lapse window — 24 h from `notified_at`, or end-of-next-day? | 24 h configurable, default 24 |
| 5 | Keep confirmation lines in the channel, or auto-delete those too? | Keep — free audit log |
| 6 | Expose beyond LAN via Tailscale, or strictly local? | Tailscale — you'll want phone access |
| 7 | Digest to channel or DM? | Channel — shared accountability |
| 8 | Single cardholder or model multi-user now? | Model now, ship single |
