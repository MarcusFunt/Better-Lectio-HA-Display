import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from lectio_gateway.lectio.client import LectioClient
from lectio_gateway.lectio.errors import (
    LectioAdapterError,
    LectioResponseChanged,
    LectioSessionExpired,
)
from lectio_gateway.lectio.models import (
    AuthenticatedLectioSession,
    LectioAssignment,
    LectioCancellation,
    LectioHomework,
    LectioLesson,
    LectioSyncStatus,
)

_LOGGER = logging.getLogger(__name__)
_UTC = timezone.utc
_SOURCES = ("schedule", "assignments", "homework", "cancellations")
_METHODS = {
    "schedule": "get_schedule",
    "assignments": "get_assignments",
    "homework": "get_homework",
    "cancellations": "get_cancellations",
}
_ITEM_MODELS: dict[str, type[BaseModel]] = {
    "schedule": LectioLesson,
    "assignments": LectioAssignment,
    "homework": LectioHomework,
    "cancellations": LectioCancellation,
}
_CACHE_VERSION = 1
_MAX_CACHE_ENTRIES = 64
_MAX_STATUS_ENTRIES = 256
_NO_SESSION_SCOPE = "no-session"


class LectioSourceUnavailable(RuntimeError):
    """A Lectio source failed and has no last-known-good result to serve."""


class LectioAuthenticationRequired(LectioSourceUnavailable):
    """No authenticated Lectio session is available."""


class LectioStudentIdRequired(LectioSourceUnavailable):
    """This source requires an explicitly configured student ID."""


@dataclass(frozen=True)
class _CacheEntry:
    source: str
    owner: str
    start: datetime
    end: datetime
    items: list[BaseModel]
    status: LectioSyncStatus


class LectioDataService:
    """Fetches normalized Lectio sources with isolated, persistent range caches."""

    def __init__(
        self,
        *,
        session_provider: Callable[[], AuthenticatedLectioSession | None],
        cache_path: Path,
        ttl_seconds: int = 300,
        client_factory: Callable[[AuthenticatedLectioSession], Any] = LectioClient,
        clock: Callable[[], datetime] | None = None,
        on_session_expired: Callable[[], None] | None = None,
    ):
        self._session_provider = session_provider
        self._cache_path = cache_path
        self._owner_key_path = cache_path.with_name("lectio-cache-key")
        self._owner_key: bytes | None = None
        self._ttl_seconds = ttl_seconds
        self._client_factory = client_factory
        self._clock = clock or (lambda: datetime.now(_UTC))
        self._on_session_expired = on_session_expired
        self._entries: dict[tuple[str, str, str, str], _CacheEntry] = {}
        self._statuses: dict[tuple[str, str], LectioSyncStatus] = {}
        self._locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}
        self._persist_lock = asyncio.Lock()

    async def initialize(self) -> None:
        try:
            self._owner_key = await asyncio.to_thread(self._load_or_create_owner_key)
        except (OSError, ValueError):
            _LOGGER.warning("Could not load the Lectio cache ownership key")
            self._owner_key = secrets.token_bytes(32)
            self._entries = {}
            self._statuses = {}
            return
        try:
            self._entries, self._statuses = await asyncio.to_thread(self._read_cache)
        except (OSError, ValueError, ValidationError, TypeError, KeyError):
            _LOGGER.warning("Could not restore the Lectio data cache")
            self._entries = {}
            self._statuses = {}
        for entry in self._entries.values():
            status_key = (entry.source, entry.owner)
            current = self._statuses.get(status_key, LectioSyncStatus())
            attempt = entry.status.last_attempt_at
            if attempt is not None and (
                current.last_attempt_at is None or attempt > current.last_attempt_at
            ):
                self._statuses[status_key] = entry.status

    def statuses(self) -> dict[str, LectioSyncStatus]:
        session = self._session_provider()
        owner = self._owner_scope(session) if session is not None else _NO_SESSION_SCOPE
        return {
            source: self._statuses.get((source, owner), LectioSyncStatus())
            for source in _SOURCES
        }

    async def get_source(
        self, source: str, start: datetime, end: datetime
    ) -> tuple[list[BaseModel], LectioSyncStatus]:
        if source not in _METHODS:
            raise ValueError("Unknown Lectio source")
        start, end = self._normalize_range(start, end)
        now = self._now()
        session = self._session_provider()
        if session is None:
            status = LectioSyncStatus(
                state="error",
                last_attempt_at=now,
                error="Lectio authentication is required.",
            )
            self._statuses[(source, _NO_SESSION_SCOPE)] = status
            await self._persist_cache()
            raise LectioAuthenticationRequired("Lectio authentication is required.")
        owner = self._owner_scope(session)
        if source in {"assignments", "homework"} and session.student_id is None:
            status = LectioSyncStatus(
                state="error",
                last_attempt_at=now,
                error="A student ID is required for this Lectio source.",
            )
            self._statuses[(source, owner)] = status
            await self._persist_cache()
            raise LectioStudentIdRequired("A student ID is required for this source.")

        key = (source, owner, start.isoformat(), end.isoformat())
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = self._now()
            active_session = self._session_provider()
            if active_session is None:
                status = LectioSyncStatus(
                    state="error",
                    last_attempt_at=now,
                    error="Lectio authentication is required.",
                )
                self._statuses[(source, _NO_SESSION_SCOPE)] = status
                await self._persist_cache()
                raise LectioAuthenticationRequired(
                    "Lectio authentication is required."
                )
            if self._owner_scope(active_session) != owner:
                raise LectioSourceUnavailable(
                    "The Lectio session changed during this request. Try again."
                )
            session = active_session
            entry = self._entries.get(key)
            last_success = entry.status.last_successful_sync if entry else None
            if (
                entry is not None
                and entry.status.state == "valid"
                and last_success is not None
                and (now - last_success).total_seconds() < self._ttl_seconds
            ):
                return entry.items, entry.status

            try:
                client = self._client_factory(session)
                items = await getattr(client, _METHODS[source])(start, end)
                model = _ITEM_MODELS[source]
                if not isinstance(items, list) or any(
                    not isinstance(item, model) for item in items
                ):
                    raise LectioResponseChanged(
                        "Lectio adapter returned an invalid normalized result"
                    )
                self._require_current_owner(owner)
            except LectioSourceUnavailable:
                raise
            except LectioSessionExpired:
                self._require_current_owner(owner)
                if self._on_session_expired is not None:
                    self._on_session_expired()
                try:
                    return await self._record_failure(
                        key,
                        source,
                        entry,
                        now,
                        state="expired",
                        error="Lectio session expired. Please sign in again.",
                    )
                except LectioSourceUnavailable as exc:
                    raise LectioAuthenticationRequired(
                        "Lectio session expired. Please sign in again."
                    ) from exc
            except LectioResponseChanged:
                self._require_current_owner(owner)
                return await self._record_failure(
                    key,
                    source,
                    entry,
                    now,
                    state="stale" if entry else "error",
                    error="Lectio returned a response the gateway could not read.",
                )
            except LectioAdapterError:
                self._require_current_owner(owner)
                return await self._record_failure(
                    key,
                    source,
                    entry,
                    now,
                    state="stale" if entry else "error",
                    error="The Lectio source request failed.",
                )
            except Exception as exc:
                self._require_current_owner(owner)
                _LOGGER.warning(
                    "Lectio %s request failed (%s)", source, type(exc).__name__
                )
                return await self._record_failure(
                    key,
                    source,
                    entry,
                    now,
                    state="stale" if entry else "error",
                    error="The Lectio source request failed.",
                )

            status = LectioSyncStatus(
                state="valid",
                last_attempt_at=now,
                last_successful_sync=now,
                is_stale=False,
            )
            self._entries[key] = _CacheEntry(source, owner, start, end, items, status)
            self._statuses[(source, owner)] = status
            self._prune_cache()
            await self._persist_cache()
            self._require_current_owner(owner)
            return items, status

    async def _record_failure(
        self,
        key: tuple[str, str, str, str],
        source: str,
        entry: _CacheEntry | None,
        now: datetime,
        *,
        state: Literal["stale", "expired", "error"],
        error: str,
    ) -> tuple[list[BaseModel], LectioSyncStatus]:
        has_stale_data = entry is not None
        status = LectioSyncStatus(
            state=state,
            last_attempt_at=now,
            last_successful_sync=(
                entry.status.last_successful_sync if entry is not None else None
            ),
            is_stale=has_stale_data,
            error=error,
        )
        owner = key[1]
        self._statuses[(source, owner)] = status
        if entry is not None:
            self._entries[key] = _CacheEntry(
                source, owner, entry.start, entry.end, entry.items, status
            )
        await self._persist_cache()
        if entry is not None:
            self._require_current_owner(owner)
            return entry.items, status
        raise LectioSourceUnavailable(error)

    def _require_current_owner(self, owner: str) -> None:
        session = self._session_provider()
        if session is None or self._owner_scope(session) != owner:
            raise LectioSourceUnavailable(
                "The Lectio session changed during this request. Try again."
            )

    async def _persist_cache(self) -> None:
        async with self._persist_lock:
            payload = self._serialize_cache()
            try:
                await asyncio.to_thread(self._write_cache, payload)
            except OSError:
                _LOGGER.warning("Could not persist the Lectio data cache")

    def _serialize_cache(self) -> str:
        entries = []
        for entry in self._entries.values():
            entries.append(
                {
                    "source": entry.source,
                    "owner": entry.owner,
                    "start": entry.start.isoformat(),
                    "end": entry.end.isoformat(),
                    "items": [item.model_dump(mode="json") for item in entry.items],
                    "status": entry.status.model_dump(mode="json"),
                }
            )
        statuses = [
            {
                "source": source,
                "owner": owner,
                "status": status.model_dump(mode="json"),
            }
            for (source, owner), status in self._statuses.items()
        ]
        return json.dumps(
            {"version": _CACHE_VERSION, "entries": entries, "statuses": statuses},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _read_cache(
        self,
    ) -> tuple[
        dict[tuple[str, str, str, str], _CacheEntry],
        dict[tuple[str, str], LectioSyncStatus],
    ]:
        if not self._cache_path.exists():
            return {}, {}
        payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        if payload.get("version") != _CACHE_VERSION or not isinstance(
            payload.get("entries"), list
        ):
            raise ValueError("Unsupported Lectio cache format")
        entries = {}
        for raw in payload["entries"]:
            source = raw["source"]
            if source not in _ITEM_MODELS:
                raise ValueError("Unknown cached Lectio source")
            owner = raw["owner"]
            start = self._parse_cached_datetime(raw["start"])
            end = self._parse_cached_datetime(raw["end"])
            status = LectioSyncStatus.model_validate(raw["status"])
            items = [_ITEM_MODELS[source].model_validate(item) for item in raw["items"]]
            key = (source, owner, start.isoformat(), end.isoformat())
            entries[key] = _CacheEntry(source, owner, start, end, items, status)
        statuses = {}
        for raw in payload.get("statuses", []):
            source = raw["source"]
            if source not in _ITEM_MODELS:
                raise ValueError("Unknown cached Lectio source status")
            owner = raw["owner"]
            statuses[(source, owner)] = LectioSyncStatus.model_validate(raw["status"])
        return entries, statuses

    def _owner_scope(self, session: AuthenticatedLectioSession) -> str:
        if self._owner_key is None:
            raise RuntimeError("Lectio data service has not been initialized")
        identity = f"{session.school_id}\0{session.student_id or ''}"
        if session.student_id is None:
            identity += f"\0{session.created_at.isoformat()}"
        return hmac.new(
            self._owner_key,
            identity.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _load_or_create_owner_key(self) -> bytes:
        if self._owner_key_path.exists():
            key = self._owner_key_path.read_bytes()
            if len(key) != 32:
                raise ValueError("Lectio cache key has an invalid length")
            return key

        self._owner_key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._owner_key_path.parent, 0o700)
        key = secrets.token_bytes(32)
        try:
            descriptor = os.open(
                self._owner_key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            existing = self._owner_key_path.read_bytes()
            if len(existing) != 32:
                raise ValueError("Lectio cache key has an invalid length")
            return existing
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(self._owner_key_path, 0o600)
        return key

    @staticmethod
    def _parse_cached_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Cache datetime is not timezone-aware")
        return parsed.astimezone(_UTC)

    def _write_cache(self, payload: str) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._cache_path.parent, 0o700)
        temporary = self._cache_path.parent / f".{self._cache_path.name}.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._cache_path)
            os.chmod(self._cache_path, 0o600)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _prune_cache(self) -> None:
        if len(self._entries) > _MAX_CACHE_ENTRIES:
            ordered = sorted(
                self._entries.items(),
                key=lambda pair: pair[1].status.last_attempt_at
                or datetime.min.replace(tzinfo=_UTC),
            )
            for key, _entry in ordered[: len(self._entries) - _MAX_CACHE_ENTRIES]:
                del self._entries[key]
        if len(self._statuses) > _MAX_STATUS_ENTRIES:
            ordered_statuses = sorted(
                self._statuses.items(),
                key=lambda pair: pair[1].last_attempt_at
                or datetime.min.replace(tzinfo=_UTC),
            )
            for key, _status in ordered_statuses[
                : len(self._statuses) - _MAX_STATUS_ENTRIES
            ]:
                del self._statuses[key]

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock must return a timezone-aware datetime")
        return now.astimezone(_UTC)

    @staticmethod
    def _normalize_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
        if (
            start.tzinfo is None
            or start.utcoffset() is None
            or end.tzinfo is None
            or end.utcoffset() is None
            or start >= end
        ):
            raise ValueError("Range must contain ordered timezone-aware datetimes")
        return start.astimezone(_UTC), end.astimezone(_UTC)
