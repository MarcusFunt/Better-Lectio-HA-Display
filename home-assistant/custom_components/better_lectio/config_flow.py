"""Config flow for the Better Lectio Gateway."""

from __future__ import annotations

from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant import config_entries

from .api import GatewayApi, GatewayApiError
from .const import (
    CONF_REFRESH_INTERVAL,
    CONF_URL,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    MAX_REFRESH_INTERVAL,
    MIN_REFRESH_INTERVAL,
)


def normalize_gateway_url(value: str) -> str:
    """Validate and normalize the gateway base URL without credentials."""
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Enter a valid HTTP or HTTPS gateway URL.")
    try:
        parsed.port
    except ValueError as err:
        raise ValueError("Enter a valid gateway port.") from err
    return candidate


class LectioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up a connection to one local Lectio Gateway."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, str] | None = None):
        """Ask for the gateway URL and verify it is reachable."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_gateway_url(user_input[CONF_URL])
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                api = GatewayApi(self.hass, url)
                try:
                    await api.get_status()
                except GatewayApiError:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(url)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="Better Lectio",
                        data={CONF_URL: url},
                        options={CONF_REFRESH_INTERVAL: DEFAULT_REFRESH_INTERVAL},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_URL): str}),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Return the refresh interval options flow."""
        return LectioOptionsFlow()


class LectioOptionsFlow(config_entries.OptionsFlow):
    """Configure the polling interval."""

    async def async_step_init(self, user_input: dict[str, int] | None = None):
        """Show and save refresh behavior."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current_interval = self.config_entry.options.get(
            CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_REFRESH_INTERVAL, default=current_interval
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_REFRESH_INTERVAL, max=MAX_REFRESH_INTERVAL),
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


def validate_gateway_url_for_test(value: str) -> str:
    """Expose a pure validator used by focused integration tests."""
    return normalize_gateway_url(value)
