import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from custom_components.better_lectio import calendar as calendar_platform
from custom_components.better_lectio import sensor as sensor_platform
from custom_components.better_lectio import todo as todo_platform
from custom_components.better_lectio.const import DOMAIN
from custom_components.better_lectio.coordinator import (
    LectioData,
    LectioDataUpdateCoordinator,
)
from custom_components.better_lectio.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.better_lectio.sensor import (
    LectioCancellationsSensor,
    LectioSessionStatusSensor,
)
from custom_components.better_lectio.todo import (
    LectioAssignmentsTodo,
    LectioHomeworkTodo,
)
from homeassistant.components.todo import TodoItemStatus
from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.core import HomeAssistant


class FakeCoordinator:
    def __init__(self):
        self.last_update_success = True
        self.gateway_reachable = True
        self.data = LectioData(
            items={
                "schedule": [],
                "assignments": [
                    {
                        "id": "a-1",
                        "title": "Aflevering",
                        "status": "open",
                    }
                ],
                "homework": [
                    {
                        "id": "h-1",
                        "subject": "Dansk",
                        "description": "Læs teksten",
                        "target_lesson_start": "2026-09-29T08:00:00+02:00",
                    }
                ],
                "cancellations": [
                    {
                        "id": "c-1",
                        "start": "2026-09-29T08:00:00+02:00",
                        "end": "2026-09-29T09:00:00+02:00",
                        "subject": "Dansk",
                        "teacher": "AB",
                        "reason": "Cancelled",
                    }
                ],
            },
            sources={
                "schedule": {"state": "valid", "is_stale": False},
                "assignments": {"state": "valid", "is_stale": False},
                "homework": {"state": "valid", "is_stale": False},
                "cancellations": {"state": "valid", "is_stale": False},
            },
            auth={
                "state": "AUTHENTICATED",
                "student_id_available": True,
                "last_verified_at": "2026-09-25T08:00:00+02:00",
            },
            gateway_reachable=True,
            range_start=datetime(2026, 9, 24, tzinfo=timezone.utc),
            range_end=datetime(2026, 10, 26, tzinfo=timezone.utc),
        )

    def async_add_listener(self, callback):
        return lambda: None


def test_todo_entities_are_created_and_read_only():
    entry = SimpleNamespace(entry_id="entry-1")
    coordinator = FakeCoordinator()

    assignments = LectioAssignmentsTodo(entry, coordinator)
    homework = LectioHomeworkTodo(entry, coordinator)

    assert assignments.unique_id == "entry-1_assignments"
    assert assignments.supported_features == 0
    assert assignments.todo_items[0].status is TodoItemStatus.NEEDS_ACTION
    assert homework.unique_id == "entry-1_homework"
    assert homework.supported_features == 0
    assert homework.todo_items[0].due == datetime(
        2026, 9, 29, 6, tzinfo=timezone.utc
    )


def test_cancellation_and_session_sensors_expose_safe_structured_status():
    entry = SimpleNamespace(entry_id="entry-1")
    coordinator = FakeCoordinator()

    cancellations = LectioCancellationsSensor(entry, coordinator)
    session = LectioSessionStatusSensor(entry, coordinator)

    assert cancellations.native_value == 1
    assert cancellations.extra_state_attributes["cancellations"][0]["teacher"] == "AB"
    assert session.native_value == "AUTHENTICATED"
    assert session.extra_state_attributes["student_id_available"] is True
    assert "student_id" not in session.extra_state_attributes
    coordinator.last_update_success = False
    coordinator.gateway_reachable = False
    assert session.available is False
    assert session.extra_state_attributes["gateway_reachable"] is False


def test_diagnostics_do_not_return_gateway_url_or_config_values():
    async def run():
        hass = HomeAssistant(".")
        coordinator = LectioDataUpdateCoordinator(
            hass,
            object(),
            ConfigEntry(
                domain=DOMAIN,
                discovery_keys={},
                entry_id="test-entry",
                minor_version=1,
                options={"refresh_interval": 300},
                source=SOURCE_USER,
                subentries_data=[],
                title="Better Lectio",
                unique_id="http://private-host:8000",
                version=1,
                data={"url": "http://user:secret@private-host:8000", "token": "secret"},
            ),
        )
        entry = coordinator.config_entry
        entry.runtime_data = coordinator
        coordinator._gateway_reachable = True
        coordinator.last_update_success = True
        coordinator.data = FakeCoordinator().data
        result = await async_get_config_entry_diagnostics(hass, entry)
        assert result["gateway_reachable"] is True
        assert "private-host" not in repr(result)
        assert "secret" not in repr(result)

    asyncio.run(run())


def test_platform_setup_creates_all_six_entities():
    async def run():
        coordinator = FakeCoordinator()
        entry = SimpleNamespace(entry_id="entry-1", runtime_data=coordinator)
        calendar_entities = []
        todo_entities = []
        sensor_entities = []

        await calendar_platform.async_setup_entry(
            None, entry, lambda entities: calendar_entities.extend(entities)
        )
        await todo_platform.async_setup_entry(
            None, entry, lambda entities: todo_entities.extend(entities)
        )
        await sensor_platform.async_setup_entry(
            None, entry, lambda entities: sensor_entities.extend(entities)
        )

        assert len(calendar_entities) == 1
        assert len(todo_entities) == 2
        assert len(sensor_entities) == 3
        assert len(
            {
                entity.unique_id
                for entity in calendar_entities + todo_entities + sensor_entities
            }
        ) == 6

    asyncio.run(run())
