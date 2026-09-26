import asyncio
import json
from datetime import datetime, timezone

from display_service.diagnostics import app
from display_service.image_store import DisplayImageStore
from display_service.model import DisplayModel
from display_service.renderer import render_display
from httpx import ASGITransport, AsyncClient


def test_internal_diagnostics_returns_latest_persisted_bitmap(tmp_path):
    writer = DisplayImageStore(tmp_path)
    model = DisplayModel(
        generated_at=datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc),
        days=(),
        sidebar=(),
    )
    revision = writer.publish(render_display(model).bmp, model.generated_at)
    app.state.display_image_store = DisplayImageStore(tmp_path, read_only=True)

    async def request_latest():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            metadata = await client.get("/diagnostics/current")
            image = await client.get(
                f"/diagnostics/image/{revision.content_hash}.bmp"
            )
        return metadata, image

    async def request_current():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/diagnostics/current")

    metadata, image = asyncio.run(request_latest())

    assert metadata.status_code == 200
    assert metadata.json() == {
        "available": True,
        "content_hash": revision.content_hash,
        "generated_at": "2026-09-25T06:00:00+00:00",
    }
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/bmp"
    assert image.headers["cache-control"] == "no-store"
    assert image.content == render_display(model).bmp

    newer_model = DisplayModel(
        generated_at=datetime(2026, 9, 25, 7, 0, tzinfo=timezone.utc),
        days=(),
        sidebar=(),
    )
    newer_revision = writer.publish(
        render_display(newer_model).bmp, newer_model.generated_at
    )
    latest = asyncio.run(request_current())

    assert latest.status_code == 200
    assert latest.json()["content_hash"] == newer_revision.content_hash


def test_internal_diagnostics_reports_when_no_bitmap_exists(tmp_path):
    app.state.display_image_store = DisplayImageStore(
        tmp_path / "empty", read_only=True
    )

    async def request_latest():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            metadata = await client.get("/diagnostics/current")
            image = await client.get("/diagnostics/image/" + "a" * 64 + ".bmp")
        return metadata, image

    metadata, image = asyncio.run(request_latest())

    assert metadata.status_code == 200
    assert metadata.json() == {"available": False, "state": "not_rendered"}
    assert image.status_code == 404


def test_home_assistant_setup_diagnostics_allowlist_state_and_timestamp(tmp_path):
    (tmp_path / "home-assistant-setup.json").write_text(
        json.dumps(
            {
                "state": "unauthorized",
                "last_checked_at": "2026-09-26T08:30:00+00:00",
                "token": "private-token",
                "url": "http://private-host:8123",
                "entity_id": "calendar.private_name",
            }
        ),
        encoding="utf-8",
    )
    app.state.display_data_dir = tmp_path

    async def request_setup():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/diagnostics/setup")

    response = asyncio.run(request_setup())

    assert response.status_code == 200
    assert response.json() == {
        "state": "unauthorized",
        "last_checked_at": "2026-09-26T08:30:00+00:00",
    }
    assert "private-token" not in response.text
    assert "private-host" not in response.text
    assert "calendar.private_name" not in response.text


def test_home_assistant_setup_diagnostics_defaults_to_unavailable(tmp_path):
    app.state.display_data_dir = tmp_path

    async def request_setup():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/diagnostics/setup")

    response = asyncio.run(request_setup())

    assert response.status_code == 200
    assert response.json() == {"state": "unavailable"}

    (tmp_path / "home-assistant-setup.json").write_text(
        json.dumps({"state": ["invalid"]}), encoding="utf-8"
    )
    malformed = asyncio.run(request_setup())
    assert malformed.status_code == 200
    assert malformed.json() == {"state": "unavailable"}
