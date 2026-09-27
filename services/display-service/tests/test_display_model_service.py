import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from display_service.gateway_client import GatewayApiError
from display_service.model_service import DisplayModelService

TZ = ZoneInfo("Europe/Copenhagen")


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.failed = set()
        self.data = {
            "schedule": [{"id": "lesson-1", "subject": "Math", "teacher": "AB", "room": "2.27", "start": "2026-09-25T09:00:00+02:00", "end": "2026-09-25T09:45:00+02:00"}],
            "assignments": [{"id": "assignment-1", "title": "Essay", "subject": "English", "due": "2026-09-26T12:00:00+02:00", "status": "open"}],
            "homework": [{"id": "homework-1", "subject": "Math", "description": "Read chapter", "target_lesson_start": "2026-09-25T11:00:00+02:00"}],
            "cancellations": [{"id": "cancel-1", "subject": "History", "start": "2026-09-25T12:00:00+02:00", "end": "2026-09-25T12:45:00+02:00", "original_lesson": {"id": "old"}}],
        }
        self.sync = {source: {"state": "valid", "is_stale": False, "last_successful_sync": "2026-09-25T06:00:00+02:00"} for source in self.data}

    async def async_get_status(self):
        self.calls.append(("status",))
        return {"auth": {"state": "AUTHENTICATED"}, "sources": {}}

    async def async_get_source(self, source, start, end):
        self.calls.append((source, start, end))
        if source in self.failed:
            raise GatewayApiError("connection_failed")
        return {"items": self.data[source], "sync": self.sync[source]}


def test_model_service_maps_lectio_and_requests_three_local_days():
    async def run():
        gateway = FakeGateway()
        result = await DisplayModelService(gateway).async_build(now=datetime(2026, 9, 25, 7, tzinfo=TZ))
        assert [entry[0] for entry in gateway.calls] == ["status", "schedule", "assignments", "homework", "cancellations"]
        assert all(entry[1:] == (datetime(2026, 9, 25, tzinfo=TZ), datetime(2026, 9, 28, tzinfo=TZ)) for entry in gateway.calls[1:])
        assert [day.date.isoformat() for day in result.model.days] == ["2026-09-25", "2026-09-26", "2026-09-27"]
        assert [(event.id, event.title, event.teacher, event.room) for event in result.model.days[0].events] == [("lectio:lesson-1", "Math", "AB", "2.27")]
        assert [item.kind for item in result.model.sidebar] == ["cancellation", "assignment", "homework"]
        assert result.model.sidebar[-1].when.isoformat() == "2026-09-25T11:00:00+02:00"
        assert result.sources["schedule"].last_successful_sync == "2026-09-25T06:00:00+02:00"

    asyncio.run(run())


def test_source_failure_uses_only_its_last_good_data_and_preserves_sync():
    async def run():
        gateway = FakeGateway()
        service = DisplayModelService(gateway)
        now = datetime(2026, 9, 25, 7, tzinfo=TZ)
        await service.async_build(now=now)
        gateway.failed.add("assignments")
        gateway.data["schedule"] = [{**gateway.data["schedule"][0], "subject": "Physics"}]
        result = await service.async_build(now=now)
        assert result.model.days[0].events[0].title == "Physics"
        assert [item.title for item in result.model.sidebar if item.kind == "assignment"] == ["Essay"]
        assert result.sources["assignments"].state == "stale"
        assert result.sources["assignments"].last_successful_sync == "2026-09-25T06:00:00+02:00"
        gateway.sync["schedule"] = {"state": "expired", "is_stale": True, "last_successful_sync": "2026-09-24T06:00:00+02:00"}
        gateway.data["schedule"] = []  # Partial week response must not replace a complete in-memory view.
        result = await service.async_build(now=now)
        assert result.sources["schedule"].state == "expired"
        assert result.sources["schedule"].is_stale is True
        assert result.model.days[0].events[0].title == "Physics"

    asyncio.run(run())


def test_first_schedule_failure_is_error_without_empty_calendar_render():
    async def run():
        gateway = FakeGateway()
        gateway.failed.add("schedule")
        result = await DisplayModelService(gateway).async_build(now=datetime(2026, 9, 25, 7, tzinfo=TZ))
        assert result.sources["schedule"].state == "error"
        assert result.model.days[0].events == ()
        assert [item.kind for item in result.model.sidebar] == ["cancellation", "assignment", "homework"]

    asyncio.run(run())
