import asyncio
from datetime import datetime, timezone

import aiohttp
import pytest
from custom_components.better_lectio.api import GatewayApi, GatewayApiError


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, *, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        return self.response


def make_api(response):
    api = object.__new__(GatewayApi)
    api._base_url = "http://gateway.local"
    api._timeout = aiohttp.ClientTimeout(total=15)
    api._session = FakeSession(response)
    return api


def test_source_request_sends_timezone_aware_date_range():
    async def run():
        api = make_api(FakeResponse(200, {"items": [], "sync": {"state": "valid"}}))
        start = datetime(2026, 9, 25, 8, tzinfo=timezone.utc)
        end = datetime(2026, 9, 26, 8, tzinfo=timezone.utc)

        await api.get_source("schedule", start, end)

        url, params, timeout = api._session.calls[0]
        assert url == "http://gateway.local/api/v1/schedule"
        assert params == {"start": start.isoformat(), "end": end.isoformat()}
        assert timeout.total == 15

    asyncio.run(run())


def test_http_errors_discard_response_body():
    async def run():
        api = make_api(FakeResponse(401, {"detail": "private cookie and student data"}))

        with pytest.raises(GatewayApiError) as error:
            await api.get_status()

        assert error.value.code == "authentication_required"
        assert "private" not in str(error.value)
        assert "student" not in str(error.value)

    asyncio.run(run())
