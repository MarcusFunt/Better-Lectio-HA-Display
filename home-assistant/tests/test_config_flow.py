import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntry

from custom_components.better_lectio import config_flow
from custom_components.better_lectio.api import GatewayApiError
from custom_components.better_lectio.config_flow import (
    LectioConfigFlow,
    LectioOptionsFlow,
    normalize_gateway_url,
)


def test_normalize_gateway_url_removes_trailing_slash():
    assert normalize_gateway_url(" http://ha-gateway.local:8000/ ") == (
        "http://ha-gateway.local:8000"
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://gateway.local",
        "http://user:password@gateway.local",
        "http://gateway.local?token=secret",
        "http://gateway.local/#private",
        "http://gateway.local:not-a-port",
    ],
)
def test_normalize_gateway_url_rejects_invalid_or_credentialed_urls(url):
    with pytest.raises(ValueError):
        normalize_gateway_url(url)


def test_options_flow_uses_home_assistants_config_entry_property():
    entry = ConfigEntry(
        domain="better_lectio",
        discovery_keys={},
        entry_id="test-entry",
        minor_version=1,
        options={},
        source=SOURCE_USER,
        subentries_data=[],
        title="Better Lectio",
        unique_id="http://gateway.local",
        version=1,
        data={"url": "http://gateway.local"},
    )

    flow = LectioConfigFlow.async_get_options_flow(entry)

    assert isinstance(flow, LectioOptionsFlow)


class TestableConfigFlow(LectioConfigFlow):
    __test__ = False

    def __init__(self):
        self.hass = SimpleNamespace()
        self.entry = None

    async def async_set_unique_id(self, unique_id):
        self.test_unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        return None

    def async_create_entry(self, **result):
        return {"type": "create_entry", **result}

    def async_show_form(self, **result):
        return {"type": "form", **result}

    def _get_reconfigure_entry(self):
        return self.entry

    def async_abort(self, **result):
        return {"type": "abort", **result}


def test_config_flow_collects_token_and_validates_gateway(monkeypatch):
    token = "x" * 43
    calls = []

    class FakeApi:
        def __init__(self, hass, url, api_token=None):
            calls.append((url, api_token))

        async def get_status(self):
            return {"auth": {}, "sources": {}}

    monkeypatch.setattr(config_flow, "GatewayApi", FakeApi)
    flow = TestableConfigFlow()

    result = asyncio.run(
        flow.async_step_user({"url": "http://lectio-host.local:8002/", "api_token": token})
    )

    assert calls == [("http://lectio-host.local:8002", token)]
    assert result["type"] == "create_entry"
    assert result["data"] == {"url": "http://lectio-host.local:8002", "api_token": token}


def test_config_flow_reports_invalid_api_token(monkeypatch):
    class UnauthorizedApi:
        def __init__(self, hass, url, api_token=None):
            pass

        async def get_status(self):
            raise GatewayApiError(401)

    monkeypatch.setattr(config_flow, "GatewayApi", UnauthorizedApi)
    flow = TestableConfigFlow()

    result = asyncio.run(
        flow.async_step_user({"url": "http://lectio-host.local:8002", "api_token": "x" * 43})
    )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_auth"


def test_config_flow_form_requires_password_token():
    flow = TestableConfigFlow()

    result = asyncio.run(flow.async_step_user())

    fields = {field.schema for field in result["data_schema"].schema}
    assert fields == {"url", "api_token"}
    token_field = next(
        selector
        for field, selector in result["data_schema"].schema.items()
        if field.schema == "api_token"
    )
    assert token_field.config["type"] == "password"


def test_reconfigure_flow_updates_credentials_and_reloads_same_entry(monkeypatch):
    token = "y" * 43
    calls = []

    class FakeApi:
        def __init__(self, hass, url, api_token=None):
            calls.append(("api", url, api_token))

        async def get_status(self):
            return {"auth": {}, "sources": {}}

    class FakeConfigEntries:
        def async_update_entry(self, entry, **changes):
            calls.append(("update", entry, changes))

        async def async_reload(self, entry_id):
            calls.append(("reload", entry_id))

    monkeypatch.setattr(config_flow, "GatewayApi", FakeApi)
    flow = TestableConfigFlow()
    flow.entry = ConfigEntry(
        domain="better_lectio",
        discovery_keys={},
        entry_id="stable-entry-id",
        minor_version=1,
        options={"refresh_interval": 300},
        source=SOURCE_USER,
        subentries_data=[],
        title="Better Lectio",
        unique_id="http://old-gateway.local",
        version=1,
        data={"url": "http://old-gateway.local", "api_token": "old-token"},
    )
    flow.hass.config_entries = FakeConfigEntries()

    result = asyncio.run(
        flow.async_step_reconfigure(
            {"url": "http://new-gateway.local/", "api_token": token}
        )
    )

    assert calls[0] == ("api", "http://new-gateway.local", token)
    assert calls[1] == (
        "update",
        flow.entry,
        {
            "data": {"url": "http://new-gateway.local", "api_token": token},
            "unique_id": "http://new-gateway.local",
        },
    )
    assert calls[2] == ("reload", "stable-entry-id")
    assert result == {"type": "abort", "reason": "reconfigure_successful"}
    assert flow.test_unique_id == "http://new-gateway.local"


def test_reconfigure_flow_keeps_existing_entry_when_new_token_is_rejected(monkeypatch):
    class UnauthorizedApi:
        def __init__(self, hass, url, api_token=None):
            pass

        async def get_status(self):
            raise GatewayApiError(401)

    monkeypatch.setattr(config_flow, "GatewayApi", UnauthorizedApi)
    flow = TestableConfigFlow()
    flow.entry = ConfigEntry(
        domain="better_lectio",
        discovery_keys={},
        entry_id="stable-entry-id",
        minor_version=1,
        options={"refresh_interval": 300},
        source=SOURCE_USER,
        subentries_data=[],
        title="Better Lectio",
        unique_id="http://gateway.local",
        version=1,
        data={"url": "http://gateway.local", "api_token": "known-good-token"},
    )

    result = asyncio.run(
        flow.async_step_reconfigure(
            {"url": "http://gateway.local", "api_token": "rejected-token"}
        )
    )

    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"
    assert result["errors"]["base"] == "invalid_auth"
    assert flow.entry.data["api_token"] == "known-good-token"


def test_reconfigure_form_requires_new_write_only_token():
    flow = TestableConfigFlow()
    flow.entry = ConfigEntry(
        domain="better_lectio",
        discovery_keys={},
        entry_id="stable-entry-id",
        minor_version=1,
        options={},
        source=SOURCE_USER,
        subentries_data=[],
        title="Better Lectio",
        unique_id="http://gateway.local",
        version=1,
        data={"url": "http://gateway.local", "api_token": "never-prefill"},
    )

    result = asyncio.run(flow.async_step_reconfigure())

    fields = {field.schema for field in result["data_schema"].schema}
    assert fields == {"url", "api_token"}
    token_field = next(
        selector
        for field, selector in result["data_schema"].schema.items()
        if field.schema == "api_token"
    )
    assert token_field.config["type"] == "password"
    assert "never-prefill" not in repr(result)
