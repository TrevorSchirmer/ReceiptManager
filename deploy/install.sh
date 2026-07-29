#!/usr/bin/env bash
#
# ReceiptManager installer for a Debian 12/13 unprivileged Proxmox LXC.
#
# Creates a service user, installs into /opt/receiptmanager with a venv, puts all
# mutable state under /data (mount that separately so a container rebuild does
# not destroy your receipts), and installs a systemd unit plus a nightly backup.
#
# From a checkout:
#   bash deploy/install.sh
#
# Or standalone, with no checkout — it clones itself. Short enough to paste into
# a Proxmox web console, which is the point:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/TrevorSchirmer/ReceiptManager/main/deploy/install.sh)"
#
set -euo pipefail

APP_USER=receiptmanager
APP_DIR=/opt/receiptmanager
DATA_DIR=${RM_DATA_DIR:-/data}
PORT=${RM_PORT:-8080}
REPO_URL=${RM_REPO_URL:-https://github.com/TrevorSchirmer/ReceiptManager.git}
REPO_REF=${RM_REPO_REF:-main}

# Resolved after the packages are installed: piped through curl there is no
# BASH_SOURCE to derive a checkout from, so one is cloned instead.
REPO_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (or with sudo)."

# Refuse to install onto the hypervisor itself. This is meant to run *inside* the
# container; on the Proxmox host it would scatter a service user, a systemd unit
# and a cron job across the node.
if [[ -d /etc/pve ]] \
   && ! systemd-detect-virt --container --quiet 2>/dev/null \
   && [[ "${RM_ALLOW_HOST_INSTALL:-0}" != "1" ]]; then
    die "This looks like the Proxmox host, not a container.
  Run it inside the LXC instead:
    pct exec <vmid> -- bash /opt/src/deploy/install.sh
  Or set RM_ALLOW_HOST_INSTALL=1 if you really mean to install on this node."
fi

log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# Only Python, curl and sqlite3 are needed — Pillow, pillow-heif and pypdfium2
# all ship binary wheels, so there is no libheif/poppler/imagemagick to chase.
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-dev \
    ca-certificates curl git sqlite3 tzdata

# Resolve the source. A checkout beside this script wins; otherwise clone, which
# is what happens when the script is piped straight from curl.
if [[ -z "$REPO_DIR" || ! -d "$REPO_DIR/app" ]]; then
    REPO_DIR="$(mktemp -d)"
    log "No local checkout — cloning ${REPO_URL} (${REPO_REF})"
    git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$REPO_DIR" 2>&1 | sed 's/^/    /'
    trap 'rm -rf "$REPO_DIR"' EXIT
fi
[[ -d "$REPO_DIR/app" ]] || die "No application source found at ${REPO_DIR}."
log "Installing from ${REPO_DIR}"

log "Creating service user ${APP_USER}"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

log "Creating ${DATA_DIR}"
mkdir -p "$DATA_DIR"/{receipts,thumbs,tmp,backups}

# Chown only the directories this app owns, never the mount root recursively.
#
# A freshly formatted ext4 volume — which is what an LXC mountpoint is — contains
# a lost+found owned by host uid 0. In an unprivileged container that maps to
# nobody, so `chown -R` over the mount root fails on it and aborts the install.
# The app has no business owning lost+found anyway.
for sub in receipts thumbs tmp backups; do
    chown -R "$APP_USER:$APP_USER" "$DATA_DIR/$sub"
done

# The mount root itself may be owned by the host; the app only needs to traverse
# and write inside it, so a failure here is not fatal.
chown "$APP_USER:$APP_USER" "$DATA_DIR" 2>/dev/null \
    || warn "Could not chown ${DATA_DIR} itself (normal for an LXC mountpoint) — continuing."
chmod 750 "$DATA_DIR" 2>/dev/null || true

# Prove the service account can actually write there before going further; a
# failure at this point is far cheaper to diagnose than one at first boot.
if ! runuser -u "$APP_USER" -- test -w "$DATA_DIR"; then
    die "${APP_USER} cannot write to ${DATA_DIR}. Check the mountpoint ownership:
    ls -land ${DATA_DIR}
  On an unprivileged LXC, run this on the Proxmox host:
    chown -R 100000:100000 /path/to/the/mountpoint/on/the/host"
fi

log "Installing application into ${APP_DIR}"
mkdir -p "$APP_DIR"
if [[ "$REPO_DIR" != "$APP_DIR" ]]; then
    cp -r "$REPO_DIR"/{app,pyproject.toml,alembic.ini} "$APP_DIR"/
    [[ -f "$REPO_DIR/README.md" ]] && cp "$REPO_DIR/README.md" "$APP_DIR"/
fi

log "Building virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
"$APP_DIR/.venv/bin/pip" install --quiet "$APP_DIR"

# HTMX is only needed by the parse-rule tester; the app degrades gracefully
# without it, so a failed download is a warning rather than a hard failure.
log "Fetching HTMX"
if ! curl -fsSL -o "$APP_DIR/app/static/htmx.min.js" \
        https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js; then
    warn "Could not download HTMX — the rule tester will still work (it uses plain fetch)."
    : > "$APP_DIR/app/static/htmx.min.js"
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

log "Installing backup script"
install -m 0755 "$REPO_DIR/deploy/backup.sh" /usr/local/bin/receiptmanager-backup
cat >/etc/cron.d/receiptmanager <<EOF
# Nightly backup at 02:30. These are tax records — this is not optional.
30 2 * * * ${APP_USER} /usr/local/bin/receiptmanager-backup >/dev/null 2>&1
# Sweep export temp files older than a day.
15 3 * * * ${APP_USER} find ${DATA_DIR}/tmp -type f -mtime +1 -delete >/dev/null 2>&1
EOF

log "Installing systemd unit"
sed -e "s#/opt/receiptmanager#${APP_DIR}#g" \
    -e "s#RM_DATA_DIR=/data#RM_DATA_DIR=${DATA_DIR}#" \
    -e "s#RM_PORT=8080#RM_PORT=${PORT}#" \
    -e "s#ReadWritePaths=/data#ReadWritePaths=${DATA_DIR}#" \
    "$REPO_DIR/deploy/receiptmanager.service" >/etc/systemd/system/receiptmanager.service

systemctl daemon-reload
systemctl enable receiptmanager

# restart, not `enable --now`.
#
# `--now` only *starts* a stopped service; on an already-running one it does
# nothing. Re-installing would then leave new templates on disk — Jinja reads
# those per request — while the old Python stayed loaded in memory. A template
# referencing a context variable the running code does not pass becomes a 500
# on that page alone, which is a genuinely confusing way to fail.
log "Restarting receiptmanager"
systemctl restart receiptmanager

sleep 2
if ! systemctl is-active --quiet receiptmanager; then
    warn "Service did not start. Recent logs:"
    journalctl -u receiptmanager -n 40 --no-pager || true
    die "Installation finished but the service is not running."
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<EOF

  ReceiptManager is running.

  Open  http://${IP:-localhost}:${PORT}/  and set your admin password.

  Then, in Settings:
    1. Microsoft Graph — tenant/client/secret, mailbox, and folder.
       Scope the app registration to the one mailbox:
         New-ApplicationAccessPolicy -AppId <client-id> \\
             -PolicyScopeGroupId <mailbox> -AccessRight RestrictAccess \\
             -Description "ReceiptManager"
    2. Discord — bot token and channel ID.
       Enable MESSAGE_CONTENT under Developer Portal > Bot > Privileged Gateway Intents,
       and give the bot Manage Messages in the channel so it can delete receipts.
    3. Add a parse rule, and use the tester with a real alert email.
    4. Hit "Simulate email" in Settings to verify the whole pipeline.

  Logs:     journalctl -u receiptmanager -f
  Data:     ${DATA_DIR}
  Backups:  ${DATA_DIR}/backups (nightly at 02:30)

  Back up ${DATA_DIR}/secret.key somewhere separate — without it the stored
  Graph and Discord credentials cannot be decrypted from a database restore.

EOF
