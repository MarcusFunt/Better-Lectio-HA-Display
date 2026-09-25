"""HTTP API and lifecycle for the Home Assistant-backed display service."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response

from .device_registry import DeviceRecord, DeviceRegistry
from .ha_client import HomeAssistantClient
from .image_store import DisplayImageStore
from .model_service import DisplayModelService
from .renderer import render_display_model

_LOGGER = logging.getLogger(__name__)
_DEFAULT_REFRESH_SECONDS = 30


class DisplayBackend:
    """Coordinate model refresh, rendering, persistent content, and devices."""

    def __init__(self, data_dir: Path) -> None:
        self.devices = DeviceRegistry(data_dir)
        self.images = DisplayImageStore(data_dir)
        self._models: DisplayModelService | None = None
        self._refresh_lock = asyncio.Lock()
        self._next_refresh_at = 0.0

    def set_home_assistant(self, client: HomeAssistantClient) -> None:
        self._models = DisplayModelService(client)

    async def refresh_if_due(self) -> None:
        """Refresh at most once per polling interval; preserve the last good BMP."""
        models = self._models
        if models is None or time.monotonic() < self._next_refresh_at:
            return
        async with self._refresh_lock:
            now = time.monotonic()
            if now < self._next_refresh_at:
                return
            self._next_refresh_at = now + _DEFAULT_REFRESH_SECONDS
            try:
                result = await models.async_build()
                usable = any(
                    source.state in {"valid", "stale", "expired"}
                    for source in result.sources.values()
                )
                if not usable:
                    _LOGGER.warning("Display refresh skipped: no usable Home Assistant source")
                    return
                calendar_sources = {
                    entity_id: source
                    for entity_id, source in result.sources.items()
                    if entity_id.startswith("calendar.")
                }
                usable_calendars = {
                    entity_id
                    for entity_id, source in calendar_sources.items()
                    if source.state in {"valid", "stale", "expired"}
                }
                if not usable_calendars:
                    _LOGGER.warning("Display refresh skipped: no usable calendar source")
                    return
                if self.images.current is not None and usable_calendars != set(calendar_sources):
                    _LOGGER.warning(
                        "Display refresh skipped: preserving image until all calendar sources are usable"
                    )
                    return
                bmp = render_display_model(result.model)
                self.images.publish(bmp, result.model.generated_at)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = getattr(error, "code", None)
                _LOGGER.warning(
                    "Display refresh failed (%s)",
                    code if isinstance(code, str) else "refresh_failed",
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = Path(
        os.getenv("DISPLAY_DATA_DIR", "/var/lib/better-lectio-display")
    )
    backend = DisplayBackend(data_dir)
    app.state.display_backend = backend

    ha_url = os.getenv("HOME_ASSISTANT_URL", "").strip()
    ha_token = os.getenv("HOME_ASSISTANT_TOKEN", "").strip()
    client: HomeAssistantClient | None = None
    refresh_task: asyncio.Task | None = None
    if ha_url and ha_token:
        try:
            client = HomeAssistantClient(ha_url, ha_token)
            await client.__aenter__()
            backend.set_home_assistant(client)
            refresh_task = asyncio.create_task(_refresh_loop(backend))
        except ValueError:
            _LOGGER.warning("Home Assistant display source is not configured correctly")
            if client is not None:
                await client.__aexit__(None, None, None)
                client = None
    try:
        yield
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await refresh_task
        if client is not None:
            await client.__aexit__(None, None, None)


async def _refresh_loop(backend: DisplayBackend) -> None:
    while True:
        await backend.refresh_if_due()
        await asyncio.sleep(_DEFAULT_REFRESH_SECONDS)


app = FastAPI(title="Better Lectio Display Service", lifespan=lifespan)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "display-service"}


@app.get("/device/v1/status")
async def device_status(request: Request) -> dict[str, object]:
    device = await _authenticated_device(request)
    backend = _backend(request)
    current = backend.images.current
    return {
        "status": "ok",
        "device_id": device.device_id,
        "firmware_version": device.firmware_version,
        "last_seen": device.last_seen,
        "current_content_hash": current.content_hash if current else None,
    }


@app.get("/device/v1/display")
async def display_metadata(request: Request) -> dict[str, object]:
    await _authenticated_device(request)
    backend = _backend(request)
    await backend.refresh_if_due()
    current = backend.images.current
    if current is None:
        raise HTTPException(status_code=503, detail="Display content is not available")
    return {
        "content_hash": current.content_hash,
        "image_url": f"/device/v1/image/{current.content_hash}.bmp",
        "generated_at": current.generated_at,
        "next_check_seconds": _DEFAULT_REFRESH_SECONDS,
    }


@app.get("/device/v1/image/{content_hash}.bmp")
async def display_image(content_hash: str, request: Request) -> Response:
    await _authenticated_device(request)
    bmp = _backend(request).images.read(content_hash)
    if bmp is None:
        raise HTTPException(status_code=404, detail="Display image not found")
    return Response(
        content=bmp,
        media_type="image/bmp",
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "ETag": f'"{content_hash}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _authenticated_device(request: Request) -> DeviceRecord:
    backend = _backend(request)
    device_id = request.headers.get("x-device-id")
    authorization = request.headers.get("authorization", "")
    parts = authorization.strip().split(None, 1)
    secret = (
        parts[1].strip()
        if len(parts) == 2 and parts[0].casefold() == "bearer"
        else None
    )
    if secret is not None and (not secret or len(secret) > 512):
        secret = None
    device = backend.devices.authenticate(
        device_id,
        secret,
        firmware_version=request.headers.get("x-firmware-version"),
    )
    if device is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid device credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return device


def _backend(request: Request) -> DisplayBackend:
    backend = getattr(request.app.state, "display_backend", None)
    if not isinstance(backend, DisplayBackend):
        raise HTTPException(status_code=503, detail="Display service is not ready")
    return backend
