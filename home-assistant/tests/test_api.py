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

    def get(self, url, *, params=None, timeout=None, headers=None):
        self.calls.append((url, params, timeout, headers))
        return self.response


def make_api(response, token=None):
    api = object.__new__(GatewayApi)
    api._base_url = "http://gateway.local"
    api._timeout = aiohttp.ClientTimeout(total=15)
    api._session = FakeSession(response)
    api._api_token = token
    return api


def test_source_request_sends_timezone_aware_date_range():
    async def run():
        token = "x" * 43
        api = make_api(
            FakeResponse(200, {"items": [], "sync": {"state": "valid"}}), token
        )
        start = datetime(2026, 9, 25, 8, tzinfo=timezone.utc)
        end = datetime(2026, 9, 26, 8, tzinfo=timezone.utc)

        await api.get_source("schedule", start, end)

        url, params, timeout, headers = api._session.calls[0]
        assert url == "http://gateway.local/api/v1/schedule"
        assert params == {"start": start.isoformat(), "end": end.isoformat()}
        assert timeout.total == 15
        assert headers == {"Authorization": f"Bearer {token}"}

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


def test_legacy_config_without_api_token_sends_no_authorization_header():
    async def run():
        api = make_api(FakeResponse(200, {"auth": {}, "sources": {}}))
        await api.get_status()
        return api._session.calls[0][3]

    assert asyncio.run(run()) is None
