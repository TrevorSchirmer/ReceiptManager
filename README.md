# ReceiptManager

Watches an Outlook folder for credit-card charge alerts, posts each charge to a
Discord channel, captures the receipt image someone replies with, and keeps the
whole thing as a searchable archive you can hand to an accountant.

Self-hosted, single process, SQLite. Runs in a Debian 12 unprivileged Proxmox LXC
and needs **no inbound ports** — both the Graph poller and the Discord gateway
dial outbound.

See [SCOPE.md](SCOPE.md) for the full design and the reasoning behind it.

---

## How it works

```
Outlook folder ──poll /delta 15s──► parse rules ──► Transaction #1042
                                                          │
                                                          ▼
                                            post to Discord channel
                                                          │
                                        human uploads a receipt image
                                                          ▼
                        reply-to? · #1042 in caption? · only one open? · else ask
                                                          ▼
              download ─► fsync ─► sha256 verify ─► commit ─► THEN delete messages
```

The last line is the important one. Discord attachment URLs are signed and
expiring, and a deleted message's attachment is gone permanently — so the bytes
are verified on disk before anything is deleted, and deletion runs as a separate
queued job that re-verifies first. A failed capture never deletes anything.

## Install

On a fresh Debian 12 LXC, with `/data` mounted as its own volume:

```bash
sudo bash deploy/install.sh
```

Then open `http://<container-ip>:8080/` and set an admin password.

## Configure

**Microsoft Graph** — app-only credentials with the `Mail.Read` *application*
permission. Scope it to a single mailbox so the credential cannot read the whole
tenant:

```powershell
New-ApplicationAccessPolicy -AppId <client-id> -PolicyScopeGroupId <mailbox> -AccessRight RestrictAccess -Description "ReceiptManager"
```

**Discord** — bot token and channel ID. Three things are easy to miss:

- Enable **`MESSAGE_CONTENT`** under Developer Portal → Bot → Privileged Gateway
  Intents, or the bot cannot read `#1042` codes from captions.
- Give the bot **Manage Messages** in the channel, or it cannot delete the
  uploaded receipt (it can always delete its own messages).
- Set the **allowed uploader IDs**. Without it, anyone in the channel can put
  receipts into your tax records.

**Parse rules** — a regex with named groups `merchant`, `amount`, `currency`,
`card_last4`, `cardholder`, `occurred_at`. Only `amount` is required. Rules run
against the *normalized plain text*, never the raw HTML. Use the live tester on
the Parse rules page with a real alert email; you will not get it right first try,
which is what **Re-parse stalled emails** is for.

Then hit **Simulate email** in Settings to push a fake charge through the whole
pipeline without spending money.

## Slash commands

| Command | |
|---|---|
| `/pending` | charges still awaiting a receipt |
| `/skip <code>` | mark as not needing a receipt |
| `/note <code> <text>` | append a note |
| `/cat <code> <category>` | set the accounting category |
| `/search <query>` | find by merchant, code, amount or note |

All replies are ephemeral, so querying state doesn't clutter the channel that
doubles as your receipt log. Mutating commands enforce the uploader allowlist —
a slash command can alter a financial record just as an upload can.

## Behaviour worth knowing

**Nothing is ever dropped.** If no rule matches, the charge is still created (as
`needs attention`, with the raw body kept) and still posted to Discord.

**Merchant auto-rules.** Charges matching a merchant pattern are filed
automatically and **never announced**. This is for recurring SaaS: without it the
bot nags monthly for a receipt that will never arrive, and that noise trains you
to ignore the channel — which is how genuine receipts start getting missed.

**Refunds** (negative amounts) link back to the charge they reverse, matched on
merchant and amount. A charge already claimed by one refund won't be claimed by
another. The link is a guess, so it's editable.

**Lapsing.** A charge with no receipt after 24h stops appearing in the picker and
stops being nudged. It stays fully visible, searchable and exportable, and can
still be claimed by an explicit `#1042` in Discord or attached from the UI —
lapsing suppresses *implicit* matching only.

**The digest runs before the lapse sweep**, so its "lapsing soon" warning is
actionable.

**Heartbeat.** If no email arrives within the configured window, the bot posts a
loud alert. Silent ingest failure is the worst-case bug here.

## Export

Export → date range → ZIP containing `transactions.csv`, every receipt named
`2026-07-28_1042_amazon-marketplace_1.jpg`, and a `MANIFEST.txt` that explicitly
names any receipt whose file is missing on disk. A partial export with an honest
manifest beats a hard failure.

## Backups

Nightly at 02:30 via cron into `/data/backups` (SQLite `.backup` + a receipts
tarball), kept 30 days.

Once receipts are deleted from Discord, this database and these files are the only
copy. A Proxmox snapshot of the same disk is not a backup — copy them off-box.

### `/data/secret.key`

A Fernet key generated on first boot, `chmod 600`. It encrypts exactly two things:
the **Graph client secret** and the **Discord bot token**. Nothing else — your
transactions, receipts and audit log are stored in the clear.

**Do this once:** `sudo cat /data/secret.key` and paste it into your password
manager. It is a single line.

It is deliberately **excluded from the nightly backup**, because it lives outside
the database precisely so a stolen backup does not also hand over those two
credentials. Set `RM_BACKUP_INCLUDE_KEY=1` if you would rather have one-step
restores and accept that trade-off.

**If you lose it:** annoying, not fatal. The app logs a clear error, you delete
the file, restart, and re-enter the client secret and bot token in Settings. No
transactions or receipts are affected.

**Restoring to a new container:** bring the key across too, or expect to re-enter
those two credentials.

## Accounts

One admin is created at first boot. Add more from **Account**, where you can also
turn on **TOTP two-factor** — worth doing the moment this is reachable beyond your
LAN. A session that has passed the password but not the code can reach nothing,
including receipt files.

The login form only asks for a username once a second account exists.

There is still no password reset. If every account's password is lost you need
shell access to the container.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
RM_DATA_DIR=./devdata .venv/bin/python -m uvicorn app.main:app --reload --port 8080
.venv/bin/python -m pytest tests/ -q
```

Migrations run automatically at startup. After changing a model:

```bash
RM_DATA_DIR=./devdata .venv/bin/alembic revision --autogenerate -m "what changed"
```

Review the generated file before committing — autogenerate does **not** add a
`server_default`, so adding a `NOT NULL` column will fail against a populated
database until you add one. `tests/test_migrations.py` fails the build if a model
drifts from its migration.

## Layout

| Path | |
|---|---|
| `app/models.py` | schema; `UTCDateTime` exists because SQLite drops tzinfo |
| `app/services/storage.py` | the capture-then-delete durability guarantee |
| `app/services/matching.py` | reply → `#code` → sole-open → ask |
| `app/services/parsing.py` | HTML→text, regex rules, amount parsing |
| `app/services/jobs.py` | transactional outbox with retry + dead-letter |
| `app/services/scheduler.py` | digest, lapse sweep, heartbeat |
| `app/discordbot/bot.py` | gateway, upload capture, the picker |
| `app/discordbot/tasks.py` | notify, finalize (delete), digest, alert |
| `app/discordbot/commands.py` | slash commands, ephemeral, allowlist-gated |
| `app/web/` | FastAPI routes; receipts served authenticated, never static |
| `app/web/routes_account.py` | TOTP enrollment, password change, users |
| `app/migrations/` | Alembic; custom types render as their DDL types |
| `deploy/` | install script, systemd unit, backup script |
