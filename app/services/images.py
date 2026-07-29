"""Image and PDF normalization for receipt archiving.

Two rules govern this module:

1. **HEIC must be converted to JPEG.** iPhones send HEIC by default and no
   browser can render it. An unconverted receipt is an invisible receipt.
2. **Thumbnail failure is always non-fatal.** The receipt bytes are the record
   of value; the thumbnail is cosmetic. :func:`make_thumbnail` therefore logs
   and returns ``None`` rather than raising, so a corrupt or encrypted PDF can
   never block capture.

Every function that produces a file writes to a temp path and ``os.replace``s it
into position, so a crash never leaves a half-written receipt on disk.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import PIL.Image
import PIL.ImageOps
import pypdfium2 as pdfium

logger = logging.getLogger(__name__)

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:  # pragma: no cover - degraded mode, surfaced at conversion time
    HEIF_AVAILABLE = False
    logger.warning("pillow-heif unavailable; HEIC receipts cannot be converted")

# Bound decompression-bomb risk. A 48 MP phone photo is ~48 Mpx, so this leaves
# generous headroom while still refusing a crafted image that would exhaust RAM.
PIL.Image.MAX_IMAGE_PIXELS = 120_000_000

SUPPORTED_IMAGE_MIMES: frozenset[str] = frozenset(
    (
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/heic",
        "image/tiff",
        "image/bmp",
    )
)
SUPPORTED_MIMES: frozenset[str] = SUPPORTED_IMAGE_MIMES | {"application/pdf"}
MAX_THUMB_PX: int = 512

_HEIC_BRANDS: frozenset[bytes] = frozenset(
    (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs",
     b"mif1", b"msf1")
)

_MIME_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/tiff": ".tif",
    "image/bmp": ".bmp",
    "application/pdf": ".pdf",
}

_MIME_TO_PIL_FORMAT: dict[str, str] = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
    "image/tiff": "TIFF",
    "image/bmp": "BMP",
}

_EXIF_ORIENTATION_TAG = 0x0112


class UnsupportedFileError(ValueError):
    """Raised when a file's detected type is not something we archive."""


@dataclass(slots=True)
class NormalizedFile:
    path: Path
    mime: str
    bytes: int
    width: int | None
    height: int | None
    converted_from: str | None = None


def sniff_mime(data: bytes, filename: str | None = None) -> str:
    """Detect type from magic bytes, falling back to the filename.

    Magic bytes come first deliberately: a Discord upload's declared type and
    filename are both attacker-controlled.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith((b"II\x2a\x00", b"MM\x00\x2a")):
        return "image/tiff"
    if data.startswith(b"BM"):
        return "image/bmp"
    if len(data) >= 12:
        if data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
            return "image/heic"

    if filename:
        guessed = mimetypes.guess_type(filename)[0]
        if guessed:
            return guessed
    return "application/octet-stream"


def is_supported(mime: str) -> bool:
    return mime in SUPPORTED_MIMES


def extension_for_mime(mime: str) -> str:
    return _MIME_EXT.get(mime, ".bin")


def normalize_file(src: Path, *, dest_dir: Path, stem: str) -> NormalizedFile:
    """Move ``src`` into ``dest_dir`` as ``stem`` + a correct extension.

    HEIC is transcoded to JPEG; other images are EXIF-auto-oriented (and only
    re-encoded when a rotation was actually needed, so untouched originals keep
    their exact bytes); PDFs pass through verbatim.

    ``src`` is always consumed — on return it no longer exists.
    """
    with open(src, "rb") as fh:
        mime = sniff_mime(fh.read(4096), src.name)

    if mime not in SUPPORTED_MIMES:
        raise UnsupportedFileError(f"Unsupported file type: {mime}")

    dest_dir.mkdir(parents=True, exist_ok=True)

    if mime == "application/pdf":
        dest_path = dest_dir / f"{stem}.pdf"
        shutil.move(str(src), str(dest_path))
        return NormalizedFile(
            path=dest_path, mime=mime, bytes=dest_path.stat().st_size,
            width=None, height=None,
        )

    if mime == "image/heic":
        if not HEIF_AVAILABLE:
            raise UnsupportedFileError(
                "HEIC received but pillow-heif is unavailable — cannot convert, "
                "and the browser cannot display it."
            )
        dest_path = dest_dir / f"{stem}.jpg"
        with PIL.Image.open(src) as opened:
            img = PIL.ImageOps.exif_transpose(opened) or opened
            img = _to_rgb(img)
            width, height = img.size
            _save_atomic(img, dest_path, format="JPEG", quality=88, progressive=True)
        src.unlink(missing_ok=True)
        return NormalizedFile(
            path=dest_path, mime="image/jpeg", bytes=dest_path.stat().st_size,
            width=width, height=height, converted_from="image/heic",
        )

    dest_path = dest_dir / f"{stem}{extension_for_mime(mime)}"
    with PIL.Image.open(src) as opened:
        # Read the format before any transform — exif_transpose returns a new
        # image with .format set to None.
        pil_format = opened.format or _MIME_TO_PIL_FORMAT.get(mime, "JPEG")
        orientation = opened.getexif().get(_EXIF_ORIENTATION_TAG)
        needs_rotation = orientation not in (None, 1)
        width, height = opened.size

        if not needs_rotation:
            # Nothing to change — preserve the original bytes exactly.
            rotated = None
        else:
            rotated = PIL.ImageOps.exif_transpose(opened)
            if rotated is not None:
                if pil_format == "JPEG":
                    rotated = _to_rgb(rotated)
                width, height = rotated.size
                _save_atomic(
                    rotated, dest_path, format=pil_format,
                    **({"quality": 92, "progressive": True} if pil_format == "JPEG" else {}),
                )

    if rotated is None:
        shutil.move(str(src), str(dest_path))
    else:
        src.unlink(missing_ok=True)

    return NormalizedFile(
        path=dest_path, mime=mime, bytes=dest_path.stat().st_size,
        width=width, height=height,
    )


def make_thumbnail(
    src: Path, mime: str, dest: Path, max_px: int = MAX_THUMB_PX
) -> Path | None:
    """Render a JPEG thumbnail, or return None if it cannot be produced.

    Never raises: a missing thumbnail is cosmetic, a lost receipt is not.
    """
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if mime == "application/pdf":
            return _pdf_thumbnail(src, dest, max_px)
        return _image_thumbnail(src, dest, max_px)
    except Exception:
        logger.warning("Thumbnail generation failed for %s", src, exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

def _to_rgb(img: PIL.Image.Image) -> PIL.Image.Image:
    """Flatten to RGB, compositing any alpha over white (JPEG has no alpha)."""
    if img.mode == "RGB":
        return img
    if img.mode in ("P", "PA"):
        img = img.convert("RGBA")
    if img.mode in ("RGBA", "LA"):
        background = PIL.Image.new("RGB", img.size, (255, 255, 255))
        rgba = img if img.mode == "RGBA" else img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[3])
        return background
    return img.convert("RGB")


def _save_atomic(img: PIL.Image.Image, dest: Path, **save_kwargs: object) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.stem}.", suffix=".tmp")
    os.close(fd)
    try:
        img.save(tmp_name, **save_kwargs)
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _pdf_thumbnail(src: Path, dest: Path, max_px: int) -> Path:
    doc = pdfium.PdfDocument(str(src))
    try:
        page = doc[0]
        width_pt, height_pt = page.get_size()
        longest = max(width_pt, height_pt)
        # Cap the scale so a postage-stamp-sized page cannot blow up into a
        # gigantic bitmap.
        scale = min(max_px / longest, 4.0) if longest else 1.0
        img = page.render(scale=scale).to_pil()
        img = _to_rgb(img)
        img.thumbnail((max_px, max_px), PIL.Image.Resampling.LANCZOS)
        _save_atomic(img, dest, format="JPEG", quality=82, progressive=True)
    finally:
        doc.close()
    return dest


def _image_thumbnail(src: Path, dest: Path, max_px: int) -> Path:
    with PIL.Image.open(src) as opened:
        img = PIL.ImageOps.exif_transpose(opened) or opened
        img = _to_rgb(img)
        img.thumbnail((max_px, max_px), PIL.Image.Resampling.LANCZOS)
        _save_atomic(img, dest, format="JPEG", quality=82, progressive=True)
    return dest
