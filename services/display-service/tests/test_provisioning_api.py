import asyncio
import json

from display_service.main import DisplayBackend, app
from display_service.provisioning_auth import ProvisioningTokenStore
from httpx import ASGITransport, AsyncClient


def test_provisioning_token_is_persistent_and_private(tmp_path):
    store = ProvisioningTokenStore(tmp_path / "internal-auth")

    token = store.load_or_create()

    assert len(token) >= 40
    assert store.load_or_create() == token
    assert (tmp_path / "internal-auth" / "gateway-token").stat().st_mode & 0o777 == 0o600


def test_provisioning_token_store_fails_closed_for_invalid_existing_file(tmp_path):
    directory = tmp_path / "internal-auth"
    directory.mkdir()
    (directory / "gateway-token").write_text("short", encoding="ascii")

    try:
        ProvisioningTokenStore(directory).load_or_create()
    except RuntimeError as error:
        assert "invalid" in str(error)
    else:
        raise AssertionError("Invalid provisioning token was accepted")


def test_internal_provisioning_api_registers_revokes_and_reports_device(tmp_path):
    async def run():
        previous_backend = getattr(app.state, "display_backend", None)
        previous_token = getattr(app.state, "provisioning_token", None)
        app.state.display_backend = DisplayBackend(tmp_path)
        app.state.provisioning_token = "internal-provisioning-secret-value-1234567890"
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                headers = {"Authorization": "Bearer internal-provisioning-secret-value-1234567890"}
                created = await client.post(
                    "/internal/v1/provisioning/devices",
                    headers=headers,
                    json={"device_id": "web-device-1", "name": "Kitchen display"},
                )
                assert created.status_code == 201
                credential = created.json()
                assert credential["device_id"] == "web-device-1"
                assert isinstance(credential["device_secret"], str)
                assert "Cache-Control" in created.headers

                persisted = json.loads(
                    (tmp_path / "devices.json").read_text(encoding="utf-8")
                )
                assert credential["device_secret"] not in json.dumps(persisted)

                status = await client.get(
                    "/internal/v1/provisioning/devices/web-device-1",
                    headers=headers,
                )
                assert status.status_code == 200
                assert status.json()["registered"] is True
                assert status.json()["last_seen"] is None

                revoked = await client.post(
                    "/internal/v1/provisioning/devices/web-device-1/revoke",
                    headers=headers,
                )
                assert revoked.status_code == 200
                assert revoked.json() == {"device_id": "web-device-1", "revoked": True}
        finally:
            if previous_backend is None:
                del app.state.display_backend
            else:
                app.state.display_backend = previous_backend
            if previous_token is None:
                del app.state.provisioning_token
            else:
                app.state.provisioning_token = previous_token

    asyncio.run(run())


def test_internal_provisioning_api_rejects_missing_or_wrong_token(tmp_path):
    async def run():
        previous_backend = getattr(app.state, "display_backend", None)
        previous_token = getattr(app.state, "provisioning_token", None)
        app.state.display_backend = DisplayBackend(tmp_path)
        app.state.provisioning_token = "internal-provisioning-secret-value-1234567890"
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                missing = await client.post(
                    "/internal/v1/provisioning/devices",
                    json={"device_id": "web-device-1", "name": "Kitchen display"},
                )
                wrong = await client.post(
                    "/internal/v1/provisioning/devices",
                    headers={"Authorization": "Bearer wrong"},
                    json={"device_id": "web-device-1", "name": "Kitchen display"},
                )
            assert missing.status_code == wrong.status_code == 401
        finally:
            if previous_backend is None:
                del app.state.display_backend
            else:
                app.state.display_backend = previous_backend
            if previous_token is None:
                del app.state.provisioning_token
            else:
                app.state.provisioning_token = previous_token

    asyncio.run(run())
