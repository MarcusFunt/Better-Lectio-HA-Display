import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from display_service.entity_config import HomeAssistantEntityConfig
from display_service.main import DisplayBackend, app
from display_service.model import DisplayModel
from display_service.model_builder import build_display_model
from display_service.model_service import SourceStatus
from display_service.renderer import render_display, render_display_model
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


def test_authenticated_device_acknowledges_fresh_plan_changes(tmp_path):
    async def run():
        previous_backend = getattr(app.state, "display_backend", None)
        backend = DisplayBackend(tmp_path)
        credential = backend.devices.create("Test display", device_id="test-display")
        config = HomeAssistantEntityConfig(private_calendar_entity_ids=())
        fresh = {config.lectio_calendar_entity_id: SourceStatus(state="valid")}
        now = datetime(2026, 9, 25, 7, 0, tzinfo=ZoneInfo("Europe/Copenhagen"))
        first = build_display_model(
            now=now,
            lectio_events=[
                {
                    "uid": "lesson-1",
                    "summary": "Math",
                    "start": "2026-09-25T09:00:00+02:00",
                    "end": "2026-09-25T09:45:00+02:00",
                }
            ],
        )
        changed = build_display_model(
            now=now,
            lectio_events=[
                {
                    "uid": "lesson-1",
                    "summary": "Math moved",
                    "start": "2026-09-25T10:00:00+02:00",
                    "end": "2026-09-25T10:45:00+02:00",
                }
            ],
        )
        backend.changes.update(first, fresh, config)
        pending = backend.changes.update(changed, fresh, config)
        backend._last_model = changed
        original = backend.images.publish(
            render_display_model(
                changed,
                change_count=pending.total,
                acknowledged_at=pending.acknowledged_at,
                show_status=True,
            ),
            changed.generated_at,
        )
        app.state.display_backend = backend
        headers = {
            "X-Device-ID": credential.record.device_id,
            "Authorization": f"Bearer {credential.secret}",
        }
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                stale_revision = await client.post(
                    "/device/v1/acknowledge",
                    headers=headers,
                    json={"content_hash": "0" * 64},
                )
                assert stale_revision.status_code == 409
                assert backend.changes.summary.total == 1

                response = await client.post(
                    "/device/v1/acknowledge",
                    headers=headers,
                    json={"content_hash": original.content_hash},
                )
                assert response.status_code == 200
                assert response.json()["changes_since_acknowledgement"] == 0
                assert response.json()["changed"] == 0
                assert backend.changes.summary.total == 0
                assert backend.images.current is not None
                assert backend.images.current.content_hash != original.content_hash

                rejected = await client.post(
                    "/device/v1/acknowledge",
                    headers={**headers, "Authorization": "Bearer wrong"},
                    json={"content_hash": original.content_hash},
                )
                assert rejected.status_code == 401
        finally:
            if previous_backend is None:
                del app.state.display_backend
            else:
                app.state.display_backend = previous_backend

    asyncio.run(run())


def test_device_cannot_acknowledge_when_no_fresh_model_has_been_loaded(tmp_path):
    async def run():
        previous_backend = getattr(app.state, "display_backend", None)
        backend = DisplayBackend(tmp_path)
        credential = backend.devices.create("Test display", device_id="test-display")
        app.state.display_backend = backend
        headers = {
            "X-Device-ID": credential.record.device_id,
            "Authorization": f"Bearer {credential.secret}",
        }
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/device/v1/acknowledge",
                    headers=headers,
                    json={"content_hash": "0" * 64},
                )

            assert response.status_code == 409
            assert backend.changes.summary.total == 0
        finally:
            if previous_backend is None:
                del app.state.display_backend
            else:
                app.state.display_backend = previous_backend

    asyncio.run(run())
