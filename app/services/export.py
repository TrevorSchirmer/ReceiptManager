"""The accountant hand-off artifact: a CSV plus every receipt file, in one ZIP.

Design decisions worth stating:

* **A partial export with an honest manifest beats a hard failure.** If a receipt
  file has gone missing on disk, the export still completes and names exactly
  what is absent, rather than raising and leaving the user with nothing.
* The CSV carries a UTF-8 BOM so Excel renders accented merchant names correctly.
* Amounts are plain signed decimals with no currency symbol — symbols make Excel
  treat the column as text.
* The ZIP is written to a temp file and atomically replaced, so a crash never
  leaves behind a truncated archive that looks complete.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.formatting import iso_date, local, money_plain
from app.models import Attachment, Transaction, TransactionStatus
from app.services.storage import CaptureError, absolute_path

logger = logging.getLogger(__name__)

CSV_COLUMNS: tuple[str, ...] = (
    "code", "date", "time", "merchant", "amount", "currency", "final_amount",
    "card_last4", "cardholder", "status", "category", "notes",
    "receipt_count", "receipt_files",
)

_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_SLUG_NON_ALPHANUM = re.compile(r"[^a-z0-9]+")
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|]')


@dataclass(slots=True)
class ExportStats:
    transactions: int
    receipts: int
    missing_files: int
    total_bytes: int


def _zone(tz_name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        logger.warning("Unknown timezone %r; using UTC", tz_name)
        return timezone.utc


def _slugify_merchant(merchant: str) -> str:
    slug = _SLUG_NON_ALPHANUM.sub("-", (merchant or "").lower()).strip("-")
    return (slug or "unknown")[:40]


def _sanitise_filename(filename: str) -> str:
    """Make a name safe on Windows as well as POSIX."""
    name = _UNSAFE_CHARS.sub("-", filename)
    stem, ext = os.path.splitext(name)
    if stem.upper() in _RESERVED_NAMES:
        stem = f"_{stem}"
    return stem + ext


def iter_transactions(
    db: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    statuses: Sequence[str] | None = None,
    tz_name: str = "UTC",
) -> list[Transaction]:
    """Transactions in an inclusive local-date range, attachments eager-loaded."""
    stmt = select(Transaction).options(selectinload(Transaction.attachments))

    if start is not None or end is not None:
        tz = _zone(tz_name)
        if start is not None:
            stmt = stmt.where(
                Transaction.occurred_at
                >= datetime.combine(start, time.min, tzinfo=tz).astimezone(timezone.utc)
            )
        if end is not None:
            stmt = stmt.where(
                Transaction.occurred_at
                <= datetime.combine(end, time.max, tzinfo=tz).astimezone(timezone.utc)
            )

    if statuses:
        # Compare on the string values the column actually stores.
        wanted = [TransactionStatus(s).value for s in statuses]
        stmt = stmt.where(Transaction.status.in_(wanted))

    stmt = stmt.order_by(Transaction.occurred_at.asc(), Transaction.id.asc())
    return list(db.execute(stmt).scalars().all())


def receipt_filename(
    tx: Transaction, attachment: Attachment, index: int, tz_name: str = "UTC"
) -> str:
    """``2026-07-28_1042_amazon-marketplace_1.jpg`` — keys back to the CSV row."""
    ext = Path(attachment.path).suffix.lower() or ".bin"
    raw = (
        f"{iso_date(tx.occurred_at, tz_name)}_{tx.short_code}_"
        f"{_slugify_merchant(tx.merchant)}_{index}{ext}"
    )
    return _sanitise_filename(raw)


def build_csv(transactions: Sequence[Transaction], *, tz_name: str = "UTC") -> str:
    out = io.StringIO(newline="")
    # Excel misdetects UTF-8 without a byte-order mark, mangling accented names.
    out.write("﻿")
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)

    for tx in transactions:
        files = ";".join(
            receipt_filename(tx, att, i, tz_name)
            for i, att in enumerate(tx.attachments, start=1)
        )
        writer.writerow([
            tx.short_code or "",
            iso_date(tx.occurred_at, tz_name),
            f"{local(tx.occurred_at, tz_name):%H:%M}",
            tx.merchant or "",
            money_plain(tx.amount_minor),
            (tx.currency or "").upper(),
            money_plain(tx.amount_final_minor) if tx.amount_final_minor is not None else "",
            tx.card_last4 or "",
            tx.cardholder or "",
            tx.status.value if isinstance(tx.status, TransactionStatus) else str(tx.status),
            tx.category or "",
            tx.notes or "",
            len(tx.attachments),
            files,
        ])
    return out.getvalue()


def build_zip(
    db: Session,
    dest: Path,
    *,
    start: date | None = None,
    end: date | None = None,
    statuses: Sequence[str] | None = None,
    tz_name: str = "UTC",
) -> ExportStats:
    """Write ``transactions.csv`` + ``receipts/*`` + ``MANIFEST.txt`` to ``dest``."""
    transactions = iter_transactions(
        db, start=start, end=end, statuses=statuses, tz_name=tz_name
    )
    csv_text = build_csv(transactions, tz_name=tz_name)

    # Carry the owning transaction alongside each entry — the manifest needs it
    # to attribute a missing file to the right charge.
    entries: list[tuple[str, Transaction, Attachment]] = []
    used_names: set[str] = set()
    for tx in transactions:
        for index, att in enumerate(tx.attachments, start=1):
            base = receipt_filename(tx, att, index, tz_name)
            name = base
            counter = 2
            while name in used_names:
                stem, ext = os.path.splitext(base)
                name = f"{stem}-{counter}{ext}"
                counter += 1
            used_names.add(name)
            entries.append((name, tx, att))

    stats = ExportStats(
        transactions=len(transactions), receipts=len(entries),
        missing_files=0, total_bytes=0,
    )
    missing: list[str] = []

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("transactions.csv", csv_text.encode("utf-8"))

            for name, tx, att in entries:
                arcname = str(PurePosixPath("receipts") / name)
                try:
                    path = absolute_path(att.path)
                    if not path.is_file():
                        raise FileNotFoundError(att.path)
                    archive.write(path, arcname=arcname)
                    stats.total_bytes += path.stat().st_size
                except (CaptureError, FileNotFoundError, OSError) as exc:
                    logger.warning("Receipt missing for #%s: %s (%s)", tx.short_code, att.path, exc)
                    missing.append(
                        f"  #{tx.short_code}  {iso_date(tx.occurred_at, tz_name)}  "
                        f"{tx.merchant}  ->  {att.path}"
                    )
                    stats.missing_files += 1

            lines = [
                "ReceiptManager export",
                f"Generated:    {datetime.now(timezone.utc).isoformat()}",
                f"Timezone:     {tz_name}",
                f"Date range:   {start.isoformat() if start else 'any'}"
                f" .. {end.isoformat() if end else 'any'}",
                f"Statuses:     {', '.join(statuses) if statuses else 'all'}",
                "",
                f"Transactions: {stats.transactions}",
                f"Receipts:     {stats.receipts - stats.missing_files} of {stats.receipts}",
                f"Total bytes:  {stats.total_bytes}",
            ]
            if missing:
                lines += [
                    "",
                    f"MISSING RECEIPT FILES ({stats.missing_files}) — these charges are "
                    "listed in the CSV but their files were not found on disk:",
                    *missing,
                ]
            archive.writestr("MANIFEST.txt", "\n".join(lines).encode("utf-8"))

        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    logger.info(
        "Export %s: %d transactions, %d receipts, %d missing",
        dest.name, stats.transactions, stats.receipts, stats.missing_files,
    )
    return stats
