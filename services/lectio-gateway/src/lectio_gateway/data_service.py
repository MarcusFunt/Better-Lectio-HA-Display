import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from lectio_gateway.lectio.client import LectioClient, derive_cancellations
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
_COPENHAGEN = ZoneInfo("Europe/Copenhagen")
_SOURCES = ("schedule", "assignments", "homework", "cancellations")
_METHODS = {"schedule", "assignments", "homework", "cancellations"}
_ITEM_MODELS: dict[str, type[BaseModel]] = {
    "schedule": LectioLesson,
    "assignments": LectioAssignment,
    "homework": LectioHomework,
    "cancellations": LectioCancellation,
}
_CACHE_VERSION = 2
_LEGACY_CACHE_VERSION = 1
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


@dataclass(frozen=True)
class _ScheduleWeekEntry:
    owner: str
    iso_year: int
    iso_week: int
    items: list[LectioLesson]
    status: LectioSyncStatus

    @property
    def source(self) -> str:
        return "schedule"


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
        self._schedule_weeks: dict[tuple[str, int, int], _ScheduleWeekEntry] = {}
        self._statuses: dict[tuple[str, str], LectioSyncStatus] = {}
        self._locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}
        self._week_locks: dict[tuple[str, int, int], asyncio.Lock] = {}
        self._persist_lock = asyncio.Lock()

    async def initialize(self) -> None:
        try:
            self._owner_key = await asyncio.to_thread(self._load_or_create_owner_key)
        except (OSError, ValueError):
            _LOGGER.warning("Could not load the Lectio cache ownership key")
            self._owner_key = secrets.token_bytes(32)
            self._entries = {}
            self._schedule_weeks = {}
            self._statuses = {}
            return
        try:
            (
                self._entries,
                self._statuses,
                self._schedule_weeks,
            ) = await asyncio.to_thread(self._read_cache)
        except (OSError, ValueError, ValidationError, TypeError, KeyError):
            _LOGGER.warning("Could not restore the Lectio data cache")
            self._entries = {}
            self._schedule_weeks = {}
            self._statuses = {}
        cached_entries = [*self._entries.values(), *self._schedule_weeks.values()]
        for entry in cached_entries:
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
        if source in {"assignments", "homework"}:
            start, end = _canonical_day_range(start, end)
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

        if source == "schedule":
            active_session = self._session_provider()
            if active_session is None:
                status = LectioSyncStatus(
                    state="error",
                    last_attempt_at=self._now(),
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
            try:
                client = self._client_factory(active_session)
            except Exception as exc:
                _LOGGER.warning(
                    "Lectio schedule client setup failed (%s)", type(exc).__name__
                )
                client = None
            return await self._get_schedule_range(
                active_session, client, start, end
            )

        if source == "cancellations":
            try:
                client = self._client_factory(session)
            except Exception as exc:
                _LOGGER.warning(
                    "Lectio cancellation client setup failed (%s)",
                    type(exc).__name__,
                )
                client = None
            lessons, status = await self._get_schedule_range(
                session, client, start, end
            )
            self._require_current_owner(owner)
            items = derive_cancellations(lessons)
            self._statuses[(source, owner)] = status
            await self._persist_cache()
            return items, status

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
                if source == "homework":
                    try:
                        client = self._client_factory(session)
                    except Exception as exc:
                        _LOGGER.warning(
                            "Lectio homework client setup failed (%s)",
                            type(exc).__name__,
                        )
                        client = None
                    try:
                        _lessons, dependency_status = await self._get_schedule_range(
                            session, client, start, end
                        )
                    except LectioSourceUnavailable as exc:
                        self._require_current_owner(owner)
                        return await self._record_failure(
                            key,
                            source,
                            entry,
                            now,
                            state="stale",
                            error=str(exc),
                        )
                    reported = _combine_dependency_status(
                        entry.status, dependency_status
                    )
                    self._statuses[(source, owner)] = reported
                    await self._persist_cache()
                    return entry.items, reported
                return entry.items, entry.status

            dependency_status = None
            homework_lessons: list[LectioLesson] = []
            client = None
            if source == "homework":
                try:
                    client = self._client_factory(session)
                except Exception as exc:
                    _LOGGER.warning(
                        "Lectio homework client setup failed (%s)", type(exc).__name__
                    )
                try:
                    homework_lessons, dependency_status = (
                        await self._get_schedule_range(
                            session, client, start, end
                        )
                    )
                except LectioSourceUnavailable as exc:
                    self._require_current_owner(owner)
                    return await self._record_failure(
                        key,
                        source,
                        entry,
                        now,
                        state="stale" if entry else "error",
                        error=str(exc),
                    )

            try:
                if source == "homework":
                    if client is None:
                        raise LectioAdapterError("Lectio client is unavailable")
                    items = await client.get_homework(
                        start, end, lessons=homework_lessons
                    )
                else:
                    client = self._client_factory(session)
                    items = await client.get_assignments(start, end)
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
                        dependency_status=dependency_status,
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
                    dependency_status=dependency_status,
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
                    dependency_status=dependency_status,
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
                    dependency_status=dependency_status,
                )

            status = LectioSyncStatus(
                state="valid",
                last_attempt_at=now,
                last_successful_sync=now,
                is_stale=False,
            )
            self._entries[key] = _CacheEntry(source, owner, start, end, items, status)
            reported_status = (
                _combine_dependency_status(status, dependency_status)
                if dependency_status is not None
                else status
            )
            self._statuses[(source, owner)] = reported_status
            self._prune_cache()
            await self._persist_cache()
            self._require_current_owner(owner)
            return items, reported_status

    async def _get_schedule_range(
        self,
        session: AuthenticatedLectioSession,
        client: LectioClient | None,
        start: datetime,
        end: datetime,
    ) -> tuple[list[LectioLesson], LectioSyncStatus]:
        """Compose the caller's range from canonical Copenhagen ISO weeks."""
        owner = self._owner_scope(session)
        local_start = start.astimezone(_COPENHAGEN)
        local_end = end.astimezone(_COPENHAGEN)
        last_local_day = (local_end - timedelta(microseconds=1)).date()
        week_monday = local_start.date() - timedelta(days=local_start.weekday())
        week_keys: list[tuple[int, int]] = []
        while week_monday <= last_local_day:
            iso_year, iso_week, _ = week_monday.isocalendar()
            week_keys.append((iso_year, iso_week))
            week_monday += timedelta(days=7)

        pages = await asyncio.gather(
            *(
                self._get_schedule_week(session, client, iso_year, iso_week)
                for iso_year, iso_week in week_keys
            )
        )
        items_by_id: dict[str, LectioLesson] = {}
        for entry, _status in pages:
            if entry is not None:
                for lesson in entry.items:
                    items_by_id[lesson.id] = lesson

        usable_pages = [entry for entry, _status in pages if entry is not None]
        status = _aggregate_schedule_status(pages, self._now())
        self._statuses[("schedule", owner)] = status
        await self._persist_cache()
        if not usable_pages:
            if status.state == "expired":
                raise LectioAuthenticationRequired(
                    status.error or "Lectio session expired. Please sign in again."
                )
            raise LectioSourceUnavailable(
                status.error or "The Lectio schedule request failed."
            )

        filtered = [
            lesson
            for lesson in items_by_id.values()
            if lesson.start < end and lesson.end > start
        ]
        return sorted(filtered, key=lambda lesson: lesson.start), status

    async def _get_schedule_week(
        self,
        session: AuthenticatedLectioSession,
        client: LectioClient | None,
        iso_year: int,
        iso_week: int,
    ) -> tuple[_ScheduleWeekEntry | None, LectioSyncStatus]:
        owner = self._owner_scope(session)
        key = (owner, iso_year, iso_week)
        lock = self._week_locks.setdefault(key, asyncio.Lock())
        async with lock:
            active_session = self._session_provider()
            if active_session is None:
                raise LectioAuthenticationRequired(
                    "Lectio authentication is required."
                )
            if self._owner_scope(active_session) != owner:
                raise LectioSourceUnavailable(
                    "The Lectio session changed during this request. Try again."
                )

            now = self._now()
            entry = self._schedule_weeks.get(key)
            last_success = entry.status.last_successful_sync if entry else None
            if (
                entry is not None
                and entry.status.state == "valid"
                and last_success is not None
                and (now - last_success).total_seconds() < self._ttl_seconds
            ):
                return entry, entry.status

            fallback = entry or self._legacy_schedule_week(owner, iso_year, iso_week)
            try:
                if client is None:
                    raise LectioAdapterError(
                        "The Lectio client could not be initialized"
                    )
                items = await client.get_schedule_week(iso_year, iso_week)
                if not isinstance(items, list) or any(
                    not isinstance(item, LectioLesson) for item in items
                ):
                    raise LectioResponseChanged(
                        "Lectio adapter returned an invalid schedule-week result"
                    )
                self._require_current_owner(owner)
            except LectioSourceUnavailable:
                raise
            except LectioSessionExpired:
                self._require_current_owner(owner)
                if self._on_session_expired is not None:
                    self._on_session_expired()
                status = self._schedule_week_failure(
                    fallback,
                    now,
                    state="expired",
                    error="Lectio session expired. Please sign in again.",
                )
            except LectioResponseChanged:
                self._require_current_owner(owner)
                status = self._schedule_week_failure(
                    fallback,
                    now,
                    state="stale" if fallback is not None else "error",
                    error="Lectio returned a response the gateway could not read.",
                )
            except LectioAdapterError:
                self._require_current_owner(owner)
                status = self._schedule_week_failure(
                    fallback,
                    now,
                    state="stale" if fallback is not None else "error",
                    error="The Lectio schedule request failed.",
                )
            except Exception as exc:
                self._require_current_owner(owner)
                _LOGGER.warning(
                    "Lectio schedule week request failed (%s)", type(exc).__name__
                )
                status = self._schedule_week_failure(
                    fallback,
                    now,
                    state="stale" if fallback is not None else "error",
                    error="The Lectio schedule request failed.",
                )
            else:
                status = LectioSyncStatus(
                    state="valid",
                    last_attempt_at=now,
                    last_successful_sync=now,
                    is_stale=False,
                )
                entry = _ScheduleWeekEntry(
                    owner, iso_year, iso_week, items, status
                )
                self._schedule_weeks[key] = entry
                self._prune_cache()
                await self._persist_cache()
                self._require_current_owner(owner)
                return entry, status

            if entry is not None:
                entry = _ScheduleWeekEntry(
                    owner, iso_year, iso_week, entry.items, status
                )
                self._schedule_weeks[key] = entry
            self._statuses[("schedule", owner)] = status
            await self._persist_cache()
            if fallback is not None:
                return (
                    _ScheduleWeekEntry(
                        owner, iso_year, iso_week, fallback.items, status
                    ),
                    status,
                )
            return None, status

    def _legacy_schedule_week(
        self, owner: str, iso_year: int, iso_week: int
    ) -> _ScheduleWeekEntry | None:
        week_start, week_end = _iso_week_bounds_utc(iso_year, iso_week)
        candidates = [
            entry
            for entry in self._entries.values()
            if entry.source == "schedule"
            and entry.owner == owner
            and entry.start <= week_start
            and entry.end >= week_end
        ]
        if not candidates:
            return None
        legacy = max(
            candidates,
            key=lambda entry: entry.status.last_successful_sync
            or datetime.min.replace(tzinfo=_UTC),
        )
        return _ScheduleWeekEntry(
            owner, iso_year, iso_week, legacy.items, legacy.status
        )

    @staticmethod
    def _schedule_week_failure(
        fallback: _ScheduleWeekEntry | None,
        now: datetime,
        *,
        state: Literal["stale", "expired", "error"],
        error: str,
    ) -> LectioSyncStatus:
        return LectioSyncStatus(
            state=state,
            last_attempt_at=now,
            last_successful_sync=(
                fallback.status.last_successful_sync if fallback is not None else None
            ),
            is_stale=fallback is not None,
            error=error,
        )

    async def _record_failure(
        self,
        key: tuple[str, str, str, str],
        source: str,
        entry: _CacheEntry | None,
        now: datetime,
        *,
        state: Literal["stale", "expired", "error"],
        error: str,
        dependency_status: LectioSyncStatus | None = None,
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
        reported_status = (
            _combine_dependency_status(status, dependency_status)
            if dependency_status is not None
            else status
        )
        self._statuses[(source, owner)] = reported_status
        if entry is not None:
            self._entries[key] = _CacheEntry(
                source, owner, entry.start, entry.end, entry.items, status
            )
        await self._persist_cache()
        if entry is not None:
            self._require_current_owner(owner)
            return entry.items, reported_status
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
        schedule_weeks = [
            {
                "owner": entry.owner,
                "iso_year": entry.iso_year,
                "iso_week": entry.iso_week,
                "items": [item.model_dump(mode="json") for item in entry.items],
                "status": entry.status.model_dump(mode="json"),
            }
            for entry in self._schedule_weeks.values()
        ]
        statuses = [
            {
                "source": source,
                "owner": owner,
                "status": status.model_dump(mode="json"),
            }
            for (source, owner), status in self._statuses.items()
        ]
        return json.dumps(
            {
                "version": _CACHE_VERSION,
                "entries": entries,
                "schedule_weeks": schedule_weeks,
                "statuses": statuses,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _read_cache(
        self,
    ) -> tuple[
        dict[tuple[str, str, str, str], _CacheEntry],
        dict[tuple[str, str], LectioSyncStatus],
        dict[tuple[str, int, int], _ScheduleWeekEntry],
    ]:
        if not self._cache_path.exists():
            return {}, {}, {}
        payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        version = payload.get("version")
        if version not in {_LEGACY_CACHE_VERSION, _CACHE_VERSION} or not isinstance(
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
        schedule_weeks: dict[tuple[str, int, int], _ScheduleWeekEntry] = {}
        raw_schedule_weeks = payload.get("schedule_weeks", [])
        if not isinstance(raw_schedule_weeks, list):
            raise ValueError("Invalid cached schedule weeks")
        for raw in raw_schedule_weeks:
            owner = raw["owner"]
            iso_year = raw["iso_year"]
            iso_week = raw["iso_week"]
            _iso_week_bounds_utc(iso_year, iso_week)
            status = LectioSyncStatus.model_validate(raw["status"])
            items = [LectioLesson.model_validate(item) for item in raw["items"]]
            key = (owner, iso_year, iso_week)
            schedule_weeks[key] = _ScheduleWeekEntry(
                owner, iso_year, iso_week, items, status
            )
        statuses = {}
        for raw in payload.get("statuses", []):
            source = raw["source"]
            if source not in _ITEM_MODELS:
                raise ValueError("Unknown cached Lectio source status")
            owner = raw["owner"]
            statuses[(source, owner)] = LectioSyncStatus.model_validate(raw["status"])
        return entries, statuses, schedule_weeks

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
        if len(self._schedule_weeks) > _MAX_CACHE_ENTRIES:
            ordered_weeks = sorted(
                self._schedule_weeks.items(),
                key=lambda pair: pair[1].status.last_attempt_at
                or datetime.min.replace(tzinfo=_UTC),
            )
            for key, _entry in ordered_weeks[
                : len(self._schedule_weeks) - _MAX_CACHE_ENTRIES
            ]:
                del self._schedule_weeks[key]
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


def _iso_week_bounds_utc(iso_year: int, iso_week: int) -> tuple[datetime, datetime]:
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    next_monday = monday + timedelta(days=7)
    start = datetime(
        monday.year, monday.month, monday.day, tzinfo=_COPENHAGEN
    ).astimezone(_UTC)
    end = datetime(
        next_monday.year,
        next_monday.month,
        next_monday.day,
        tzinfo=_COPENHAGEN,
    ).astimezone(_UTC)
    return start, end


def _canonical_day_range(
    start: datetime, end: datetime
) -> tuple[datetime, datetime]:
    """Align a source query to Copenhagen midnight boundaries."""
    local_start = start.astimezone(_COPENHAGEN)
    local_end = end.astimezone(_COPENHAGEN)
    end_day = local_end.date()
    if any((local_end.hour, local_end.minute, local_end.second, local_end.microsecond)):
        end_day += timedelta(days=1)
    canonical_start = datetime(
        local_start.year, local_start.month, local_start.day, tzinfo=_COPENHAGEN
    )
    canonical_end = datetime(
        end_day.year, end_day.month, end_day.day, tzinfo=_COPENHAGEN
    )
    return canonical_start.astimezone(_UTC), canonical_end.astimezone(_UTC)


def _combine_dependency_status(
    source_status: LectioSyncStatus,
    dependency_status: LectioSyncStatus,
) -> LectioSyncStatus:
    has_source_data = source_status.last_successful_sync is not None
    attempts = [
        attempt
        for attempt in (
            source_status.last_attempt_at,
            dependency_status.last_attempt_at,
        )
        if attempt is not None
    ]
    successful = []
    if has_source_data:
        successful.append(source_status.last_successful_sync)
        if dependency_status.last_successful_sync is not None:
            successful.append(dependency_status.last_successful_sync)

    fully_fresh = (
        source_status.state == "valid"
        and not source_status.is_stale
        and dependency_status.state == "valid"
        and not dependency_status.is_stale
    )
    state = "valid" if fully_fresh else ("stale" if has_source_data else source_status.state)
    error = source_status.error or dependency_status.error
    return LectioSyncStatus(
        state=state,
        last_attempt_at=max(attempts, default=None),
        last_successful_sync=min(successful, default=None),
        is_stale=has_source_data and not fully_fresh,
        error=error,
    )


def _aggregate_schedule_status(
    pages: list[tuple[_ScheduleWeekEntry | None, LectioSyncStatus]],
    now: datetime,
) -> LectioSyncStatus:
    statuses = [status for _entry, status in pages]
    usable = [
        (entry, status)
        for entry, status in pages
        if entry is not None
    ]
    successful_times = [
        status.last_successful_sync
        for _entry, status in usable
        if status.last_successful_sync is not None
    ]
    attempt_times = [
        status.last_attempt_at
        for status in statuses
        if status.last_attempt_at is not None
    ]
    expired = any(status.state == "expired" for status in statuses)
    complete_and_fresh = len(usable) == len(pages) and all(
        status.state == "valid" and not status.is_stale
        for _entry, status in usable
    )
    if not usable:
        state = "expired" if expired else "error"
        is_stale = False
    elif complete_and_fresh:
        state = "valid"
        is_stale = False
    else:
        state = "expired" if expired else "stale"
        is_stale = True
    error = None
    if state != "valid":
        error = next(
            (status.error for status in statuses if status.error is not None),
            "The Lectio schedule is incomplete.",
        )
    return LectioSyncStatus(
        state=state,
        last_attempt_at=max(attempt_times, default=now),
        last_successful_sync=min(successful_times, default=None),
        is_stale=is_stale,
        error=error,
    )
