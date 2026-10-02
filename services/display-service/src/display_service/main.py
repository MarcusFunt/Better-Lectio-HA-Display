"""HTTP API and lifecycle for the direct Lectio display service."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from starlette.responses import JSONResponse, Response

from .change_tracker import DisplayChangeSummary, DisplayChangeTracker
from .device_registry import DeviceRecord, DeviceRegistry
from .gateway_client import LectioGatewayClient
from .image_store import DisplayImageStore
from .model import DisplayModel
from .model_service import DisplayModelService
from .provisioning_auth import ProvisioningTokenStore
from .renderer import render_display_model

_LOGGER = logging.getLogger(__name__)
_DEFAULT_REFRESH_SECONDS = 30
_CONTENT_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class DisplayBackend:
    """Coordinate model refresh, rendering, persistent content, and devices."""

    def __init__(self, data_dir: Path) -> None:
        self.devices = DeviceRegistry(data_dir)
        self.images = DisplayImageStore(data_dir)
        self.changes = DisplayChangeTracker(data_dir / "plan-review.json")
        self._models: DisplayModelService | None = None
        self._refresh_lock = asyncio.Lock()
        self._next_refresh_at = 0.0
        self._last_model: DisplayModel | None = None
        self._last_data_stale = False

    def set_gateway(self, client: LectioGatewayClient) -> None:
        self._models = DisplayModelService(client)
        self._next_refresh_at = 0.0

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
                schedule = result.sources["schedule"]
                if schedule.state != "valid" or schedule.is_stale or all(
                    source.error is not None for source in result.sources.values()
                ):
                    _LOGGER.warning("Display refresh skipped: Lectio gateway data unavailable")
                    return
                data_stale = any(
                    source.state != "valid" or source.is_stale
                    for source in result.sources.values()
                )
                self._last_data_stale = data_stale
                summary = self.changes.update(result.model, result.sources)
                self._last_model = result.model
                bmp = render_display_model(
                    result.model,
                    change_count=summary.total,
                    acknowledged_at=summary.acknowledged_at,
                    show_status=True,
                    data_stale=data_stale,
                )
                self.images.publish(bmp, result.model.generated_at)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = getattr(error, "code", None)
                _LOGGER.warning(
                    "Display refresh failed (%s)",
                    code if isinstance(code, str) else "refresh_failed",
                )

    async def acknowledge_changes(
        self, expected_content_hash: str
    ) -> DisplayChangeSummary | None:
        """Acknowledge the latest fresh sources and publish the cleared marker."""
        async with self._refresh_lock:
            current = self.images.refresh_current()
            if (
                self._last_model is None
                or current is None
                or current.content_hash != expected_content_hash
            ):
                return None
            summary = self.changes.acknowledge()
            if summary is None:
                return None
            bmp = render_display_model(
                self._last_model,
                change_count=summary.total,
                acknowledged_at=summary.acknowledged_at,
                show_status=True,
                data_stale=self._last_data_stale,
            )
            self.images.publish(bmp, self._last_model.generated_at)
            return summary


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = Path(
        os.getenv("DISPLAY_DATA_DIR", "/var/lib/better-lectio-display")
    )
    backend = DisplayBackend(data_dir)
    app.state.display_backend = backend
    provisioning_auth_dir = Path(
        os.getenv(
            "DISPLAY_PROVISIONING_AUTH_DIR",
            "/var/lib/better-lectio-provisioning-auth",
        )
    )
    app.state.provisioning_token = ProvisioningTokenStore(
        provisioning_auth_dir
    ).load_or_create()
    async with LectioGatewayClient(os.getenv("LECTIO_GATEWAY_URL", "http://lectio-gateway:8000")) as client:
        backend.set_gateway(client)
        refresh_task = asyncio.create_task(_refresh_loop(backend))
        try:
            yield
        finally:
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await refresh_task


async def _refresh_loop(backend: DisplayBackend) -> None:
    while True:
        await backend.refresh_if_due()
        await asyncio.sleep(_DEFAULT_REFRESH_SECONDS)


app = FastAPI(title="Better Lectio Display Service", lifespan=lifespan)


@app.post(
    "/internal/v1/provisioning/devices",
    status_code=201,
    include_in_schema=False,
)
async def provision_device(request: Request, payload: dict[str, object] = Body(...)) -> Response:
    _require_provisioning_token(request)
    device_id = payload.get("device_id")
    name = payload.get("name")
    if (
        not isinstance(device_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", device_id)
        or not isinstance(name, str)
        or not name.strip()
        or len(name.strip()) > 120
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise HTTPException(status_code=422, detail="Invalid device provisioning details")
    try:
        credential = _backend(request).devices.create(name, device_id=device_id)
    except ValueError as error:
        if "already exists" in str(error):
            raise HTTPException(status_code=409, detail="Device ID is already registered") from error
        raise HTTPException(status_code=422, detail="Invalid device provisioning details") from error
    return JSONResponse(
        {
            "device_id": credential.record.device_id,
            "device_secret": credential.secret,
        },
        status_code=201,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.post(
    "/internal/v1/provisioning/devices/{device_id}/revoke",
    include_in_schema=False,
)
async def revoke_provisioned_device(device_id: str, request: Request) -> dict[str, object]:
    _require_provisioning_token(request)
    try:
        _backend(request).devices.revoke(device_id)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail="Device not found") from error
    return {"device_id": device_id, "revoked": True}


@app.get(
    "/internal/v1/provisioning/devices/{device_id}", include_in_schema=False
)
async def provisioned_device_status(device_id: str, request: Request) -> dict[str, object]:
    _require_provisioning_token(request)
    try:
        record = _backend(request).devices.get(device_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Device not found") from error
    if record is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return {
        "device_id": record.device_id,
        "registered": not record.revoked,
        "last_seen": record.last_seen,
    }


def _require_provisioning_token(request: Request) -> None:
    expected = getattr(request.app.state, "provisioning_token", None)
    authorization = request.headers.get("authorization", "")
    parts = authorization.strip().split(None, 1)
    candidate = (
        parts[1].strip()
        if len(parts) == 2 and parts[0].casefold() == "bearer"
        else ""
    )
    if not isinstance(expected, str) or len(expected) < 40:
        raise HTTPException(status_code=503, detail="Provisioning is not configured")
    if not hmac.compare_digest(expected, candidate):
        raise HTTPException(status_code=401, detail="Invalid provisioning credential")


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
        "changes_since_acknowledgement": backend.changes.summary.total,
        "change_tracking_available": backend.changes.available,
    }


@app.post("/device/v1/acknowledge")
async def acknowledge_display(
    request: Request,
    payload: dict[str, object] = Body(...),
) -> dict[str, object]:
    await _authenticated_device(request)
    content_hash = payload.get("content_hash")
    if not isinstance(content_hash, str) or not _CONTENT_HASH_PATTERN.fullmatch(
        content_hash
    ):
        raise HTTPException(status_code=422, detail="A valid content hash is required")
    summary = await _backend(request).acknowledge_changes(content_hash)
    if summary is None:
        raise HTTPException(
            status_code=409,
            detail="No fresh display data is available to acknowledge",
        )
    return {
        "status": "ok",
        "added": summary.added,
        "changed": summary.changed,
        "removed": summary.removed,
        "changes_since_acknowledgement": summary.total,
        "acknowledged_at": summary.acknowledged_at,
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
