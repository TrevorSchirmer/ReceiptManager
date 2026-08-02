# Deployment checklist

Everything between a clean Proxmox host and a working receipt pipeline. Roughly
an hour, most of it waiting on Azure and Discord consoles rather than on the app.

Work top to bottom — steps 2 and 3 gather credentials that step 6 needs.

---

## 1. Provision the LXC

Debian 12 or 13, unprivileged, with `/data` as its own mount so a container
rebuild does not take your receipts with it.

Download the template first — a fresh Proxmox host has none, and the exact
version string changes over time, so list rather than guess:

```bash
pveam update
pveam available --section system | grep debian-1
pveam download local <exact-name-from-that-list>
```

Debian 12 ships Python 3.11 and Debian 13 ships 3.13; both work. Prefer 13 for
the longer support window.

```bash
pct create 110 local:vztmpl/<exact-name-from-that-list> \
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

## 4. Card alerts, Outlook folder, and rule

### First: check the alert threshold at the card issuer

This decides whether the system sees everything or only part of it. Amex's
"Large Purchase Approved" alert ships with a **$50 threshold** — the email itself
says so:

> *we're letting you know that this purchase was more than $50.00. You can change
> the dollar amount of these large purchase notifications online.*

Below that, no email is sent at all: nothing reaches Outlook, nothing reaches
this app, and no receipt is ever chased. Small charges are exactly the ones that
go missing at tax time.

In **Amex → Account Services → Email & Push Notifications**, lower the threshold
to the minimum, or switch to an all-transactions alert if the issuer offers one
(a different subject line just means a second parse rule). If the threshold will
not go to zero, you have a known blind spot — decide deliberately whether that is
acceptable.

### Then the folder and rule

Create a folder (e.g. `Card Alerts`) and a rule moving alerts into it. Match on
**sender plus a subject keyword**, not sender alone — issuers send marketing from
the same domain, and every promo landing in the folder becomes a
`needs attention` charge announced in Discord.

For Amex: from `AmericanExpress@welcome.americanexpress.com`, subject contains
`Approved`.

Make it a **server-side rule** — build it in Outlook on the web, or leave the
"on this computer only" flag off. A desktop rule only runs while Outlook is open,
and the app would see nothing for days at a time.

Marking the mail read in the rule is fine. Graph reports that as a change and
re-delivers the message, but ingest deduplicates on `internetMessageId`, so the
second delivery is a no-op. It does remove the unread pile as a manual "ingest
has stopped" signal, which makes the heartbeat in step 6 your backstop.

A second filter layer lives in the app (Settings → *Only process mail from* /
*subjects containing*), so extra mail slipping into the folder can be excluded
without touching Outlook.

---

## 5. Install

In the container's console (Proxmox web UI → the container → **Console**), one line:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/TrevorSchirmer/ReceiptManager/main/deploy/install.sh)"
```

The script clones itself, so there is nothing to fetch first. Run it **inside the
container**, not on the Proxmox node — it refuses if it detects it is on the host.

From a checkout instead:

```bash
git clone https://github.com/TrevorSchirmer/ReceiptManager.git /opt/src
bash /opt/src/deploy/install.sh
```

Needs outbound internet: GitHub, PyPI, and unpkg for HTMX. The HTMX fetch is
non-fatal — only the parse-rule tester's live preview uses it.

The installer is safe to re-run: the service user, directories, venv and systemd
unit are all created idempotently.

**If the service account cannot write to `/data`**, the mountpoint is owned by
the host rather than by the container's mapped root. Fix it from the Proxmox
host, where an unprivileged container's root is uid 100000:

```bash
pct stop <vmid>
chown -R 100000:100000 /path/to/mountpoint   # see: pct config <vmid>
pct start <vmid>
```

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

## 7b. QuickBooks Online (optional)

Phase one only: connect the company and pull the chart of accounts, so categories
become real QuickBooks accounts instead of free text. Matching and write-back come
later.

**HTTPS is a prerequisite** — Intuit requires an https redirect URI for
production keys. See §8.

At **developer.intuit.com → My Apps → Create an app** (QuickBooks Online
Accounting):

- Copy the **Client ID** and **Client Secret** from the *Development* or
  *Production* keys tab, matching whichever `Environment` you set in Settings.
- Under **Redirect URIs**, add `https://<your-host>/qbo/callback` exactly as it
  will appear in the browser.

The redirect is a **browser** redirect — Intuit never connects to it — so an
internal-only hostname is fine, provided the browser doing the authorisation can
reach it.

Then in **Settings → QuickBooks Online**: paste the client ID and secret, set the
environment and redirect URI, **Save**, then **Connect to QuickBooks**. After
authorising you should see the company name and a cached account count.

Start against a **sandbox** company — the flow and account shapes are identical
and mistakes cost nothing.

Health shows when the authorisation expires. Intuit's refresh token lapses after
about 100 days of non-use, and once it does the connection simply stops.

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

### Internal-only HTTPS behind Nginx Proxy Manager

You can have a real, publicly-trusted certificate for a hostname that is never
reachable from the internet. The trick is **DNS-01 validation**: Let's Encrypt
proves you own the name via a TXT record and never connects to your server, so
no inbound port is required and the name need not resolve publicly at all.

Keeping it internal takes **two independent controls**. Do both — the first alone
is not a security boundary.

**1. DNS that only answers internally.** On your internal resolver (Pi-hole,
AdGuard, pfSense, UniFi), point `receipts.example.com` at the NPM host's LAN
address. Create no public A record. Split-horizon like this keeps your internal
addressing out of public DNS entirely.

*(A public A record containing an RFC1918 address also works and is simpler, but
it publishes your internal layout, and resolvers with DNS-rebinding protection —
including Pi-hole and many routers — will discard the answer.)*

**2. An NPM Access List, which is the control that actually enforces it.** Your
NPM already answers on 80/443 from the internet, so anyone who learns the
hostname can reach the service by sending a `Host:` header straight to your
public IP. DNS does not stop that; an allow-list does.

In NPM → **Access Lists → Add**:

| | |
|---|---|
| Satisfy | Any |
| Authorization | leave empty |
| Access | `allow 192.168.0.0/16`, `allow 10.0.0.0/8`, `allow 172.16.0.0/12`, then `deny all` |

Then **SSL Certificates → Add → Let's Encrypt**, tick **Use a DNS Challenge**,
choose your DNS provider and supply an API token. A wildcard (`*.example.com`)
requires DNS-01 anyway and covers future internal hosts in one certificate.

Then **Proxy Hosts → Add**:

- Domain: `receipts.example.com`
- Forward to: `http` → the container's IP → `8080`
- **Access List:** the one created above ← the step that does the enforcing
- SSL tab: the DNS-01 certificate, **Force SSL**, HTTP/2, HSTS
- Advanced tab:
  ```nginx
  client_max_body_size 32m;
  proxy_read_timeout 300s;   # large ZIP exports
  ```

Finally, tell the app it is behind a proxy — add to
`/etc/systemd/system/receiptmanager.service`:

```ini
Environment=RM_BASE_URL=https://receipts.example.com
Environment=RM_SECURE_COOKIES=true
Environment=RM_FORWARDED_ALLOW_IPS=<NPM's IP>
```

```bash
systemctl daemon-reload && systemctl restart receiptmanager
```

`RM_SECURE_COOKIES=true` marks the session cookie Secure, so **plain-HTTP access
can no longer log in** — the browser withholds the cookie. That is the point, but
it does mean `http://<container-ip>:8080` stops working as a fallback. Leave that
port reachable on the LAN for recovery, or be ready to unset the variable.

`RM_FORWARDED_ALLOW_IPS` should name the proxy. Left at `*`, anything that can
reach port 8080 directly can forge its client IP in the logs and in login
throttling.

**Most robust variant:** run a *second* NPM instance that is not port-forwarded
at all, and put internal services behind that one. Then no combination of DNS
and `Host` headers can reach them from outside, because nothing is listening.

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
