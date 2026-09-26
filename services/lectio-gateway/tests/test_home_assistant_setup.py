import asyncio
import hashlib

import pytest
from httpx import ASGITransport, AsyncClient
from lectio_gateway.ha_api_auth import GatewayApiTokenStore
from lectio_gateway.main import app
from lectio_gateway.setup_config import (
    HomeAssistantDisplaySettings,
    HomeAssistantSetupStore,
)

HA_TOKEN = "home-assistant-long-lived-secret"


def _setup_state(tmp_path):
    setup_store = HomeAssistantSetupStore(
        tmp_path / "shared-config", tmp_path / "gateway-data"
    )
    token_store = GatewayApiTokenStore(tmp_path / "api-auth")
    app.state.home_assistant_setup_store = setup_store
    app.state.home_assistant_api_token_store = token_store
    app.state.display_diagnostics_client = FakeDisplayDiagnosticsClient()
    return setup_store, token_store


async def _request(
    method, path, *, json_body=None, origin="http://test", base_url="http://test"
):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=base_url
    ) as client:
        return await client.request(
            method,
            path,
            json=json_body,
            headers={"origin": origin} if origin else {},
        )


def test_setup_status_redacts_both_tokens_and_reports_saved_settings(tmp_path):
    setup_store, token_store = _setup_state(tmp_path)
    setup_store.save_display_settings(
        HomeAssistantDisplaySettings(
            ha_url="http://homeassistant.local:8123", ha_token=HA_TOKEN
        )
    )
    setup_store.save_gateway_api_url("http://192.168.1.20:8002")
    api_token = token_store.create_or_rotate()

    response = asyncio.run(_request("GET", "/auth/home-assistant/setup"))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["display_configured"] is True
    assert payload["ha_url"] == "http://homeassistant.local:8123"
    assert payload["gateway_api_url"] == "http://192.168.1.20:8002"
    assert payload["api_token_configured"] is True
    assert payload["api_bind_scope"] in {"loopback", "wildcard", "host"}
    assert HA_TOKEN not in response.text
    assert api_token not in response.text


def test_display_settings_route_preserves_blank_token_and_never_returns_it(tmp_path):
    setup_store, _ = _setup_state(tmp_path)
    setup_store.save_display_settings(
        HomeAssistantDisplaySettings(
            ha_url="http://homeassistant.local:8123", ha_token=HA_TOKEN
        )
    )

    response = asyncio.run(
        _request(
            "POST",
            "/auth/home-assistant/display-settings",
            json_body={
                "ha_url": "http://new-ha.local:8123",
                "ha_token": "",
                "entities": {
                    "lectio_calendar": "calendar.school",
                    "private_calendars": [],
                    "assignments_todo": "todo.assignments",
                    "homework_todo": "todo.homework",
                    "cancellations_sensor": "sensor.cancellations",
                },
            },
        )
    )

    assert response.status_code == 200
    assert HA_TOKEN not in response.text
    loaded = setup_store.load_display_settings()
    assert loaded.ha_url == "http://new-ha.local:8123"
    assert loaded.ha_token.get_secret_value() == HA_TOKEN
    assert loaded.entity_roles.private_calendars == ()


def test_invalid_display_settings_returns_safe_validation_error(tmp_path):
    setup_store, _ = _setup_state(tmp_path)

    response = asyncio.run(
        _request(
            "POST",
            "/auth/home-assistant/display-settings",
            json_body={"ha_url": "http://user:private@ha.local:8123", "ha_token": HA_TOKEN},
        )
    )

    assert response.status_code == 422
    assert HA_TOKEN not in response.text
    assert setup_store.load_display_settings() is None


def test_api_token_route_reveals_only_new_token_and_persists_only_digest(tmp_path):
    _, token_store = _setup_state(tmp_path)

    response = asyncio.run(
        _request("POST", "/auth/home-assistant/gateway-api-token")
    )

    token = response.json()["token"]
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["show_once"] is True
    assert token_store.digest_path.read_text(encoding="ascii").strip() == hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()
    assert token not in token_store.digest_path.read_text(encoding="ascii")


def test_disconnect_clears_managed_display_secret(tmp_path):
    setup_store, _ = _setup_state(tmp_path)
    setup_store.save_display_settings(
        HomeAssistantDisplaySettings(
            ha_url="http://homeassistant.local:8123", ha_token=HA_TOKEN
        )
    )

    response = asyncio.run(
        _request("POST", "/auth/home-assistant/disconnect", json_body={})
    )

    assert response.status_code == 200
    assert setup_store.load_display_settings() is None
    assert HA_TOKEN not in response.text


def test_gateway_api_url_route_saves_only_a_valid_reachable_url(tmp_path):
    setup_store, _ = _setup_state(tmp_path)

    response = asyncio.run(
        _request(
            "POST",
            "/auth/home-assistant/gateway-api-url",
            json_body={"url": "http://192.168.1.20:8002/"},
        )
    )

    assert response.status_code == 200
    assert response.json() == {"saved": True, "url": "http://192.168.1.20:8002"}
    assert setup_store.load_gateway_api_url() == "http://192.168.1.20:8002"


def test_setup_mutations_reject_cross_origin_requests(tmp_path):
    setup_store, token_store = _setup_state(tmp_path)
    paths_and_bodies = [
        ("/auth/home-assistant/display-settings", {"ha_url": "http://ha.local", "ha_token": "x"}),
        ("/auth/home-assistant/gateway-api-url", {"url": "http://192.168.1.20:8002"}),
        ("/auth/home-assistant/gateway-api-token", {}),
        ("/auth/home-assistant/disconnect", {}),
    ]

    async def run():
        return [
            await _request(
                "POST", path, json_body=body, origin="http://attacker.invalid"
            )
            for path, body in paths_and_bodies
        ]

    responses = asyncio.run(run())

    assert [response.status_code for response in responses] == [403] * 4
    assert setup_store.load_display_settings() is None
    assert not token_store.managed_digest_exists


def test_setup_mutations_reject_same_host_with_different_scheme(tmp_path):
    _, token_store = _setup_state(tmp_path)

    response = asyncio.run(
        _request(
            "POST",
            "/auth/home-assistant/gateway-api-token",
            json_body={},
            origin="https://test",
            base_url="http://test",
        )
    )

    assert response.status_code == 403
    assert not token_store.managed_digest_exists


def test_setup_mutations_accept_normalized_default_port_and_case(tmp_path):
    _, token_store = _setup_state(tmp_path)

    response = asyncio.run(
        _request(
            "POST",
            "/auth/home-assistant/gateway-api-token",
            json_body={},
            origin="HTTP://TEST:80/",
        )
    )

    assert response.status_code == 200
    assert token_store.managed_digest_exists


@pytest.mark.parametrize(
    "origin",
    ["null", "http://user@test", "http://test/path", "http://test?query=1"],
)
def test_setup_mutations_reject_non_origin_values(tmp_path, origin):
    _, token_store = _setup_state(tmp_path)

    response = asyncio.run(
        _request(
            "POST",
            "/auth/home-assistant/gateway-api-token",
            json_body={},
            origin=origin,
        )
    )

    assert response.status_code == 403
    assert not token_store.managed_digest_exists


def test_login_page_contains_full_home_assistant_setup_wizard(tmp_path):
    _setup_state(tmp_path)
    app.state.auth_manager = FakeAuthManager()

    response = asyncio.run(_request("GET", "/auth/browser"))

    assert response.status_code == 200
    for marker in (
        'id="ha-setup-form"',
        'id="ha-token"',
        'id="gateway-api-url"',
        'id="gateway-api-url-status"',
        'id="gateway-api-token"',
        'id="hacs-setup-instructions"',
        'id="api-binding-warning"',
        "configureHomeAssistant",
        "long-lived access token from your Home Assistant user profile",
        "custom repository",
        "Rotation invalidates the previous token immediately",
        "choose <strong>Reconfigure</strong>",
    ):
        assert marker in response.text


class FakeDisplayDiagnosticsClient:
    async def setup(self):
        return {"state": "connected"}


class FakeAuthManager:
    browser_view_url = "http://localhost:6080/vnc.html"
