import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from display_service.entity_config import HomeAssistantEntityConfig
from display_service.model_service import DisplayModelService

TZ = ZoneInfo("Europe/Copenhagen")


class FakeHomeAssistantClient:
    def __init__(self):
        self.fail = set()
        self.calls = []
        self.calendar_events = {
            "calendar.lectio": [
                {
                    "summary": "Math",
                    "start": "2026-09-25T09:00:00+02:00",
                    "end": "2026-09-25T09:45:00+02:00",
                }
            ],
            "calendar.private": [
                {
                    "summary": "Dentist",
                    "start": "2026-09-25T08:00:00+02:00",
                    "end": "2026-09-25T08:30:00+02:00",
                }
            ],
            "calendar.family": [
                {
                    "summary": "Dentist",
                    "start": "2026-09-25T08:00:00+02:00",
                    "end": "2026-09-25T08:30:00+02:00",
                },
                {
                    "summary": "Dinner",
                    "start": "2026-09-26T18:00:00+02:00",
                    "end": "2026-09-26T19:00:00+02:00",
                }
            ],
        }
        self.todo_items = {
            "todo.lectio_assignments": [
                {"uid": "assignment-1", "summary": "Essay", "status": "needs_action", "due": "2026-09-26"}
            ],
            "todo.lectio_homework": [
                {"uid": "homework-1", "summary": "Read", "status": "needs_action", "due": "2026-09-25T11:00:00+02:00"}
            ],
        }
        self.cancellations = [{"id": "cancel-1", "subject": "History", "start": "2026-09-25T12:00:00+02:00"}]
        self.sync_status = {
            "calendar.lectio": {"state": "valid", "is_stale": False},
            "todo.lectio_assignments": {"state": "valid", "is_stale": False},
            "todo.lectio_homework": {"state": "valid", "is_stale": False},
            "sensor.lectio_cancellations": {"state": "valid", "is_stale": False},
        }

    async def async_get_calendar_events(self, entity_id, start, end):
        self.calls.append(("calendar", entity_id, start, end))
        if entity_id in self.fail:
            raise RuntimeError("private raw failure message")
        return self.calendar_events[entity_id]

    async def async_get_todo_items(self, entity_id):
        self.calls.append(("todo", entity_id))
        if entity_id in self.fail:
            raise RuntimeError("private raw failure message")
        return self.todo_items[entity_id]

    async def async_get_cancellations(self, entity_id):
        self.calls.append(("sensor", entity_id))
        if entity_id in self.fail:
            raise RuntimeError("private raw failure message")
        return self.cancellations

    async def async_get_entity_sync(self, entity_id):
        self.calls.append(("sync", entity_id))
        if entity_id in self.fail:
            raise RuntimeError("private raw failure message")
        return self.sync_status[entity_id]


def test_model_service_fetches_configured_entities_for_copenhagen_window():
    async def run():
        client = FakeHomeAssistantClient()
        service = DisplayModelService(
            client,
            entity_config=HomeAssistantEntityConfig(
                private_calendar_entity_ids=("calendar.private", "calendar.family")
            ),
        )

        result = await service.async_build(
            now=datetime(2026, 9, 25, 7, 0, tzinfo=TZ)
        )

        assert [day.date.isoformat() for day in result.model.days] == [
            "2026-09-25",
            "2026-09-26",
            "2026-09-27",
        ]
        assert [call[1] for call in client.calls if call[0] == "calendar"] == [
            "calendar.lectio",
            "calendar.private",
            "calendar.family",
        ]
        assert result.sources["todo.lectio_assignments"].state == "valid"
        assert result.sources["calendar.family"].state == "valid"
        assert result.model.days[1].events[0].title == "Dinner"
        dentist_events = [
            event for event in result.model.days[0].events if event.title == "Dentist"
        ]
        assert len({event.id for event in dentist_events}) == 2

    asyncio.run(run())


def test_source_failure_keeps_only_that_sources_last_good_data():
    async def run():
        client = FakeHomeAssistantClient()
        service = DisplayModelService(
            client,
            entity_config=HomeAssistantEntityConfig(
                private_calendar_entity_ids=("calendar.private",)
            ),
        )
        now = datetime(2026, 9, 25, 7, 0, tzinfo=TZ)
        first = await service.async_build(now=now)
        client.calendar_events["calendar.lectio"] = [
            {
                "summary": "Changed lesson",
                "start": "2026-09-25T10:00:00+02:00",
                "end": "2026-09-25T10:45:00+02:00",
            }
        ]
        client.fail.add("todo.lectio_assignments")

        second = await service.async_build(now=now)

        assert [event.title for event in second.model.days[0].events] == [
            "Dentist",
            "Changed lesson",
        ]
        assert [item.title for item in second.model.sidebar if item.kind == "assignment"] == [
            "Essay"
        ]
        assert second.sources["todo.lectio_assignments"].state == "stale"
        assert second.sources["todo.lectio_assignments"].error == "request_failed"
        assert "private raw failure message" not in repr(second)
        assert first.model.days[0].events[1].title == "Math"

    asyncio.run(run())


def test_first_fetch_failure_isolated_and_reports_no_stale_cache():
    async def run():
        client = FakeHomeAssistantClient()
        client.fail.add("sensor.lectio_cancellations")
        service = DisplayModelService(
            client, entity_config=HomeAssistantEntityConfig(private_calendar_entity_ids=())
        )

        result = await service.async_build(
            now=datetime(2026, 9, 25, 7, 0, tzinfo=TZ)
        )

        assert result.sources["sensor.lectio_cancellations"].state == "error"
        assert result.sources["sensor.lectio_cancellations"].error == "request_failed"
        assert [event.title for event in result.model.days[0].events] == ["Math"]

    asyncio.run(run())


def test_model_service_preserves_home_assistant_lectio_sync_freshness():
    async def run():
        client = FakeHomeAssistantClient()
        client.sync_status["calendar.lectio"] = {
            "state": "expired",
            "is_stale": True,
            "last_successful_sync": "2026-09-24T14:00:00+02:00",
        }
        service = DisplayModelService(
            client, entity_config=HomeAssistantEntityConfig(private_calendar_entity_ids=())
        )

        result = await service.async_build(
            now=datetime(2026, 9, 25, 7, 0, tzinfo=TZ)
        )

        assert result.sources["calendar.lectio"].state == "expired"
        assert result.sources["calendar.lectio"].is_stale is True
        assert (
            result.sources["calendar.lectio"].last_successful_sync
            == "2026-09-24T14:00:00+02:00"
        )
        assert result.model.days[0].events[0].title == "Math"

    asyncio.run(run())


def test_model_service_uses_custom_entity_ids_and_semantic_sidebar_roles():
    async def run():
        client = FakeHomeAssistantClient()
        config = HomeAssistantEntityConfig(
            lectio_calendar_entity_id="calendar.school",
            private_calendar_entity_ids=("calendar.marcus", "calendar.family"),
            assignments_entity_id="todo.school_assignments",
            homework_entity_id="todo.school_homework",
            cancellations_entity_id="sensor.school_changes",
        )
        client.calendar_events.update(
            {
                "calendar.school": client.calendar_events["calendar.lectio"],
                "calendar.marcus": client.calendar_events["calendar.private"],
            }
        )
        client.todo_items.update(
            {
                "todo.school_assignments": client.todo_items[
                    "todo.lectio_assignments"
                ],
                "todo.school_homework": client.todo_items["todo.lectio_homework"],
            }
        )
        client.sync_status.update(
            {
                "calendar.school": client.sync_status["calendar.lectio"],
                "todo.school_assignments": client.sync_status[
                    "todo.lectio_assignments"
                ],
                "todo.school_homework": client.sync_status["todo.lectio_homework"],
                "sensor.school_changes": client.sync_status[
                    "sensor.lectio_cancellations"
                ],
            }
        )
        service = DisplayModelService(client, entity_config=config)
        result = await service.async_build(
            now=datetime(2026, 9, 25, 7, 0, tzinfo=TZ)
        )
        return client, result

    client, result = asyncio.run(run())
    requested_ids = {call[1] for call in client.calls if call[0] != "sync"}
    expected_ids = {
        "calendar.school",
        "calendar.marcus",
        "calendar.family",
        "todo.school_assignments",
        "todo.school_homework",
        "sensor.school_changes",
    }

    assert requested_ids == expected_ids
    assert set(result.sources) == expected_ids
    assert {event.source for event in result.model.days[0].events} == {
        "lectio",
        "private",
    }
    sidebar_roles = {item.source for item in result.model.sidebar}
    assert sidebar_roles == {"assignments", "homework", "cancellations"}
