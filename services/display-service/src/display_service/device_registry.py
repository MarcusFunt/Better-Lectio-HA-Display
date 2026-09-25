"""A small persistent per-device credential registry."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FIRMWARE_VERSION_RE = re.compile(r"^[A-Za-z0-9.+_-]{1,64}$")
_DUMMY_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    device_id: str
    credential_hash: str
    name: str
    created_at: str
    last_seen: str | None = None
    revoked: bool = False
    firmware_version: str | None = None
    last_content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class NewDeviceCredential:
    record: DeviceRecord
    secret: str


class DeviceRegistry:
    """Persist one-way credential hashes and device lifecycle metadata."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "devices.json"
        self._lock_path = data_dir / "devices.lock"
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._exclusive_lock():
            if not self._path.exists():
                self._write({})

    def create(self, name: str, *, device_id: str | None = None) -> NewDeviceCredential:
        clean_name = " ".join(name.split())
        if not clean_name or len(clean_name) > 120:
            raise ValueError("Device name must contain 1 to 120 characters")
        identifier = device_id or uuid.uuid4().hex
        _validate_device_id(identifier)
        secret = secrets.token_urlsafe(32)
        record = DeviceRecord(
            device_id=identifier,
            credential_hash=_credential_hash(secret),
            name=clean_name,
            created_at=_now(),
        )
        with self._exclusive_lock():
            records = self._read()
            if identifier in records:
                raise ValueError("Device ID already exists")
            records[identifier] = record
            self._write(records)
        return NewDeviceCredential(record=record, secret=secret)

    def rotate(self, device_id: str) -> NewDeviceCredential:
        _validate_device_id(device_id)
        secret = secrets.token_urlsafe(32)
        with self._exclusive_lock():
            records = self._read()
            record = records.get(device_id)
            if record is None or record.revoked:
                raise KeyError("Device not found or revoked")
            updated = DeviceRecord(
                **{
                    **asdict(record),
                    "credential_hash": _credential_hash(secret),
                    "last_seen": None,
                }
            )
            records[device_id] = updated
            self._write(records)
        return NewDeviceCredential(record=updated, secret=secret)

    def revoke(self, device_id: str) -> DeviceRecord:
        _validate_device_id(device_id)
        with self._exclusive_lock():
            records = self._read()
            record = records.get(device_id)
            if record is None:
                raise KeyError("Device not found")
            updated = DeviceRecord(**{**asdict(record), "revoked": True})
            records[device_id] = updated
            self._write(records)
        return updated

    def list_devices(self) -> tuple[DeviceRecord, ...]:
        with self._exclusive_lock():
            records = self._read()
        return tuple(records[key] for key in sorted(records))

    def authenticate(
        self,
        device_id: str | None,
        secret: str | None,
        *,
        firmware_version: str | None = None,
    ) -> DeviceRecord | None:
        candidate_hash = _credential_hash(secret or "")
        if device_id is not None and not _DEVICE_ID_RE.fullmatch(device_id):
            device_id = None
        valid_firmware = (
            firmware_version
            if isinstance(firmware_version, str)
            and _FIRMWARE_VERSION_RE.fullmatch(firmware_version)
            else None
        )
        with self._exclusive_lock():
            records = self._read()
            record = records.get(device_id) if device_id else None
            expected_hash = record.credential_hash if record is not None else _DUMMY_HASH
            matches = hmac.compare_digest(expected_hash, candidate_hash)
            if record is None or record.revoked or not matches:
                return None
            updated = DeviceRecord(
                **{
                    **asdict(record),
                    "last_seen": _now(),
                    "firmware_version": valid_firmware or record.firmware_version,
                }
            )
            records[device_id] = updated
            self._write(records)
        return updated

    def _read(self) -> dict[str, DeviceRecord]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("Device registry is unreadable") from error
        if not isinstance(raw, dict):
            raise RuntimeError("Device registry has an invalid format")
        records: dict[str, DeviceRecord] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise RuntimeError("Device registry has an invalid record")
            try:
                record = DeviceRecord(**value)
            except TypeError as error:
                raise RuntimeError("Device registry has an invalid record") from error
            if (
                record.device_id != key
                or not _DEVICE_ID_RE.fullmatch(key)
                or not isinstance(record.credential_hash, str)
                or not re.fullmatch(r"[a-f0-9]{64}", record.credential_hash)
                or not isinstance(record.name, str)
                or not record.name
                or not isinstance(record.created_at, str)
                or not isinstance(record.revoked, bool)
                or (record.last_seen is not None and not isinstance(record.last_seen, str))
                or (
                    record.firmware_version is not None
                    and not isinstance(record.firmware_version, str)
                )
                or (
                    record.last_content_hash is not None
                    and not isinstance(record.last_content_hash, str)
                )
            ):
                raise RuntimeError("Device registry has an invalid record")
            records[key] = record
        return records

    def _write(self, records: dict[str, DeviceRecord]) -> None:
        payload = {
            key: asdict(records[key])
            for key in sorted(records)
        }
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=".devices.", suffix=".tmp"
        )
        temporary_path = Path(temporary_name)
        try:
            os.chmod(temporary_path, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(payload, file, separators=(",", ":"), sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self._path)
        finally:
            temporary_path.unlink(missing_ok=True)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        with self._lock_path.open("a+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                if self._lock_path.stat().st_size == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _validate_device_id(device_id: str) -> None:
    if not isinstance(device_id, str) or not _DEVICE_ID_RE.fullmatch(device_id):
        raise ValueError("Invalid device ID")


def _credential_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
