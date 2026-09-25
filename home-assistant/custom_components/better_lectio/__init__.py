"""Home Assistant integration for the Better Lectio Gateway."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import GatewayApi
from .const import PLATFORMS
from .coordinator import LectioDataUpdateCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Better Lectio Gateway entry."""
    api = GatewayApi(hass, entry.data["url"])
    coordinator = LectioDataUpdateCoordinator(
        hass,
        api,
        entry,
        refresh_interval=entry.options.get("refresh_interval", 300),
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload platforms for a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
