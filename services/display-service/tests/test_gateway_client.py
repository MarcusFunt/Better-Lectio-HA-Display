import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from display_service.gateway_client import GatewayApiError, LectioGatewayClient


def test_client_requests_status_and_all_sources_with_copenhagen_window():
    seen = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def json(self, **_):
            if seen[-1][0].endswith("/status"):
                return {"auth": {"state": "AUTHENTICATED"}, "sources": {}}
            return {"items": [], "sync": {"state": "valid", "is_stale": False}}

    class Session:
        def get(self, url, **kwargs):
            seen.append((url, kwargs))
            return Response()

    async def run():
        client = LectioGatewayClient("http://lectio-gateway:8000")
        client._session = Session()
        assert (await client.async_get_status())["auth"]["state"] == "AUTHENTICATED"
        start = datetime(2026, 3, 28, tzinfo=ZoneInfo("Europe/Copenhagen"))
        end = datetime(2026, 3, 31, tzinfo=ZoneInfo("Europe/Copenhagen"))
        for source in ("schedule", "assignments", "homework", "cancellations"):
            assert (await client.async_get_source(source, start, end))["items"] == []

    asyncio.run(run())
    assert [entry[0].rsplit("/", 1)[-1] for entry in seen] == [
        "status", "schedule", "assignments", "homework", "cancellations"
    ]
    for _, options in seen[1:]:
        assert options["params"] == {
            "start": "2026-03-28T00:00:00+01:00",
            "end": "2026-03-31T00:00:00+02:00",
        }
        assert "Authorization" not in options.get("headers", {})


@pytest.mark.parametrize("payload", [[], {}, {"items": [{}], "sync": {"state": "valid"}}, {"items": [], "sync": {"state": "bogus"}}])
def test_client_rejects_malformed_source_response(payload):
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def json(self, **_):
            return payload

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    async def run():
        client = LectioGatewayClient("http://lectio-gateway:8000")
        client._session = Session()
        start = datetime(2026, 9, 25, tzinfo=ZoneInfo("Europe/Copenhagen"))
        with pytest.raises(GatewayApiError, match="invalid_response"):
            await client.async_get_source("schedule", start, start.replace(day=26))

    asyncio.run(run())


def test_client_classifies_http_and_connection_failures():
    class Response:
        status = 502

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class HttpSession:
        def get(self, *_args, **_kwargs):
            return Response()

    class FailedSession:
        def get(self, *_args, **_kwargs):
            raise aiohttp.ClientConnectionError("secret endpoint")

    async def run():
        client = LectioGatewayClient("http://lectio-gateway:8000")
        for session, expected in ((HttpSession(), "gateway_error"), (FailedSession(), "connection_failed")):
            client._session = session
            with pytest.raises(GatewayApiError) as failure:
                await client.async_get_status()
            assert failure.value.code == expected
            assert "secret endpoint" not in str(failure.value)

    asyncio.run(run())


def test_client_classifies_invalid_json_as_invalid_response():
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def json(self, **_):
            raise ValueError("private response body")

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    async def run():
        client = LectioGatewayClient("http://lectio-gateway:8000")
        client._session = Session()
        with pytest.raises(GatewayApiError) as failure:
            await client.async_get_status()
        assert failure.value.code == "invalid_response"
        assert "private response body" not in str(failure.value)

    asyncio.run(run())
