import pytest
from custom_components.better_lectio.config_flow import (
    LectioConfigFlow,
    LectioOptionsFlow,
    normalize_gateway_url,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntry


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
