"""Config flow for the Better Lectio Gateway."""

from __future__ import annotations

from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .api import GatewayApi, GatewayApiError
from .const import (
    CONF_API_TOKEN,
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
        """Ask for the gateway URL and scoped API token, then verify access."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_gateway_url(user_input[CONF_URL])
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                api_token = user_input[CONF_API_TOKEN].strip()
                api = GatewayApi(self.hass, url, api_token)
                try:
                    await api.get_status()
                except GatewayApiError as err:
                    errors["base"] = (
                        "invalid_auth" if err.status == 401 else "cannot_connect"
                    )
                else:
                    await self.async_set_unique_id(url)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="Better Lectio",
                        data={CONF_URL: url, CONF_API_TOKEN: api_token},
                        options={CONF_REFRESH_INTERVAL: DEFAULT_REFRESH_INTERVAL},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL): str,
                    vol.Required(CONF_API_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, str] | None = None):
        """Update gateway credentials without replacing the config entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_gateway_url(user_input[CONF_URL])
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                api_token = user_input[CONF_API_TOKEN].strip()
                api = GatewayApi(self.hass, url, api_token)
                try:
                    await api.get_status()
                except GatewayApiError as err:
                    errors["base"] = (
                        "invalid_auth" if err.status == 401 else "cannot_connect"
                    )
                else:
                    await self.async_set_unique_id(url)
                    self._abort_if_unique_id_configured()
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={CONF_URL: url, CONF_API_TOKEN: api_token},
                        unique_id=url,
                    )
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_URL, default=entry.data.get(CONF_URL, "")
                    ): str,
                    vol.Required(CONF_API_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
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
