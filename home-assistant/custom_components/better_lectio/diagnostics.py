"""Redacted diagnostics for Better Lectio Gateway entries."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_REFRESH_INTERVAL,
    DEFAULT_REFRESH_INTERVAL,
    INTEGRATION_VERSION,
    SOURCES,
)
from .coordinator import LectioDataUpdateCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return a deliberately allow-listed diagnostic summary."""
    coordinator = getattr(entry, "runtime_data", None)
    data = coordinator.data if isinstance(coordinator, LectioDataUpdateCoordinator) else None
    return {
        "integration_version": INTEGRATION_VERSION,
        "refresh_interval_seconds": entry.options.get(
            CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL
        ),
        "gateway_reachable": (
            coordinator.gateway_reachable
            if isinstance(coordinator, LectioDataUpdateCoordinator)
            else False
        ),
        "auth_state": data.auth["state"] if data is not None else "UNKNOWN",
        "student_id_available": (
            data.auth["student_id_available"] if data is not None else False
        ),
        "last_sync": {
            source: data.sources.get(source, {}) for source in SOURCES
        } if data is not None else {},
        "entity_status": {
            "calendar": data.sources.get("schedule", {}) if data is not None else {},
            "assignments": data.sources.get("assignments", {}) if data is not None else {},
            "homework": data.sources.get("homework", {}) if data is not None else {},
            "cancellations": (
                data.sources.get("cancellations", {}) if data is not None else {}
            ),
        },
    }
