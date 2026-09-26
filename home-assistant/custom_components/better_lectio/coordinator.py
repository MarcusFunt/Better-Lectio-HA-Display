"""Independent polling and stale-data handling for Lectio sources."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api import GatewayApi, GatewayApiError
from .const import (
    DEFAULT_RANGE_DAYS_AFTER,
    DEFAULT_RANGE_DAYS_BEFORE,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    SOURCE_SCHEDULE,
    SOURCES,
)

_SYNC_STATES = {"unknown", "valid", "stale", "expired", "error"}
_AUTH_STATES = {
    "UNCONFIGURED",
    "LOGIN_REQUIRED",
    "STARTING_BROWSER",
    "WAITING_FOR_USER",
    "AUTHENTICATED",
    "SESSION_EXPIRED",
    "AUTH_FAILED",
    "UNKNOWN",
}


@dataclass(slots=True)
class LectioData:
    """Latest data and safe status for each independent gateway source."""

    items: dict[str, list[dict[str, Any]]]
    sources: dict[str, dict[str, Any]]
    auth: dict[str, Any]
    gateway_reachable: bool
    range_start: datetime
    range_end: datetime


class LectioDataUpdateCoordinator(DataUpdateCoordinator[LectioData]):
    """Poll gateway sources independently and retain last-good source data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: GatewayApi,
        config_entry: ConfigEntry,
        *,
        refresh_interval: int = DEFAULT_REFRESH_INTERVAL,
    ) -> None:
        self.api = api
        self._items = {source: [] for source in SOURCES}
        self._source_status: dict[str, dict[str, Any]] = {
            source: {"state": "unknown", "is_stale": False} for source in SOURCES
        }
        self._source_success = {source: False for source in SOURCES}
        self._has_gateway_status = False
        self._gateway_reachable = False
        self._last_schedule_range: tuple[
            datetime, datetime, list[dict[str, Any]]
        ] | None = None
        super().__init__(
            hass,
            logging.getLogger(__name__),
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=refresh_interval),
        )

    async def _async_update_data(self) -> LectioData:
        """Fetch gateway status and each data source, keeping failures isolated."""
        try:
            status = await self.api.get_status()
        except GatewayApiError as err:
            self._gateway_reachable = False
            self._mark_gateway_unreachable()
            raise UpdateFailed("Could not reach the Lectio Gateway.") from err

        self._gateway_reachable = True
        self._has_gateway_status = True
        now = dt_util.now()
        start = now - timedelta(days=DEFAULT_RANGE_DAYS_BEFORE)
        end = now + timedelta(days=DEFAULT_RANGE_DAYS_AFTER)
        responses = await asyncio.gather(
            *(
                self.api.get_source(source, start, end)
                for source in SOURCES
            ),
            return_exceptions=True,
        )
        auth_payload = status.get("auth", {})
        auth = {
            "state": _safe_auth_state(auth_payload.get("state")),
            "student_id_available": bool(auth_payload.get("student_id_available")),
            "last_verified_at": _safe_datetime(auth_payload.get("last_verified_at")),
        }
        gateway_sources = status.get("sources", {})

        for source, response in zip(SOURCES, responses, strict=True):
            fallback_sync = _safe_sync(gateway_sources.get(source))
            if isinstance(response, GatewayApiError):
                sync_state = "expired" if response.status == 401 else "error"
                previous = self._source_status[source]
                self._source_status[source] = {
                    **fallback_sync,
                    "state": sync_state,
                    "is_stale": self.has_source_succeeded(source),
                    "last_successful_sync": (
                        fallback_sync["last_successful_sync"]
                        or previous.get("last_successful_sync")
                    ),
                    "error": response.code,
                }
                if response.status == 401:
                    auth["state"] = "SESSION_EXPIRED"
                continue
            if isinstance(response, Exception):
                previous = self._source_status[source]
                self._source_status[source] = {
                    **fallback_sync,
                    "state": "error",
                    "is_stale": self.has_source_succeeded(source),
                    "last_successful_sync": (
                        fallback_sync["last_successful_sync"]
                        or previous.get("last_successful_sync")
                    ),
                    "error": "connection_failed",
                }
                continue

            self._items[source] = response["items"]
            self._source_status[source] = _safe_sync(response["sync"])
            self._source_success[source] = True

        return LectioData(
            items={source: list(self._items[source]) for source in SOURCES},
            sources={source: dict(self._source_status[source]) for source in SOURCES},
            auth=auth,
            gateway_reachable=True,
            range_start=start,
            range_end=end,
        )

    async def async_get_schedule(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Get lessons for a requested HA calendar window."""
        data = self.data
        if data is not None and data.range_start <= start and end <= data.range_end:
            lessons = data.items[SOURCE_SCHEDULE]
        else:
            cached = self._last_schedule_range
            try:
                response = await self.api.get_source(SOURCE_SCHEDULE, start, end)
            except GatewayApiError as err:
                has_matching_cache = (
                    cached is not None and cached[0] == start and cached[1] == end
                )
                self._mark_schedule_range_failure(stale=has_matching_cache)
                if not has_matching_cache:
                    raise UpdateFailed(
                        "Could not fetch the requested calendar range."
                    ) from err
                lessons = cached[2]
            else:
                lessons = response["items"]
                self._last_schedule_range = (start, end, lessons)
                self._source_status[SOURCE_SCHEDULE] = _safe_sync(response["sync"])
                if self.data is not None:
                    self.data.sources[SOURCE_SCHEDULE] = dict(
                        self._source_status[SOURCE_SCHEDULE]
                    )
                    self.async_update_listeners()
        return [
            item
            for item in lessons
            if _overlaps(item.get("start"), item.get("end"), start, end)
        ]

    @property
    def gateway_reachable(self) -> bool:
        """Whether the latest gateway status request succeeded."""
        return self._gateway_reachable

    def has_source_succeeded(self, source: str) -> bool:
        """Whether this category has returned a successful response at least once."""
        return self._source_success.get(source, False)

    @property
    def has_gateway_status(self) -> bool:
        """Whether gateway status has returned successfully at least once."""
        return self._has_gateway_status

    @property
    def has_any_source_succeeded(self) -> bool:
        """Whether any data category has returned a successful response."""
        return any(self._source_success.values())

    def _mark_gateway_unreachable(self) -> None:
        """Mark previously synchronized sources stale after a gateway outage."""
        for source in SOURCES:
            previous = self._source_status[source]
            has_good_sync = self.has_source_succeeded(source)
            self._source_status[source] = {
                **previous,
                "state": "stale" if has_good_sync else "error",
                "is_stale": has_good_sync,
                "error": "connection_failed",
            }
        if self.data is not None:
            self.data.gateway_reachable = False
            self.data.sources = {
                source: dict(self._source_status[source]) for source in SOURCES
            }

    def _mark_schedule_range_failure(self, *, stale: bool) -> None:
        """Record a sanitized failure from a calendar-specific range query."""
        stale = stale or self.has_source_succeeded(SOURCE_SCHEDULE)
        previous = self._source_status[SOURCE_SCHEDULE]
        self._source_status[SOURCE_SCHEDULE] = {
            **previous,
            "state": "stale" if stale else "error",
            "is_stale": stale,
            "error": "connection_failed",
        }
        if self.data is not None:
            self.data.sources[SOURCE_SCHEDULE] = dict(
                self._source_status[SOURCE_SCHEDULE]
            )
            self.async_update_listeners()


def _safe_sync(value: Any) -> dict[str, Any]:
    """Project gateway sync metadata onto non-sensitive fields."""
    if not isinstance(value, dict):
        value = {}
    state = value.get("state")
    if not isinstance(state, str) or state not in _SYNC_STATES:
        state = "unknown"
    result: dict[str, Any] = {
        "state": state,
        "is_stale": bool(value.get("is_stale", state == "stale")),
        "last_successful_sync": _safe_datetime(value.get("last_successful_sync")),
    }
    return result


def _safe_auth_state(value: Any) -> str:
    return value if isinstance(value, str) and value in _AUTH_STATES else "UNKNOWN"


def _safe_datetime(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo is not None else None


def _overlaps(
    start_value: Any, end_value: Any, start: datetime, end: datetime
) -> bool:
    try:
        item_start = datetime.fromisoformat(start_value)
        item_end = datetime.fromisoformat(end_value)
    except (TypeError, ValueError):
        return False
    return (
        item_start.tzinfo is not None
        and item_end.tzinfo is not None
        and item_start < end
        and item_end > start
    )
