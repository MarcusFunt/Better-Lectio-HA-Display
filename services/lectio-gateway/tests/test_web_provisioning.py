import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from lectio_gateway.auth.manager import AuthManager
from lectio_gateway.main import app


class FakeProvisioningClient:
    def __init__(self):
        self.credentials = None
        self.revoked = []

    async def create(self, name, device_id):
        self.credentials = {"device_id": device_id, "device_secret": "one-time-secret"}
        return self.credentials

    async def revoke(self, device_id):
        self.revoked.append(device_id)
        return {"device_id": device_id, "revoked": True}

    async def status(self, device_id):
        return {"device_id": device_id, "registered": True, "last_seen": None}


def test_web_provisioning_register_route_is_same_origin_and_returns_secret_once(tmp_path):
    async def run():
        previous_manager = getattr(app.state, "auth_manager", None)
        previous_client = getattr(app.state, "display_provisioning_client", None)
        app.state.auth_manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://browser:6080",
            login_url="https://www.lectio.dk/",
        )
        provisioning = FakeProvisioningClient()
        app.state.display_provisioning_client = provisioning
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                rejected = await client.post(
                    "/auth/provisioning/devices",
                    json={"device_id": "web-device-1", "name": "Kitchen display"},
                )
                cross_origin = await client.post(
                    "/auth/provisioning/devices",
                    headers={"origin": "https://attacker.example"},
                    json={"device_id": "web-device-1", "name": "Kitchen display"},
                )
                created = await client.post(
                    "/auth/provisioning/devices",
                    headers={"origin": "http://test"},
                    json={"device_id": "web-device-1", "name": "Kitchen display"},
                )
            assert rejected.status_code == 403
            assert cross_origin.status_code == 403
            assert created.status_code == 201
            assert created.json() == {
                "device_id": "web-device-1",
                "device_secret": "one-time-secret",
            }
            assert created.headers["cache-control"] == "no-store"
            assert provisioning.credentials["device_secret"] == "one-time-secret"
        finally:
            if previous_manager is None:
                del app.state.auth_manager
            else:
                app.state.auth_manager = previous_manager
            if previous_client is None:
                del app.state.display_provisioning_client
            else:
                app.state.display_provisioning_client = previous_client

    asyncio.run(run())


def test_web_provisioning_rejects_invalid_fields_and_exposes_revoke_status(tmp_path):
    async def run():
        previous_manager = getattr(app.state, "auth_manager", None)
        previous_client = getattr(app.state, "display_provisioning_client", None)
        app.state.auth_manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://browser:6080",
            login_url="https://www.lectio.dk/",
        )
        provisioning = FakeProvisioningClient()
        app.state.display_provisioning_client = provisioning
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                invalid = await client.post(
                    "/auth/provisioning/devices",
                    headers={"origin": "http://test"},
                    json={"device_id": "bad/id", "name": "Kitchen display"},
                )
                status = await client.get(
                    "/auth/provisioning/devices/web-device-1"
                )
                revoked = await client.post(
                    "/auth/provisioning/devices/web-device-1/revoke",
                    headers={"origin": "http://test"},
                )
            assert invalid.status_code == 422
            assert status.status_code == 200
            assert status.json() == {
                "device_id": "web-device-1",
                "registered": True,
                "last_seen": None,
            }
            assert revoked.status_code == 200
            assert provisioning.revoked == ["web-device-1"]
        finally:
            if previous_manager is None:
                del app.state.auth_manager
            else:
                app.state.auth_manager = previous_manager
            if previous_client is None:
                del app.state.display_provisioning_client
            else:
                app.state.display_provisioning_client = previous_client

    asyncio.run(run())


def test_login_page_links_web_serial_workflow_and_keeps_credentials_out_of_url(tmp_path):
    async def run():
        previous_manager = getattr(app.state, "auth_manager", None)
        app.state.auth_manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://browser:6080",
            login_url="https://www.lectio.dk/",
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get("/auth/browser")
                script = await client.get("/auth/usb-provisioning.js")
            assert page.status_code == 200
            assert 'id="usb-provisioning-form"' in page.text
            assert 'id="usb-connect"' in page.text
            assert "/auth/usb-provisioning.js" in page.text
            assert "Web Serial" in page.text
            assert "Flash firmware separately" in page.text
            assert script.status_code == 200
            assert "navigator.serial.requestPort" in script.text
            assert "wifi_password" in script.text
            assert "localStorage" not in script.text
            assert "sessionStorage" not in script.text
        finally:
            if previous_manager is None:
                del app.state.auth_manager
            else:
                app.state.auth_manager = previous_manager

    asyncio.run(run())
