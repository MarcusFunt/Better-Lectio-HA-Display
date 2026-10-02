import asyncio

import httpx
import pytest
from lectio_gateway import provisioning_client
from lectio_gateway.provisioning_client import (
    DeviceRegistrationConflict,
    DisplayProvisioningClient,
    DisplayProvisioningUnavailable,
)


def test_display_provisioning_client_authenticates_and_returns_created_secret(
    monkeypatch, tmp_path
):
    token_path = tmp_path / "gateway-token"
    token_path.write_text("internal-token-value-which-is-long-enough", encoding="ascii")
    captured = {}

    def handle(request):
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(
            201,
            json={"device_id": "web-device-1", "device_secret": "one-time-device-secret"},
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        provisioning_client.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            **kwargs, transport=httpx.MockTransport(handle)
        ),
    )
    client = DisplayProvisioningClient("http://display-service:8000", token_path)

    credential = asyncio.run(client.create("Kitchen display", "web-device-1"))

    assert credential == {
        "device_id": "web-device-1",
        "device_secret": "one-time-device-secret",
    }
    assert captured["method"] == "POST"
    assert captured["url"] == "http://display-service:8000/internal/v1/provisioning/devices"
    assert captured["authorization"] == "Bearer internal-token-value-which-is-long-enough"
    assert captured["body"] == '{"name":"Kitchen display","device_id":"web-device-1"}'
    assert "wifi_password" not in captured["body"]


def test_display_provisioning_client_maps_conflicts_and_sanitizes_failures(
    monkeypatch, tmp_path
):
    token_path = tmp_path / "gateway-token"
    token_path.write_text("internal-token-value-which-is-long-enough", encoding="ascii")
    responses = iter(
        [
            httpx.Response(409, json={"detail": "conflict"}),
            httpx.Response(500, json={"detail": "sensitive backend response"}),
        ]
    )
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        provisioning_client.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            **kwargs, transport=httpx.MockTransport(lambda request: next(responses))
        ),
    )
    client = DisplayProvisioningClient("http://display-service:8000", token_path)

    with pytest.raises(DeviceRegistrationConflict):
        asyncio.run(client.create("Kitchen display", "web-device-1"))
    with pytest.raises(DisplayProvisioningUnavailable) as error:
        asyncio.run(client.create("Kitchen display", "web-device-2"))
    assert "sensitive backend response" not in str(error.value)
