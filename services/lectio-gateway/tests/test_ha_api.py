import asyncio
import importlib

import httpx
from httpx import ASGITransport, AsyncClient, MockTransport

try:
    app = importlib.import_module("lectio_gateway.ha_api").app
except (ImportError, AttributeError):
    app = None


API_TOKEN = "t" * 43


def _require_app():
    assert app is not None, "The scoped Home Assistant API is not implemented"
    return app


async def _request(
    method,
    path,
    *,
    token=API_TOKEN,
    configured_token=API_TOKEN,
    token_store=None,
    params=None,
    upstream=None,
):
    app = _require_app()
    app.state.api_token = configured_token or ""
    app.state.api_token_store = token_store
    gateway_client = AsyncClient(
        base_url="http://gateway:8000",
        transport=MockTransport(
            upstream or (lambda request: httpx.Response(200, json={"ok": True}))
        ),
    )
    app.state.gateway_client = gateway_client
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://ha-api"
        ) as client:
            return await client.request(method, path, headers=headers, params=params)
    finally:
        await gateway_client.aclose()


def test_authenticated_status_request_is_forwarded_to_gateway():
    async def run():
        requests = []

        def upstream(request):
            requests.append(request)
            return httpx.Response(200, json={"auth": {"state": "AUTHENTICATED"}})

        response = await _request("GET", "/api/v1/status", upstream=upstream)
        return response, requests

    response, requests = asyncio.run(run())

    assert response.status_code == 200
    assert response.json()["auth"]["state"] == "AUTHENTICATED"
    assert requests[0].url == "http://gateway:8000/api/v1/status"
    assert "authorization" not in requests[0].headers
    assert API_TOKEN not in response.text


def test_source_request_forwards_only_its_date_range():
    async def run():
        requests = []

        def upstream(request):
            requests.append(request)
            return httpx.Response(200, json={"items": [], "sync": {"state": "valid"}})

        response = await _request(
            "GET",
            "/api/v1/schedule",
            params={
                "start": "2026-09-26T00:00:00+02:00",
                "end": "2026-09-27T00:00:00+02:00",
            },
            upstream=upstream,
        )
        return response, requests

    response, requests = asyncio.run(run())

    assert response.status_code == 200
    assert requests[0].url.path == "/api/v1/schedule"
    assert dict(requests[0].url.params) == {
        "start": "2026-09-26T00:00:00+02:00",
        "end": "2026-09-27T00:00:00+02:00",
    }


def test_missing_or_incorrect_token_is_rejected_without_forwarding():
    async def run():
        calls = []

        def upstream(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": True})

        missing = await _request(
            "GET", "/api/v1/status", token=None, upstream=upstream
        )
        incorrect = await _request(
            "GET", "/api/v1/status", token="wrong", upstream=upstream
        )
        return missing, incorrect, calls

    missing, incorrect, calls = asyncio.run(run())

    assert missing.status_code == 401
    assert incorrect.status_code == 401
    assert calls == []


def test_unconfigured_api_credential_disables_forwarding():
    async def run():
        calls = []

        def upstream(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": True})

        response = await _request(
            "GET",
            "/api/v1/status",
            configured_token="",
            upstream=upstream,
        )
        return response, calls

    response, calls = asyncio.run(run())

    assert response.status_code == 503
    assert calls == []


def test_managed_api_token_rotation_takes_effect_immediately(tmp_path):
    from lectio_gateway.ha_api_auth import GatewayApiTokenStore

    async def run():
        store = GatewayApiTokenStore(tmp_path / "api-auth")
        first = store.create_or_rotate()
        old_token = await _request(
            "GET", "/api/v1/status", token=first, token_store=store,
            configured_token="legacy-environment-token",
        )
        second = store.create_or_rotate()
        rejected = await _request(
            "GET", "/api/v1/status", token=first, token_store=store,
            configured_token="legacy-environment-token",
        )
        accepted = await _request(
            "GET", "/api/v1/status", token=second, token_store=store,
            configured_token="legacy-environment-token",
        )
        return old_token, rejected, accepted

    old_token, rejected, accepted = asyncio.run(run())

    assert old_token.status_code == 200
    assert rejected.status_code == 401
    assert accepted.status_code == 200


def test_corrupt_managed_digest_does_not_fall_back_to_environment_token(tmp_path):
    from lectio_gateway.ha_api_auth import GatewayApiTokenStore

    async def run():
        store = GatewayApiTokenStore(tmp_path / "api-auth")
        store.digest_path.parent.mkdir(parents=True)
        store.digest_path.write_text("corrupt", encoding="ascii")
        return await _request(
            "GET", "/api/v1/status", token="legacy-environment-token",
            configured_token="legacy-environment-token", token_store=store,
        )

    response = asyncio.run(run())

    assert response.status_code == 401


def test_admin_routes_and_non_get_api_methods_are_not_exposed():
    async def run():
        admin = await _request("GET", "/auth/browser")
        mutation = await _request("POST", "/api/v1/status")
        unknown = await _request("GET", "/api/v1/anything")
        return admin, mutation, unknown

    admin, mutation, unknown = asyncio.run(run())

    assert admin.status_code == 404
    assert mutation.status_code == 405
    assert unknown.status_code == 404


def test_gateway_failures_are_sanitized():
    async def run():
        def upstream(_request):
            raise httpx.ConnectError("private gateway address and credential")

        return await _request("GET", "/api/v1/status", upstream=upstream)

    response = asyncio.run(run())

    assert response.status_code == 502
    assert "credential" not in response.text
    assert "private gateway" not in response.text
