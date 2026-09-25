"""Read-only, Compose-internal diagnostics for the latest rendered bitmap."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response

from .image_store import DisplayImageStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = Path(
        os.getenv("DISPLAY_DATA_DIR", "/var/lib/better-lectio-display")
    )
    app.state.display_image_store = DisplayImageStore(data_dir, read_only=True)
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
