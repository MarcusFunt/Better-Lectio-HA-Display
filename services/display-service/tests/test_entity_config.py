import pytest
from display_service.entity_config import HomeAssistantEntityConfig


def test_entity_config_defaults_match_the_current_ha_entity_names():
    config = HomeAssistantEntityConfig.from_env({})

    assert config.lectio_calendar_entity_id == "calendar.lectio"
    assert config.private_calendar_entity_ids == ("calendar.private",)
    assert config.assignments_entity_id == "todo.lectio_assignments"
    assert config.homework_entity_id == "todo.lectio_homework"
    assert config.cancellations_entity_id == "sensor.lectio_cancellations"


def test_entity_config_parses_custom_ids_and_multiple_private_calendars():
    config = HomeAssistantEntityConfig.from_env(
        {
            "HA_LECTIO_CALENDAR": "calendar.school",
            "HA_PRIVATE_CALENDARS": " calendar.marcus, calendar.family ",
            "HA_ASSIGNMENTS_TODO": "todo.school_assignments",
            "HA_HOMEWORK_TODO": "todo.school_homework",
            "HA_CANCELLATIONS_SENSOR": "sensor.school_changes",
        }
    )

    assert config.lectio_calendar_entity_id == "calendar.school"
    assert config.private_calendar_entity_ids == (
        "calendar.marcus",
        "calendar.family",
    )
    assert config.assignments_entity_id == "todo.school_assignments"
    assert config.homework_entity_id == "todo.school_homework"
    assert config.cancellations_entity_id == "sensor.school_changes"


def test_explicitly_blank_private_calendar_list_disables_private_calendars():
    config = HomeAssistantEntityConfig.from_env({"HA_PRIVATE_CALENDARS": "  "})

    assert config.private_calendar_entity_ids == ()


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("HA_LECTIO_CALENDAR", "sensor.school", "calendar domain"),
        ("HA_ASSIGNMENTS_TODO", "calendar.assignments", "todo domain"),
        ("HA_CANCELLATIONS_SENSOR", "sensor.bad-id", "valid entity ID"),
        (
            "HA_PRIVATE_CALENDARS",
            "calendar.private,calendar.private",
            "duplicate entity ID",
        ),
        (
            "HA_HOMEWORK_TODO",
            "todo.lectio_assignments",
            "duplicate entity ID",
        ),
        ("HA_HOMEWORK_TODO", "", "must not be empty"),
    ],
)
def test_entity_config_rejects_wrong_domains_malformed_ids_and_duplicates(
    key, value, message
):
    with pytest.raises(ValueError, match=message):
        HomeAssistantEntityConfig.from_env({key: value})
