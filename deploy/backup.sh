#!/usr/bin/env bash
#
# Nightly backup. Once ReceiptManager deletes a receipt from Discord, this
# database and these files are the ONLY copy — treat a failed backup as an
# incident, not a warning.
#
set -euo pipefail

DATA_DIR=${RM_DATA_DIR:-/data}
BACKUP_DIR="$DATA_DIR/backups"
KEEP_DAYS=${RM_BACKUP_KEEP_DAYS:-30}
STAMP=$(date -u +%Y%m%d-%H%M%S)

mkdir -p "$BACKUP_DIR"

# sqlite3 .backup is safe against a live writer; copying the file is not.
sqlite3 "$DATA_DIR/receiptmanager.db" ".backup '$BACKUP_DIR/db-$STAMP.sqlite'"
gzip -f "$BACKUP_DIR/db-$STAMP.sqlite"

# Receipts are immutable once written, so an incremental-by-date tar is enough.
tar -C "$DATA_DIR" -czf "$BACKUP_DIR/receipts-$STAMP.tar.gz" receipts

# secret.key is deliberately NOT backed up here by default.
#
# It lives outside the database precisely so that a stolen or leaked backup does
# not also hand over the Graph client secret and the Discord bot token. Copying
# it into the same archive throws that away. Losing the key is recoverable —
# re-enter those two credentials in Settings — whereas leaking it is not.
#
# Set RM_BACKUP_INCLUDE_KEY=1 if you accept that trade-off and want one-step
# restores instead.
if [[ "${RM_BACKUP_INCLUDE_KEY:-0}" == "1" ]]; then
    cp "$DATA_DIR/secret.key" "$BACKUP_DIR/secret-$STAMP.key" 2>/dev/null || true
    chmod 600 "$BACKUP_DIR"/secret-*.key 2>/dev/null || true
    echo "WARNING: secret.key included in the backup — protect it like a password."
fi

find "$BACKUP_DIR" -type f -mtime "+$KEEP_DAYS" -delete

echo "Backup complete: $BACKUP_DIR (db-$STAMP, receipts-$STAMP)"
echo "Copy these off this container — a Proxmox snapshot of the same disk is not a backup."
if [[ "${RM_BACKUP_INCLUDE_KEY:-0}" != "1" ]]; then
    echo "Reminder: store $DATA_DIR/secret.key in your password manager (it is not in this backup)."
fi
