import importlib
import json
from importlib.util import find_spec

import pytest


def _setup_config_module():
    module_name = "display_service.setup_config"
    if find_spec(module_name) is None:
        pytest.fail("Managed display-service setup configuration is not implemented")
    return importlib.import_module(module_name)


def _managed_payload(**overrides):
    payload = {
        "version": 1,
        "ha_url": "http://managed-ha.local:8123",
        "ha_token": "managed-secret-token",
        "entities": {
            "lectio_calendar": "calendar.school",
            "private_calendars": ["calendar.family", "calendar.work"],
            "assignments_todo": "todo.assignments",
            "homework_todo": "todo.homework",
            "cancellations_sensor": "sensor.cancellations",
        },
    }
    payload.update(overrides)
    return payload


def test_managed_config_overrides_environment_and_maps_entity_roles(tmp_path):
    module = _setup_config_module()
    store = module.HomeAssistantDisplaySettingsStore(tmp_path)
    store.config_path.write_text(json.dumps(_managed_payload()), encoding="utf-8")

    settings = store.load(
        {
            "HOME_ASSISTANT_URL": "http://legacy-ha.local:8123",
            "HOME_ASSISTANT_TOKEN": "legacy-token",
            "HA_LECTIO_CALENDAR": "calendar.legacy",
        }
    )

    assert settings.ha_url == "http://managed-ha.local:8123"
    assert settings.ha_token == "managed-secret-token"
    assert "managed-secret-token" not in repr(settings)
    assert settings.entity_config.lectio_calendar_entity_id == "calendar.school"
    assert settings.entity_config.private_calendar_entity_ids == (
        "calendar.family",
        "calendar.work",
    )
    assert settings.entity_config.assignments_entity_id == "todo.assignments"


def test_legacy_environment_remains_a_fallback_until_managed_file_exists(tmp_path):
    module = _setup_config_module()
    store = module.HomeAssistantDisplaySettingsStore(tmp_path)

    settings = store.load(
        {
            "HOME_ASSISTANT_URL": "http://legacy-ha.local:8123",
            "HOME_ASSISTANT_TOKEN": "legacy-token",
            "HA_PRIVATE_CALENDARS": "",
        }
    )

    assert settings.ha_url == "http://legacy-ha.local:8123"
    assert settings.ha_token == "legacy-token"
    assert settings.entity_config.private_calendar_entity_ids == ()


def test_empty_environment_and_missing_managed_file_is_unconfigured(tmp_path):
    module = _setup_config_module()
    store = module.HomeAssistantDisplaySettingsStore(tmp_path)

    assert store.load({}) is None


def test_corrupt_managed_file_fails_closed_instead_of_using_environment(tmp_path):
    module = _setup_config_module()
    store = module.HomeAssistantDisplaySettingsStore(tmp_path)
    store.config_path.parent.mkdir(parents=True, exist_ok=True)
    store.config_path.write_text("{bad json", encoding="utf-8")

    with pytest.raises(ValueError):
        store.load(
            {
                "HOME_ASSISTANT_URL": "http://legacy-ha.local:8123",
                "HOME_ASSISTANT_TOKEN": "legacy-token",
            }
        )


def test_disconnect_marker_keeps_legacy_environment_disabled(tmp_path):
    module = _setup_config_module()
    store = module.HomeAssistantDisplaySettingsStore(tmp_path)
    store.config_path.write_text(
        json.dumps({"version": 1, "disconnected": True}), encoding="utf-8"
    )

    assert store.load(
        {
            "HOME_ASSISTANT_URL": "http://legacy-ha.local:8123",
            "HOME_ASSISTANT_TOKEN": "legacy-token",
        }
    ) is None


@pytest.mark.parametrize(
    "payload",
    [
        _managed_payload(ha_token=""),
        _managed_payload(ha_url="http://user:pass@ha.local:8123"),
        _managed_payload(entities={"lectio_calendar": "sensor.wrong"}),
        _managed_payload(version=2),
    ],
)
def test_invalid_managed_config_fails_closed(tmp_path, payload):
    module = _setup_config_module()
    store = module.HomeAssistantDisplaySettingsStore(tmp_path)
    store.config_path.parent.mkdir(parents=True, exist_ok=True)
    store.config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        store.load({"HOME_ASSISTANT_URL": "http://legacy-ha.local:8123"})


def test_invalid_legacy_environment_is_reported_as_invalid(tmp_path):
    module = _setup_config_module()
    store = module.HomeAssistantDisplaySettingsStore(tmp_path)

    with pytest.raises(ValueError):
        store.load(
            {
                "HOME_ASSISTANT_URL": "http://legacy-ha.local:8123",
                "HOME_ASSISTANT_TOKEN": "",
            }
        )
