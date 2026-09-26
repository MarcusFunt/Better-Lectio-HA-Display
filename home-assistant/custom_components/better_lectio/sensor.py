"""Gateway session, sync, and cancellation sensors."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import LectioDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Create gateway status sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            LectioCancellationsSensor(entry, coordinator),
            LectioSessionStatusSensor(entry, coordinator),
            LectioLastSyncSensor(entry, coordinator),
        ]
    )


class _LectioSensor(SensorEntity):
    """Common coordinator-backed sensor behavior."""

    _attr_has_entity_name = True

    def __init__(
        self, entry: ConfigEntry, coordinator: LectioDataUpdateCoordinator, suffix: str
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._remove_coordinator_listener = coordinator.async_add_listener(
            self._coordinator_updated
        )

    @property
    def available(self) -> bool:
        """Keep gateway status available after its first successful response."""
        return self._coordinator.has_gateway_status

    def _coordinator_updated(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from coordinator updates when unloaded."""
        self._remove_coordinator_listener()


class LectioCancellationsSensor(_LectioSensor):
    """Count and expose structured upcoming cancellation records."""

    _attr_name = "Lectio cancellations"
    def __init__(self, entry: ConfigEntry, coordinator: LectioDataUpdateCoordinator):
        super().__init__(entry, coordinator, "cancellations")

    @property
    def available(self) -> bool:
        """Expose cancellation data after its first successful source response."""
        return self._coordinator.has_source_succeeded("cancellations")

    @property
    def native_value(self) -> int | None:
        data = self._coordinator.data
        return len(data.items["cancellations"]) if data is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._coordinator.data
        if data is None:
            return {"cancellations": []}
        return {
            "cancellations": [
                _cancellation_record(item)
                for item in data.items["cancellations"][:100]
            ],
            "truncated": len(data.items["cancellations"]) > 100,
            "sync": data.sources.get("cancellations", {}),
        }


class LectioSessionStatusSensor(_LectioSensor):
    """Report gateway Lectio session health without exposing identifiers."""

    _attr_name = "Lectio session status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, coordinator: LectioDataUpdateCoordinator):
        super().__init__(entry, coordinator, "session_status")

    @property
    def native_value(self) -> str:
        data = self._coordinator.data
        return data.auth["state"] if data is not None else "UNKNOWN"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._coordinator.data
        return {
            "student_id_available": (
                data.auth["student_id_available"] if data is not None else False
            ),
            "last_verified_at": data.auth["last_verified_at"] if data is not None else None,
            "gateway_reachable": self._coordinator.gateway_reachable,
        }


class LectioLastSyncSensor(_LectioSensor):
    """Report the most recent successful source synchronization."""

    _attr_name = "Lectio last sync"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, coordinator: LectioDataUpdateCoordinator):
        super().__init__(entry, coordinator, "last_sync")

    @property
    def available(self) -> bool:
        """Expose the last successful time after any source has succeeded."""
        return self._coordinator.has_any_source_succeeded

    @property
    def native_value(self) -> datetime | None:
        data = self._coordinator.data
        if data is None:
            return None
        values = [
            dt_util.parse_datetime(sync["last_successful_sync"])
            for sync in data.sources.values()
            if sync.get("last_successful_sync")
        ]
        valid_values = [value for value in values if value is not None]
        return max(valid_values) if valid_values else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._coordinator.data
        return {
            "gateway_reachable": self._coordinator.gateway_reachable,
            "sources": data.sources if data is not None else {},
        }


def _cancellation_record(item: dict[str, Any]) -> dict[str, Any]:
    """Return only the supported structured cancellation fields."""
    original = item.get("original_lesson")
    original = original if isinstance(original, dict) else {}
    return {
        key: value
        for key, value in {
            "id": item.get("id"),
            "start": item.get("start"),
            "end": item.get("end"),
            "subject": item.get("subject") or original.get("subject"),
            "teacher": item.get("teacher") or original.get("teacher"),
            "room": item.get("room") or original.get("room"),
            "reason": item.get("reason"),
            "details": item.get("details"),
        }.items()
        if value is None or isinstance(value, str | int | float | bool)
    }
