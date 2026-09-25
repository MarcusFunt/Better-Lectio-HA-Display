"""Persistent content-addressed storage for validated display bitmaps."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_BMP_SIZE = 48_062


@dataclass(frozen=True, slots=True)
class DisplayRevision:
    content_hash: str
    generated_at: str


class DisplayImageStore:
    """Save immutable BMPs and atomically point to the current revision."""

    def __init__(self, data_dir: Path, *, read_only: bool = False) -> None:
        self._data_dir = data_dir
        self._images_dir = data_dir / "images"
        self._read_only = read_only
        if not read_only:
            self._images_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._current_path = data_dir / "current.json"
        self._current: DisplayRevision | None = None
        self.refresh_current()

    @property
    def current(self) -> DisplayRevision | None:
        return self._current

    def refresh_current(self) -> DisplayRevision | None:
        """Reload the current pointer, including changes from another process."""
        self._current = self._load_current()
        return self._current

    def publish(self, bmp: bytes, generated_at: datetime) -> DisplayRevision:
        """Validate and atomically publish one exact firmware-compatible BMP."""
        if self._read_only:
            raise PermissionError("This image store is read-only")
        validate_display_bmp(bmp)
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        digest = hashlib.sha256(bmp).hexdigest()
        target = self._images_dir / f"{digest}.bmp"
        if not target.exists():
            _atomic_write(target, bmp)
        elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise OSError("Stored image does not match its content address")

        revision = DisplayRevision(
            content_hash=digest,
            generated_at=generated_at.astimezone(timezone.utc).isoformat(),
        )
        _atomic_write(
            self._current_path,
            json.dumps(
                {
                    "content_hash": revision.content_hash,
                    "generated_at": revision.generated_at,
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        self._current = revision
        self._prune_old_images(keep=digest)
        return revision

    def read(self, content_hash: str) -> bytes | None:
        if not _HASH_RE.fullmatch(content_hash):
            return None
        image_path = self._images_dir / f"{content_hash}.bmp"
        try:
            bmp = image_path.read_bytes()
        except FileNotFoundError:
            return None
        if hashlib.sha256(bmp).hexdigest() != content_hash:
            return None
        try:
            validate_display_bmp(bmp)
        except ValueError:
            return None
        return bmp

    def _load_current(self) -> DisplayRevision | None:
        try:
            value = json.loads(self._current_path.read_text(encoding="utf-8"))
            content_hash = value["content_hash"]
            generated_at = value["generated_at"]
            parsed = datetime.fromisoformat(generated_at)
            if (
                not isinstance(content_hash, str)
                or not _HASH_RE.fullmatch(content_hash)
                or parsed.tzinfo is None
                or parsed.utcoffset() is None
                or self.read(content_hash) is None
            ):
                return None
        except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        return DisplayRevision(content_hash=content_hash, generated_at=parsed.isoformat())

    def _prune_old_images(self, *, keep: str) -> None:
        # Keep recent revisions for clients that requested metadata just before
        # a publication; the active revision is always retained.
        cutoff = datetime.now(timezone.utc).timestamp() - 30 * 24 * 60 * 60
        for path in self._images_dir.glob("*.bmp"):
            if path.stem == keep:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue


def validate_display_bmp(bmp: bytes) -> None:
    """Reject any bitmap the XIAO ESP32-S3 firmware cannot display."""
    if (
        len(bmp) != _BMP_SIZE
        or bmp[:2] != b"BM"
        or _u32(bmp, 2) != _BMP_SIZE
        or _u32(bmp, 10) != 62
        or _u32(bmp, 14) != 40
        or _i32(bmp, 18) != 800
        or _i32(bmp, 22) != 480
        or _u16(bmp, 26) != 1
        or _u16(bmp, 28) != 1
        or _u32(bmp, 30) != 0
        or _u32(bmp, 34) != 48_000
    ):
        raise ValueError("BMP must be an uncompressed 800x480 1-bit Windows bitmap")
    if bmp[54:62] not in {
        b"\x00\x00\x00\x00\xff\xff\xff\x00",
        b"\xff\xff\xff\x00\x00\x00\x00\x00",
    }:
        raise ValueError("BMP must use a supported monochrome palette")


def _u16(value: bytes, offset: int) -> int:
    return int.from_bytes(value[offset : offset + 2], "little", signed=False)


def _u32(value: bytes, offset: int) -> int:
    return int.from_bytes(value[offset : offset + 4], "little", signed=False)


def _i32(value: bytes, offset: int) -> int:
    return int.from_bytes(value[offset : offset + 4], "little", signed=True)


def _atomic_write(target: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)
