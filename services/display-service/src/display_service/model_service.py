"""Fetch HA inputs independently and assemble a model with last-good fallback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .ha_client import HomeAssistantApiError, HomeAssistantClient
from .model import DisplayModel
from .model_builder import (
    ASSIGNMENTS_ENTITY_ID,
    CANCELLATIONS_ENTITY_ID,
    HOMEWORK_ENTITY_ID,
    LECTIO_CALENDAR_ENTITY_ID,
    build_display_model,
    display_window,
)


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Safe freshness state for a fetched Home Assistant source."""

    state: str
    error: str | None = None
    is_stale: bool = False
    last_successful_sync: str | None = None


@dataclass(frozen=True, slots=True)
class DisplayModelResult:
    """Model and source freshness returned by one independent fetch cycle."""

    model: DisplayModel
    sources: dict[str, SourceStatus]


class DisplayModelService:
    """Collect display inputs from HA without coupling source availability."""

    def __init__(
        self,
        client: HomeAssistantClient,
        *,
        private_calendar_entity_ids: tuple[str, ...] = ("calendar.private",),
        max_sidebar_items: int = 8,
    ) -> None:
        if max_sidebar_items < 0:
            raise ValueError("max_sidebar_items cannot be negative")
        entity_ids = tuple(dict.fromkeys(private_calendar_entity_ids))
        if LECTIO_CALENDAR_ENTITY_ID in entity_ids:
            raise ValueError("Lectio calendar cannot also be a private calendar")
        self._client = client
        self._private_calendar_entity_ids = entity_ids
        self._max_sidebar_items = max_sidebar_items
        self._last_good: dict[str, Any] = {}
        self._last_good_sync: dict[str, dict[str, Any]] = {}

    async def async_build(self, *, now: datetime | None = None) -> DisplayModelResult:
        """Fetch inputs concurrently and build a model, using per-source cache."""
        start, end, _ = display_window(now)
        calls: dict[str, Callable[[], Any]] = {
            LECTIO_CALENDAR_ENTITY_ID: lambda: self._client.async_get_calendar_events(
                LECTIO_CALENDAR_ENTITY_ID, start, end
            ),
            ASSIGNMENTS_ENTITY_ID: lambda: self._client.async_get_todo_items(
                ASSIGNMENTS_ENTITY_ID
            ),
            HOMEWORK_ENTITY_ID: lambda: self._client.async_get_todo_items(
                HOMEWORK_ENTITY_ID
            ),
            CANCELLATIONS_ENTITY_ID: lambda: self._client.async_get_cancellations(
                CANCELLATIONS_ENTITY_ID
            ),
        }
        for entity_id in self._private_calendar_entity_ids:
            calls[entity_id] = lambda entity_id=entity_id: self._client.async_get_calendar_events(
                entity_id, start, end
            )

        keys = tuple(calls)
        sync_keys = tuple(
            key
            for key in keys
            if key
            in {
                LECTIO_CALENDAR_ENTITY_ID,
                ASSIGNMENTS_ENTITY_ID,
                HOMEWORK_ENTITY_ID,
                CANCELLATIONS_ENTITY_ID,
            }
        )
        results, sync_results = await asyncio.gather(
            asyncio.gather(*(calls[key]() for key in keys), return_exceptions=True),
            asyncio.gather(
                *(
                    self._client.async_get_entity_sync(key)
                    for key in sync_keys
                ),
                return_exceptions=True,
            ),
        )
        for result in sync_results:
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result
        sync_values = dict(zip(sync_keys, sync_results, strict=True))
        sources: dict[str, SourceStatus] = {}
        values: dict[str, Any] = {}
        for key, result in zip(keys, results, strict=True):
            sync_value = sync_values.get(key)
            if isinstance(sync_value, BaseException):
                cached_sync = self._last_good_sync.get(key)
            else:
                cached_sync = _normalize_sync(sync_value)
                self._last_good_sync[key] = cached_sync
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                has_last_good = key in self._last_good
                sources[key] = SourceStatus(
                    state="stale" if has_last_good else "error",
                    error=_safe_error_code(result),
                    is_stale=has_last_good,
                    last_successful_sync=(
                        cached_sync.get("last_successful_sync")
                        if cached_sync is not None
                        else None
                    ),
                )
                values[key] = self._last_good.get(key, [])
            else:
                self._last_good[key] = result
                if key not in sync_values:
                    sources[key] = SourceStatus(state="valid")
                elif isinstance(sync_value, BaseException):
                    sources[key] = SourceStatus(
                        state="stale" if cached_sync else "unknown",
                        error=_safe_error_code(sync_value),
                        is_stale=cached_sync is not None,
                        last_successful_sync=(
                            cached_sync.get("last_successful_sync")
                            if cached_sync is not None
                            else None
                        ),
                    )
                else:
                    assert cached_sync is not None
                    sources[key] = SourceStatus(
                        state=cached_sync["state"],
                        is_stale=cached_sync["is_stale"],
                        last_successful_sync=cached_sync["last_successful_sync"],
                    )
                values[key] = result

        private_events = [
            {**event, "_display_calendar_entity_id": entity_id}
            for entity_id in self._private_calendar_entity_ids
            for event in values.get(entity_id, [])
        ]
        model = build_display_model(
            now=now,
            lectio_events=values.get(LECTIO_CALENDAR_ENTITY_ID, []),
            private_events=private_events,
            assignments=values.get(ASSIGNMENTS_ENTITY_ID, []),
            homework=values.get(HOMEWORK_ENTITY_ID, []),
            cancellations=values.get(CANCELLATIONS_ENTITY_ID, []),
            max_sidebar_items=self._max_sidebar_items,
        )
        return DisplayModelResult(model=model, sources=sources)


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, HomeAssistantApiError) and error.code in {
        "unauthorized",
        "forbidden",
        "not_found",
        "ha_error",
        "invalid_response",
        "connection_failed",
        "entity_unavailable",
    }:
        return error.code
    return "request_failed"


def _normalize_sync(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {
            "state": "unknown",
            "is_stale": False,
            "last_successful_sync": None,
        }
    state = value.get("state")
    if not isinstance(state, str) or state not in {
        "unknown",
        "valid",
        "stale",
        "expired",
        "error",
    }:
        state = "unknown"
    last_successful_sync = value.get("last_successful_sync")
    if isinstance(last_successful_sync, str):
        try:
            parsed = datetime.fromisoformat(last_successful_sync)
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
            last_successful_sync = None
        else:
            last_successful_sync = parsed.isoformat()
    else:
        last_successful_sync = None
    return {
        "state": state,
        "is_stale": bool(value.get("is_stale", state == "stale")),
        "last_successful_sync": last_successful_sync,
    }
