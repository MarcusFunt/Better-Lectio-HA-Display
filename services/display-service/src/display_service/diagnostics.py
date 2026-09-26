"""Read-only, Compose-internal diagnostics for the latest rendered bitmap."""

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response

from .image_store import DisplayImageStore

_HOME_ASSISTANT_SETUP_STATES = {
    "not_configured",
    "invalid_configuration",
    "checking",
    "connected",
    "unauthorized",
    "unreachable",
    "entity_problem",
    "partial_error",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = Path(
        os.getenv("DISPLAY_DATA_DIR", "/var/lib/better-lectio-display")
    )
    app.state.display_image_store = DisplayImageStore(data_dir, read_only=True)
    app.state.display_data_dir = data_dir
    yield


app = FastAPI(title="Better Lectio Display Diagnostics", lifespan=lifespan)


def _image_store(request: Request) -> DisplayImageStore:
    store = getattr(request.app.state, "display_image_store", None)
    if not isinstance(store, DisplayImageStore):
        raise HTTPException(status_code=503, detail="Diagnostics are not ready")
    return store


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "display-diagnostics"}


@app.get("/diagnostics/current", include_in_schema=False)
async def current_display(request: Request) -> dict[str, object]:
    revision = _image_store(request).refresh_current()
    if revision is None:
        return {"available": False, "state": "not_rendered"}
    return {
        "available": True,
        "content_hash": revision.content_hash,
        "generated_at": revision.generated_at,
    }


@app.get("/diagnostics/setup", include_in_schema=False)
async def home_assistant_setup(request: Request) -> dict[str, str]:
    """Return only the safe aggregate Home Assistant connection state."""
    data_dir = getattr(request.app.state, "display_data_dir", None)
    if not isinstance(data_dir, Path):
        data_dir = Path(os.getenv("DISPLAY_DATA_DIR", "/var/lib/better-lectio-display"))
    try:
        payload = json.loads(
            (data_dir / "home-assistant-setup.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {"state": "unavailable"}
    state = payload.get("state") if isinstance(payload, dict) else None
    if not isinstance(state, str) or state not in _HOME_ASSISTANT_SETUP_STATES:
        return {"state": "unavailable"}
    result = {"state": state}
    checked_at = payload.get("last_checked_at")
    if isinstance(checked_at, str):
        try:
            parsed = datetime.fromisoformat(checked_at)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            result["last_checked_at"] = parsed.isoformat()
    return result


@app.get("/diagnostics/image/{content_hash}.bmp", include_in_schema=False)
async def display_image(content_hash: str, request: Request) -> Response:
    bmp = _image_store(request).read(content_hash)
    if bmp is None:
        raise HTTPException(status_code=404, detail="Display image not found")
    return Response(
        content=bmp,
        media_type="image/bmp",
        headers={
            "Cache-Control": "no-store",
            "ETag": f'"{content_hash}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
