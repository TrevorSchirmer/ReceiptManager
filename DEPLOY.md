# Deployment checklist

Everything between a clean Proxmox host and a working receipt pipeline. Roughly
an hour, most of it waiting on Azure and Discord consoles rather than on the app.

Work top to bottom — steps 2 and 3 gather credentials that step 6 needs.

---

## 1. Provision the LXC

Debian 12, unprivileged, with `/data` as its own mount so a container rebuild
does not take your receipts with it.

```bash
pct create 110 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname receipts \
  --cores 2 --memory 2048 --swap 512 \
  --rootfs local-lvm:8 \
  --mp0 local-lvm:20,mp=/data \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 --onboot 1 --start 1
```

20 GB for `/data` holds roughly 5,000–10,000 phone photos. Grow it later with
`pct resize 110 mp0 +20G`.

Set the timezone — every timestamp in your export depends on it:

```bash
pct exec 110 -- timedatectl set-timezone America/New_York
```

---

## 2. Microsoft Graph (app-only)

In **portal.azure.com → Microsoft Entra ID → App registrations → New registration**:

- Name it, choose **single tenant**, leave the redirect URI blank.
- Copy the **Application (client) ID** and **Directory (tenant) ID**.

**Certificates & secrets → New client secret.** Copy the *Value* immediately —
it is never shown again. **Note the expiry date; put it in your calendar.** When
it lapses, ingest stops and Health shows a Graph auth error.

**API permissions → Add a permission → Microsoft Graph → _Application_
permissions → `Mail.Read`.** Application, not Delegated. Then **Grant admin
consent**.

That credential can now read *every* mailbox in the tenant, which is far more
authority than this app needs. Scope it to one mailbox:

```powershell
Install-Module -Name ExchangeOnlineManagement
Connect-ExchangeOnline -UserPrincipalName you@yourdomain.com

New-ApplicationAccessPolicy `
  -AppId <client-id> `
  -PolicyScopeGroupId receipts@yourdomain.com `
  -AccessRight RestrictAccess `
  -Description "ReceiptManager"

# Verify — this must report AccessCheckResult: Granted
Test-ApplicationAccessPolicy -Identity receipts@yourdomain.com -AppId <client-id>
```

Also confirm it is now *denied* for a different mailbox. A policy that silently
failed to apply looks identical to one that worked until you test the negative
case.

> If `New-ApplicationAccessPolicy` is unavailable in your tenant, Microsoft's
> newer equivalent is RBAC for Applications — same goal, different cmdlets.

**You now have:** tenant ID, client ID, client secret, mailbox address.

---

## 3. Discord

**discord.com/developers/applications → New Application.**

- **Bot → Reset Token**, copy it.
- **Bot → Privileged Gateway Intents → enable `MESSAGE CONTENT`.** Without this
  the gateway delivers empty message bodies and `#1042` codes are invisible.
  Self-serve under 100 servers.

Invite it with exactly the permissions it needs (View Channel, Send Messages,
Manage Messages, Attach Files, Read Message History):

```
https://discord.com/api/oauth2/authorize?client_id=<APP_ID>&permissions=109568&scope=bot%20applications.commands
```

`applications.commands` is required or the slash commands never appear.

**Manage Messages** is what lets the bot delete your uploaded receipt. Without
it, capture still works and the file is still stored — the message just stays in
the channel and the log says so.

### Getting the channel ID and your user ID

Discord keeps moving Developer Mode, so use these instead — neither needs it.

**Channel ID — read it off the URL.** Open Discord in a browser and click the
channel. The address bar reads:

```
https://discord.com/channels/<SERVER_ID>/<CHANNEL_ID>
```

The second number is the channel ID.

**Your user ID — let the bot tell you.** You do not need it to get started: leave
**Allowed uploader IDs** blank at first (the app then accepts anyone in the
channel), finish setup, and run **`/whoami`** in the channel. It replies
privately with both your user ID and the channel ID. Paste the user ID into
Settings and save.

If someone is wrongly blocked later, the bot's rejection message includes the ID
to add.

<details>
<summary>If you would rather use Developer Mode</summary>

It has lived in a few places depending on your Discord version — look for
**Advanced** in User Settings (older builds), or under **App Settings** in the
current layout. Once on, right-click a channel or a username → *Copy ID*. On
mobile it is under Settings → Advanced.

</details>

**You now have:** bot token, channel ID. (User ID comes from `/whoami` after setup.)

---

## 4. Outlook folder and rule

Create a folder (e.g. `Card Alerts`) and a server-side rule moving your card
alerts into it. Match on sender — `americanexpress@welcome.americanexpress.com`
for Amex.

Make it a **server-side rule** (created in Outlook on the web, or without the
"on this computer only" flag), otherwise it only runs when your desktop Outlook
is open and the app will see nothing.

---

## 5. Install

```bash
git clone https://github.com/TrevorSchirmer/ReceiptManager.git
cd ReceiptManager
sudo bash deploy/install.sh
```

Needs outbound internet: PyPI for dependencies, unpkg for HTMX. The HTMX fetch
is non-fatal — only the parse-rule tester's live preview uses it.

---

## 6. Configure

Open `http://<container-ip>:8080/`, set an admin password (12+ characters, no
reset — put it in your password manager), then in **Settings**:

**Microsoft Graph** — tenant ID, client ID, client secret, mailbox. Save, then
click **Look up mail folders** and pick `Card Alerts`. Save again.

**Discord** — bot token and channel ID.

**Allowed uploader IDs** is a second gate on top of Discord's own channel
permissions. Leave it blank if the channel is already restricted to the right
people — the channel is then the control, and every upload still records who
sent it. Fill it in only when the channel is broader than the set of people who
should be touching financial records. `/whoami` gives you an ID.

**Workflow** — set the **timezone** and default currency. Every export date
depends on the timezone.

**Parse rules** — add the Amex rule (sender contains `americanexpress.com`,
subject contains `Large Purchase`):

```regex
Account Ending:\s*(?P<card_ending>\d+).*?\n(?P<merchant>[^\n]{2,60})\n+\$(?P<amount>[\d,]+\.\d{2})\*?\n+(?P<occurred_at>[A-Za-z]{3},\s+[A-Za-z]{3}\s+\d{1,2},\s+\d{4})
```

Paste a real alert into the tester before saving.

---

## 7. Verify

Do this before trusting it with a real charge.

1. **Health** — Outlook *Polling*, Discord *Connected*. If not, the page says why.
2. **Settings → Simulate email** — the default sample creates a charge and posts
   it to Discord within a few seconds.
3. **Reply to that Discord message with any photo.** Within a second or two you
   should see a `✅` confirmation, and both the notification and your upload
   should disappear.
4. **Transactions** — the charge shows `receipt attached` with a thumbnail.
5. **`/whoami` in Discord** — replies privately with your user ID; paste it
   into **Allowed uploader IDs** and save. Then **`/pending`** to confirm
   commands still work once the allowlist is enforced.
6. **Export** — pull a ZIP and confirm the CSV and receipt filenames line up.
7. **Spend a real dollar** on the card and confirm the alert flows end to end.

---

## 8. Harden

**Reachability.** The app serves plain HTTP and binds `0.0.0.0`. Pick one:

- **Tailscale (recommended).** Encrypted device-to-device, works from your phone,
  no ports forwarded, and `tailscale serve` gives a real trusted certificate:
  ```bash
  curl -fsSL https://tailscale.com/install.sh | sh
  tailscale up
  tailscale serve --bg 8080
  ```
- **Caddy on the LAN.** Use `deploy/Caddyfile` — internal CA, so you must trust
  its root certificate on each device.

Behind either, add `Environment=RM_SECURE_COOKIES=true` to
`/etc/systemd/system/receiptmanager.service` and restart, so the session cookie
is never sent in the clear.

**Two-factor.** Account → set up TOTP. Worth doing the moment this is reachable
beyond the LAN.

**Backups off-box.** `/data/backups` on the same disk is not a backup. Add the
container to Proxmox Backup Server, or rsync the directory elsewhere nightly.

**`/data/secret.key`** — `sudo cat` it and store it in your password manager. It
is deliberately excluded from the nightly backup. Losing it costs you re-entering
two credentials; leaking it costs you the Graph token and the bot token.

---

## 9. Ongoing

| When | What |
|---|---|
| **Client secret expiry** | Calendar reminder. It will expire, ingest will stop, and Health will say so. |
| A charge lands as *needs attention* | Fix the regex in the tester, then **Re-parse stalled emails**. |
| A merchant nags monthly | Add a merchant auto-rule so it files itself and stops announcing. |
| Quarterly | Restore a backup into a throwaway container. An untested backup is a hope. |
| Year end | Export the date range and hand over the ZIP. |

---

## Known limitations

- **Discord caps uploads at ~10 MB** on the free tier. Normal phone photos
  (2–4 MB) are fine; 48 MP ProRAW is not. Discord rejects it client-side, so the
  app never sees it.
- **The receipt picker shows at most 25 charges** — a Discord limit. Beyond that,
  reply directly or use `#code`.
- **Only one mailbox** is monitored.
- **Amounts are pre-authorisations.** Amex alerts carry a `*` because the posted
  amount can differ (tips, FX). The `final_amount` column exists for that; it is
  filled in by hand today.
