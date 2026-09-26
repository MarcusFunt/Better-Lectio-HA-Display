"""Lectio schedule calendar entity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import LectioDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Create the calendar entity."""
    async_add_entities([LectioCalendar(entry, entry.runtime_data)])


class LectioCalendar(CalendarEntity):
    """Expose schedule items as a read-only HA calendar."""

    _attr_has_entity_name = True
    _attr_name = "Lectio"

    def __init__(
        self, entry: ConfigEntry, coordinator: LectioDataUpdateCoordinator
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_schedule"
        self._remove_coordinator_listener = coordinator.async_add_listener(
            self._coordinator_updated
        )

    @property
    def available(self) -> bool:
        """Keep the last good schedule visible when a later poll fails."""
        return self._coordinator.has_source_succeeded("schedule")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose safe source freshness details."""
        data = self._coordinator.data
        return {
            "gateway_reachable": self._coordinator.gateway_reachable,
            "sync": data.sources.get("schedule", {}) if data is not None else {},
        }

    def _coordinator_updated(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from coordinator updates when unloaded."""
        self._remove_coordinator_listener()

    @property
    def event(self) -> CalendarEvent | None:
        """Return the nearest upcoming lesson for the calendar card."""
        if self._coordinator.data is None:
            return None
        now = dt_util.now()
        upcoming = []
        for item in self._coordinator.data.items["schedule"]:
            if item.get("status") == "cancelled" or not _lesson_overlaps_now_or_future(
                item, now
            ):
                continue
            try:
                upcoming.append(calendar_event_from_lesson(item))
            except (TypeError, ValueError):
                continue
        return min(upcoming, key=lambda event: event.start) if upcoming else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return lessons overlapping the requested date range."""
        if start_date.tzinfo is None or end_date.tzinfo is None or start_date >= end_date:
            return []
        lessons = await self._coordinator.async_get_schedule(start_date, end_date)
        events = []
        for lesson in lessons:
            if lesson.get("status") == "cancelled":
                continue
            try:
                events.append(calendar_event_from_lesson(lesson))
            except (TypeError, ValueError):
                continue
        return sorted(events, key=lambda event: event.start)


def calendar_event_from_lesson(lesson: dict[str, Any]) -> CalendarEvent:
    """Map one gateway lesson into a stable Home Assistant calendar event."""
    teacher = _nonempty(lesson.get("teacher"))
    details = _nonempty(lesson.get("details"))
    source_url = _nonempty(lesson.get("source_url"))
    description_parts = []
    if teacher:
        description_parts.append(f"Teacher: {teacher}")
    if details:
        description_parts.append(details)
    if source_url:
        description_parts.append(source_url)
    start = _parse_datetime(lesson.get("start"))
    end = _parse_datetime(lesson.get("end"))
    return CalendarEvent(
        start=start,
        end=end,
        summary=_nonempty(lesson.get("subject")) or "Lectio lesson",
        description="\n".join(description_parts) or None,
        location=_nonempty(lesson.get("room")),
        uid=_nonempty(lesson.get("id")) or _nonempty(lesson.get("source_id")),
    )


def _lesson_overlaps_now_or_future(lesson: dict[str, Any], now: datetime) -> bool:
    end = _parse_datetime(lesson.get("end"))
    return end > now


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value)
    else:
        raise ValueError("Gateway lesson has no valid date-time")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Gateway lesson date-times must include a timezone")
    return result


def _nonempty(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
