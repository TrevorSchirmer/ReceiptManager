"""Durable receipt storage.

This module exists to make one guarantee: **once we tell the rest of the system a
receipt is stored, the bytes are on disk and survive power loss.** Everything
downstream — in particular deleting the user's Discord message — depends on that
promise being literally true, because Discord attachment URLs are signed and
expiring and a deleted message's attachment is unrecoverable.

The capture sequence is therefore:

1. verify the downloaded byte count against what Discord declared
2. write to a temp file on the *same filesystem*, then ``fsync`` it
3. normalize (HEIC transcode, EXIF rotation) into its final path
4. ``fsync`` the final file **and its parent directory** — without the directory
   fsync the rename itself is not durable
5. re-read the file from disk and verify its sha256 and size
6. only then return, letting the caller commit the DB row

:func:`assert_durable` is the final gate re-run immediately before any deletion.
If it fails we keep the Discord message, which is always the recoverable choice.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import get_config
from app.services import images

logger = logging.getLogger(__name__)

# Discord's own limit is ~10 MB on the free tier; allow headroom for UI uploads.
MAX_RECEIPT_BYTES = 32 * 1024 * 1024

_HASH_CHUNK = 1024 * 1024


class CaptureError(RuntimeError):
    """Raised when a receipt could not be durably stored.

    Callers must treat this as "do not delete anything, retry later".
    """


@dataclass(slots=True)
class StoredFile:
    rel_path: str            # relative to config.receipts_dir
    thumb_rel_path: str | None
    mime: str
    bytes: int
    sha256: str
    width: int | None
    height: int | None
    converted_from: str | None


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename into it is durable.

    Skipped silently on filesystems that refuse O_RDONLY on directories.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _partition_dir(when: dt.datetime) -> Path:
    """Shard by year/month so no single directory grows unbounded."""
    return get_config().receipts_dir / f"{when:%Y}" / f"{when:%m}"


def store_receipt_bytes(
    data: bytes,
    *,
    original_filename: str | None = None,
    expected_bytes: int | None = None,
    when: dt.datetime | None = None,
) -> StoredFile:
    """Durably store one receipt file. Raises :class:`CaptureError` on any doubt.

    ``expected_bytes`` should be the size Discord reported for the attachment; a
    mismatch means a truncated download, which is the exact failure that must
    never be followed by a delete.
    """
    cfg = get_config()
    when = when or dt.datetime.now(dt.UTC)

    if not data:
        raise CaptureError("Downloaded receipt was empty")
    if expected_bytes is not None and len(data) != expected_bytes:
        raise CaptureError(
            f"Truncated download: got {len(data)} bytes, expected {expected_bytes}"
        )
    if len(data) > MAX_RECEIPT_BYTES:
        raise CaptureError(
            f"Receipt is {len(data)} bytes, over the {MAX_RECEIPT_BYTES} byte limit"
        )

    source_sha = hashlib.sha256(data).hexdigest()
    dest_dir = _partition_dir(when)
    dest_dir.mkdir(parents=True, exist_ok=True)
    cfg.tmp_dir.mkdir(parents=True, exist_ok=True)

    # Step 1-2: land the bytes on the same filesystem and force them to platter
    # before doing anything else with them.
    fd, tmp_name = tempfile.mkstemp(dir=cfg.tmp_dir, prefix="capture-", suffix=".part")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

        mime = images.sniff_mime(data[:4096], original_filename)
        if not images.is_supported(mime):
            raise CaptureError(
                f"Unsupported receipt type {mime!r} "
                f"(accepted: images and PDF)"
            )

        # Step 3: normalize into place. normalize_file consumes tmp_path.
        stem = uuid.uuid4().hex
        try:
            normalized = images.normalize_file(tmp_path, dest_dir=dest_dir, stem=stem)
        except images.UnsupportedFileError as exc:
            raise CaptureError(str(exc)) from exc
        except Exception as exc:
            raise CaptureError(f"Failed to normalize receipt: {exc}") from exc
    finally:
        # normalize_file consumes the temp file on success; clean up if it did not.
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    final_path = normalized.path
    try:
        # Step 4: the file, then the directory. Both are required — fsyncing the
        # file alone does not make its directory entry durable.
        _fsync_path(final_path)
        _fsync_dir(dest_dir)

        # Step 5: read it back off disk. Not paranoia — this is what makes the
        # subsequent Discord deletion defensible.
        stored_sha = sha256_file(final_path)
        stored_size = final_path.stat().st_size
        if stored_size != normalized.bytes:
            raise CaptureError(
                f"Size mismatch after write: disk has {stored_size}, "
                f"expected {normalized.bytes}"
            )
        if normalized.converted_from is None and stored_sha != source_sha:
            # Untouched files must be byte-identical. Converted ones legitimately differ.
            raise CaptureError("Checksum mismatch after write — storage may be failing")
    except CaptureError:
        final_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        final_path.unlink(missing_ok=True)
        raise CaptureError(f"Verification failed after write: {exc}") from exc

    rel_path = str(final_path.relative_to(cfg.receipts_dir))

    # Step 6 is the caller's: commit the DB row. Thumbnails are cosmetic and are
    # generated last so a failure here cannot cost us the receipt.
    thumb_rel: str | None = None
    thumb_dest = cfg.thumbs_dir / f"{Path(rel_path).with_suffix('').name}.jpg"
    if images.make_thumbnail(final_path, normalized.mime, thumb_dest) is not None:
        thumb_rel = str(thumb_dest.relative_to(cfg.thumbs_dir))

    logger.info(
        "Stored receipt %s (%s, %d bytes, sha=%s)",
        rel_path, normalized.mime, normalized.bytes, stored_sha[:12],
    )
    return StoredFile(
        rel_path=rel_path,
        thumb_rel_path=thumb_rel,
        mime=normalized.mime,
        bytes=normalized.bytes,
        sha256=stored_sha,
        width=normalized.width,
        height=normalized.height,
        converted_from=normalized.converted_from,
    )


def absolute_path(rel_path: str) -> Path:
    """Resolve a stored relative path, refusing anything outside the data dir.

    Defence against a traversal value reaching us via the DB or a crafted request.
    """
    cfg = get_config()
    root = cfg.receipts_dir.resolve()
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root):
        raise CaptureError(f"Refusing path outside the receipts directory: {rel_path!r}")
    return candidate


def thumb_absolute_path(rel_path: str) -> Path:
    cfg = get_config()
    root = cfg.thumbs_dir.resolve()
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root):
        raise CaptureError(f"Refusing path outside the thumbnails directory: {rel_path!r}")
    return candidate


def assert_durable(rel_path: str, expected_sha256: str, expected_bytes: int) -> None:
    """Final gate before deleting the source message. Raises if anything is off.

    Called immediately before the Discord delete, not merely at capture time —
    between capture and deletion a disk can fill, a mount can vanish, or a
    restore can roll the filesystem back. Deleting on a stale assumption is
    unrecoverable; keeping a message we meant to delete is merely untidy.
    """
    path = absolute_path(rel_path)
    if not path.is_file():
        raise CaptureError(f"Receipt file is missing: {rel_path}")
    size = path.stat().st_size
    if size != expected_bytes:
        raise CaptureError(
            f"Receipt {rel_path} is {size} bytes on disk, expected {expected_bytes}"
        )
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise CaptureError(
            f"Receipt {rel_path} failed checksum verification "
            f"(disk={actual[:12]}, expected={expected_sha256[:12]})"
        )


def delete_stored_file(rel_path: str, thumb_rel_path: str | None = None) -> None:
    """Remove a stored receipt. Only ever called from an explicit UI action."""
    try:
        absolute_path(rel_path).unlink(missing_ok=True)
    except CaptureError:
        logger.warning("Refused to delete suspicious path %r", rel_path)
    if thumb_rel_path:
        try:
            thumb_absolute_path(thumb_rel_path).unlink(missing_ok=True)
        except CaptureError:
            logger.warning("Refused to delete suspicious thumb path %r", thumb_rel_path)
