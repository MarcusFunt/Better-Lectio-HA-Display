import asyncio
from datetime import datetime, timezone

from display_service.main import DisplayBackend, app
from display_service.model import DisplayModel
from display_service.renderer import render_display
from httpx import ASGITransport, AsyncClient


def test_device_api_serves_persisted_image_and_rejects_bad_or_revoked_credentials(
    tmp_path,
):
    async def run():
        previous_backend = getattr(app.state, "display_backend", None)
        backend = DisplayBackend(tmp_path)
        credential = backend.devices.create("Test display", device_id="test-display")
        model = DisplayModel(
            generated_at=datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc),
            days=(),
            sidebar=(),
        )
        revision = backend.images.publish(render_display(model).bmp, model.generated_at)

        # Recreate the backend to verify image and credential persistence.
        app.state.display_backend = DisplayBackend(tmp_path)
        headers = {
            "X-Device-ID": credential.record.device_id,
            "Authorization": f"Bearer {credential.secret}",
        }
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                status = await client.get("/device/v1/status", headers=headers)
                assert status.status_code == 200
                assert status.json()["current_content_hash"] == revision.content_hash

                metadata = await client.get("/device/v1/display", headers=headers)
                assert metadata.status_code == 200
                assert metadata.json()["content_hash"] == revision.content_hash
                image = await client.get(metadata.json()["image_url"], headers=headers)
                assert image.status_code == 200
                assert image.headers["content-type"] == "image/bmp"
                assert image.content == render_display(model).bmp

                bad_secret = await client.get(
                    "/device/v1/status",
                    headers={**headers, "Authorization": "Bearer wrong"},
                )
                assert bad_secret.status_code == 401

                app.state.display_backend.devices.revoke(credential.record.device_id)
                revoked = await client.get("/device/v1/status", headers=headers)
                assert revoked.status_code == 401
        finally:
            if previous_backend is None:
                del app.state.display_backend
            else:
                app.state.display_backend = previous_backend

    asyncio.run(run())
