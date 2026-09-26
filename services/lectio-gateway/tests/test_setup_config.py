import importlib
import json
import os
import stat
from importlib.util import find_spec

import pytest

HA_TOKEN = "ha-long-lived-secret-for-tests"


def test_entity_role_defaults_match_the_current_display_entities():
    setup_config = _setup_config_module()
    roles = setup_config.HomeAssistantEntityRoles()

    assert roles.lectio_calendar == "calendar.lectio"
    assert roles.private_calendars == ("calendar.private",)
    assert roles.assignments_todo == "todo.lectio_assignments"
    assert roles.homework_todo == "todo.lectio_homework"
    assert roles.cancellations_sensor == "sensor.lectio_cancellations"


def _setup_config_module():
    module_name = "lectio_gateway.setup_config"
    if find_spec(module_name) is None:
        pytest.fail("The gateway Home Assistant setup store is not implemented")
    return importlib.import_module(module_name)


def test_display_settings_round_trip_and_keep_token_out_of_repr(tmp_path):
    setup_config = _setup_config_module()
    store = setup_config.HomeAssistantSetupStore(
        config_dir=tmp_path / "shared-config",
        gateway_data_dir=tmp_path / "gateway-data",
    )
    settings = setup_config.HomeAssistantDisplaySettings(
        ha_url="http://homeassistant.local:8123",
        ha_token=HA_TOKEN,
        entity_roles=setup_config.HomeAssistantEntityRoles(
            private_calendars=("calendar.family", "calendar.work")
        ),
    )

    store.save_display_settings(settings)
    loaded = store.load_display_settings()
    payload = json.loads(store.display_config_path.read_text(encoding="utf-8"))

    assert loaded == settings
    assert loaded.ha_token.get_secret_value() == HA_TOKEN
    assert HA_TOKEN not in repr(loaded)
    assert payload["ha_token"] == HA_TOKEN
    assert loaded.entity_roles.private_calendars == (
        "calendar.family",
        "calendar.work",
    )
    if os.name == "posix":
        assert stat.S_IMODE(store.display_config_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.config_dir.stat().st_mode) == 0o700


def test_blank_token_update_preserves_the_saved_home_assistant_token(tmp_path):
    setup_config = _setup_config_module()
    store = setup_config.HomeAssistantSetupStore(
        config_dir=tmp_path / "shared-config",
        gateway_data_dir=tmp_path / "gateway-data",
    )
    original = setup_config.HomeAssistantDisplaySettings(
        ha_url="http://homeassistant.local:8123", ha_token=HA_TOKEN
    )
    store.save_display_settings(original)

    updated = store.update_display_settings(
        ha_url="https://ha.example.test",
        ha_token="",
        entity_roles=original.entity_roles,
    )

    assert updated.ha_url == "https://ha.example.test"
    assert updated.ha_token.get_secret_value() == HA_TOKEN


@pytest.mark.parametrize(
    "url",
    [
        "ftp://homeassistant.local:8123",
        "http://user:password@homeassistant.local:8123",
        "http://homeassistant.local:8123/?token=secret",
        "http://homeassistant.local:8123?",
        "http://homeassistant.local:8123/#fragment",
        "http://homeassistant.local:8123#",
    ],
)
def test_display_settings_rejects_unsafe_home_assistant_urls(url):
    setup_config = _setup_config_module()
    with pytest.raises(ValueError):
        setup_config.HomeAssistantDisplaySettings(ha_url=url, ha_token=HA_TOKEN)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lectio_calendar": "sensor.school"},
        {"assignments_todo": "calendar.assignments"},
        {"private_calendars": ("calendar.private", "calendar.private")},
        {"cancellations_sensor": "sensor.bad-id"},
    ],
)
def test_entity_roles_reject_wrong_domain_malformed_and_duplicate_ids(kwargs):
    setup_config = _setup_config_module()
    with pytest.raises(ValueError):
        setup_config.HomeAssistantEntityRoles(**kwargs)


def test_gateway_api_url_persists_as_non_secret_setup_metadata(tmp_path):
    setup_config = _setup_config_module()
    store = setup_config.HomeAssistantSetupStore(
        config_dir=tmp_path / "shared-config",
        gateway_data_dir=tmp_path / "gateway-data",
    )

    store.save_gateway_api_url("http://192.168.1.20:8002")

    assert store.load_gateway_api_url() == "http://192.168.1.20:8002"


@pytest.mark.parametrize("url", ["http://0.0.0.0:8002", "http://[::]:8002"])
def test_gateway_api_url_rejects_wildcard_listener_addresses(tmp_path, url):
    setup_config = _setup_config_module()
    store = setup_config.HomeAssistantSetupStore(
        config_dir=tmp_path / "shared-config",
        gateway_data_dir=tmp_path / "gateway-data",
    )

    with pytest.raises(ValueError):
        store.save_gateway_api_url(url)


def test_corrupt_managed_display_file_is_not_treated_as_unconfigured(tmp_path):
    setup_config = _setup_config_module()
    store = setup_config.HomeAssistantSetupStore(
        config_dir=tmp_path / "shared-config",
        gateway_data_dir=tmp_path / "gateway-data",
    )
    store.config_dir.mkdir(parents=True)
    store.display_config_path.write_text("{invalid json", encoding="utf-8")

    with pytest.raises(ValueError):
        store.load_display_settings()


def test_explicit_clear_writes_disconnect_marker_instead_of_deleting_migration_state(
    tmp_path,
):
    setup_config = _setup_config_module()
    store = setup_config.HomeAssistantSetupStore(
        config_dir=tmp_path / "shared-config",
        gateway_data_dir=tmp_path / "gateway-data",
    )
    store.update_display_settings(
        ha_url="http://homeassistant.local:8123",
        ha_token=HA_TOKEN,
        entity_roles=setup_config.HomeAssistantEntityRoles(),
    )

    store.clear_display_settings()

    assert store.load_display_settings() is None
    assert json.loads(store.display_config_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "disconnected": True,
    }
