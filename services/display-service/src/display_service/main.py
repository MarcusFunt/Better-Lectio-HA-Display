"""HTTP API and lifecycle for the Home Assistant-backed display service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response

from .device_registry import DeviceRecord, DeviceRegistry
from .entity_config import HomeAssistantEntityConfig
from .ha_client import HomeAssistantClient
from .image_store import DisplayImageStore
from .model_service import DisplayModelService
from .renderer import render_display_model
from .setup_config import (
    HomeAssistantDisplaySettings,
    HomeAssistantDisplaySettingsStore,
)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_REFRESH_SECONDS = 30
_HOME_ASSISTANT_SETUP_FILENAME = "home-assistant-setup.json"


class DisplayBackend:
    """Coordinate model refresh, rendering, persistent content, and devices."""

    def __init__(
        self,
        data_dir: Path,
        entity_config: HomeAssistantEntityConfig | None = None,
    ) -> None:
        self.devices = DeviceRegistry(data_dir)
        self.images = DisplayImageStore(data_dir)
        self._setup_status_path = data_dir / _HOME_ASSISTANT_SETUP_FILENAME
        self._entity_config = entity_config or HomeAssistantEntityConfig()
        self._models: DisplayModelService | None = None
        self._refresh_lock = asyncio.Lock()
        self._next_refresh_at = 0.0
        self.set_home_assistant_setup_state("not_configured")

    def set_home_assistant(
        self,
        client: HomeAssistantClient,
        entity_config: HomeAssistantEntityConfig | None = None,
    ) -> None:
        if entity_config is not None:
            self._entity_config = entity_config
        self._models = DisplayModelService(client, entity_config=self._entity_config)
        self._next_refresh_at = 0.0
        self.set_home_assistant_setup_state("checking")

    def clear_home_assistant(self, state: str) -> None:
        self._models = None
        self._next_refresh_at = 0.0
        self.set_home_assistant_setup_state(state)

    def set_home_assistant_setup_state(self, state: str) -> None:
        """Persist a safe, allowlisted Home Assistant setup state for diagnostics."""
        allowed_states = {
            "not_configured",
            "invalid_configuration",
            "checking",
            "connected",
            "unauthorized",
            "unreachable",
            "entity_problem",
            "partial_error",
        }
        if state not in allowed_states:
            state = "unavailable"
        payload: dict[str, str] = {"state": state}
        if state not in {"not_configured", "checking"}:
            payload["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        self._setup_status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._setup_status_path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(payload), encoding="utf-8")
        temporary_path.replace(self._setup_status_path)

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
                self.set_home_assistant_setup_state(
                    _home_assistant_setup_state(result.sources.values())
                )
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
                self.set_home_assistant_setup_state(
                    "unreachable" if code == "connection_failed" else "partial_error"
                )
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
    runtime = HomeAssistantRuntime(
        backend,
        Path(
            os.getenv(
                "HOME_ASSISTANT_CONFIG_DIR", "/var/lib/better-lectio-ha-setup"
            )
        ),
        os.environ,
    )
    refresh_task = asyncio.create_task(_refresh_loop(backend, runtime))
    try:
        yield
    finally:
        refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await refresh_task
        await runtime.close()


class HomeAssistantRuntime:
    """Apply config-file or legacy environment changes between refreshes."""

    def __init__(self, backend: DisplayBackend, config_dir: Path, environ) -> None:
        self.backend = backend
        self.settings_store = HomeAssistantDisplaySettingsStore(config_dir)
        self.environ = environ
        self._settings: HomeAssistantDisplaySettings | None = None
        self._client: HomeAssistantClient | None = None
        self._invalid_configuration = False

    async def reload_configuration(self) -> None:
        try:
            settings = self.settings_store.load(self.environ)
        except ValueError:
            if self._invalid_configuration and self._client is None:
                return
            async with self.backend._refresh_lock:
                await self._replace_client()
                self._settings = None
                self._invalid_configuration = True
                self.backend.clear_home_assistant("invalid_configuration")
            return

        if settings == self._settings and not self._invalid_configuration:
            return
        async with self.backend._refresh_lock:
            self._invalid_configuration = False
            await self._replace_client()
            self._settings = settings
            if settings is None:
                self.backend.clear_home_assistant("not_configured")
                return

            client: HomeAssistantClient | None = None
            try:
                client = HomeAssistantClient(settings.ha_url, settings.ha_token)
                await client.__aenter__()
            except ValueError:
                if client is not None:
                    await client.__aexit__(None, None, None)
                self._invalid_configuration = True
                self._settings = None
                self.backend.clear_home_assistant("invalid_configuration")
                return
            self._client = client
            self.backend.set_home_assistant(client, settings.entity_config)

    async def _replace_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.__aexit__(None, None, None)

    async def close(self) -> None:
        await self._replace_client()


async def _refresh_loop(
    backend: DisplayBackend, runtime: HomeAssistantRuntime
) -> None:
    while True:
        await runtime.reload_configuration()
        await backend.refresh_if_due()
        await asyncio.sleep(_DEFAULT_REFRESH_SECONDS)


app = FastAPI(title="Better Lectio Display Service", lifespan=lifespan)


def _home_assistant_setup_state(sources) -> str:
    """Summarize source errors without including entity IDs or HA response data."""
    error_codes = {
        source.error for source in sources if isinstance(source.error, str)
    }
    if "unauthorized" in error_codes or "forbidden" in error_codes:
        return "unauthorized"
    if error_codes and error_codes <= {"connection_failed"}:
        return "unreachable"
    if error_codes & {"not_found", "entity_unavailable"}:
        return "entity_problem"
    if error_codes or any(source.state == "error" for source in sources):
        return "partial_error"
    return "connected"


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
