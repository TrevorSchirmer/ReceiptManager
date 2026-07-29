#!/usr/bin/env bash
#
# ReceiptManager installer for a Debian 12 unprivileged Proxmox LXC.
#
# Creates a service user, installs into /opt/receiptmanager with a venv, puts all
# mutable state under /data (mount that separately so a container rebuild does
# not destroy your receipts), and installs a systemd unit plus a nightly backup.
#
#   bash deploy/install.sh
#
set -euo pipefail

APP_USER=receiptmanager
APP_DIR=/opt/receiptmanager
DATA_DIR=${RM_DATA_DIR:-/data}
PORT=${RM_PORT:-8080}
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (or with sudo)."

log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# Only Python, curl and sqlite3 are needed — Pillow, pillow-heif and pypdfium2
# all ship binary wheels, so there is no libheif/poppler/imagemagick to chase.
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-dev \
    ca-certificates curl sqlite3 tzdata

log "Creating service user ${APP_USER}"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

log "Creating ${DATA_DIR}"
mkdir -p "$DATA_DIR"/{receipts,thumbs,tmp,backups}
chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"

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
systemctl enable --now receiptmanager

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
