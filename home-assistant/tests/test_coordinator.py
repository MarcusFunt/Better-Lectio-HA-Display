import asyncio
from datetime import datetime, timedelta, timezone

from custom_components.better_lectio.api import GatewayApiError
from custom_components.better_lectio.const import DOMAIN
from custom_components.better_lectio.coordinator import (
    LectioDataUpdateCoordinator,
    _safe_auth_state,
    _safe_sync,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed


def make_entry():
    return ConfigEntry(
        domain=DOMAIN,
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


def test_malformed_sync_and_auth_state_values_are_safely_normalized():
    assert _safe_sync({"state": ["valid"]}) == {
        "state": "unknown",
        "is_stale": False,
        "last_successful_sync": None,
    }
    assert _safe_auth_state({"unexpected": "shape"}) == "UNKNOWN"


class FakeGatewayApi:
    def __init__(self):
        self.failures = {}
        self.calls = []
        self.schedule_item_id = "schedule-1"

    async def get_status(self):
        if "status" in self.failures:
            raise self.failures["status"]
        return {
            "auth": {
                "state": "AUTHENTICATED",
                "student_id_available": True,
                "last_verified_at": "2026-09-25T08:00:00+02:00",
            },
            "sources": {},
        }

    async def get_source(self, source, start, end):
        self.calls.append((source, start, end))
        if source in self.failures:
            raise self.failures[source]
        return {
            "items": [
                {
                    "id": (
                        self.schedule_item_id
                        if source == "schedule"
                        else f"{source}-1"
                    ),
                    "start": (start + timedelta(hours=1)).isoformat(),
                    "end": (start + timedelta(hours=2)).isoformat(),
                }
            ],
            "sync": {
                "state": "valid",
                "is_stale": False,
                "last_successful_sync": "2026-09-25T08:00:00+02:00",
            },
        }


def test_source_failure_keeps_last_good_data_and_marks_session_expired():
    async def run():
        hass = HomeAssistant(".")
        api = FakeGatewayApi()
        coordinator = LectioDataUpdateCoordinator(hass, api, make_entry())
        first = await coordinator._async_update_data()
        coordinator.data = first
        api.failures["assignments"] = GatewayApiError(401)

        second = await coordinator._async_update_data()

        assert second.items["assignments"] == first.items["assignments"]
        assert second.sources["assignments"]["state"] == "expired"
        assert second.sources["assignments"]["is_stale"] is True
        assert second.auth["state"] == "SESSION_EXPIRED"
        assert [item["id"] for item in second.items["schedule"]] == [
            item["id"] for item in first.items["schedule"]
        ]

    asyncio.run(run())


def test_gateway_status_failure_marks_refresh_as_failed():
    async def run():
        hass = HomeAssistant(".")
        api = FakeGatewayApi()
        coordinator = LectioDataUpdateCoordinator(hass, api, make_entry())
        coordinator.data = await coordinator._async_update_data()
        coordinator.last_update_success = True
        api.failures["status"] = GatewayApiError()

        try:
            await coordinator._async_update_data()
        except UpdateFailed as err:
            assert "Could not reach" in str(err)
        else:
            raise AssertionError("gateway failure did not fail the refresh")
        assert coordinator.gateway_reachable is False
        assert coordinator.data.gateway_reachable is False
        assert coordinator.data.sources["schedule"]["state"] == "stale"

    asyncio.run(run())


def test_uncached_calendar_range_failure_is_not_replaced_with_partial_data():
    async def run():
        hass = HomeAssistant(".")
        api = FakeGatewayApi()
        coordinator = LectioDataUpdateCoordinator(hass, api, make_entry())
        coordinator.data = await coordinator._async_update_data()
        api.failures["schedule"] = GatewayApiError(502)
        start = datetime(2027, 2, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=2)

        try:
            await coordinator.async_get_schedule(start, end)
        except UpdateFailed as err:
            assert "requested calendar range" in str(err)
        else:
            raise AssertionError("failed date-range request was silently substituted")

    asyncio.run(run())


def test_calendar_range_outside_poll_window_uses_exact_gateway_query():
    async def run():
        hass = HomeAssistant(".")
        api = FakeGatewayApi()
        coordinator = LectioDataUpdateCoordinator(hass, api, make_entry())
        coordinator.data = await coordinator._async_update_data()
        api.calls.clear()
        start = datetime(2027, 2, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=2)

        items = await coordinator.async_get_schedule(start, end)

        assert [item["id"] for item in items] == ["schedule-1"]
        assert len(api.calls) == 1
        assert api.calls[0] == ("schedule", start, end)

        api.schedule_item_id = "schedule-2"
        refreshed_items = await coordinator.async_get_schedule(start, end)
        assert [item["id"] for item in refreshed_items] == ["schedule-2"]

        api.failures["schedule"] = GatewayApiError(502)
        stale_items = await coordinator.async_get_schedule(start, end)
        assert [item["id"] for item in stale_items] == ["schedule-2"]
        assert len(api.calls) == 3
        assert coordinator.data.sources["schedule"]["state"] == "stale"

    asyncio.run(run())
