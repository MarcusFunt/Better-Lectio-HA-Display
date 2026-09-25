import asyncio
import os
from contextlib import asynccontextmanager

import docker
from fastapi import FastAPI, HTTPException, Request

from lectio_auth_lifecycle.manager import BrowserLifecycle, BrowserLifecycleError


def _create_lifecycle() -> BrowserLifecycle:
    return BrowserLifecycle(
        docker_client=docker.from_env(),
        image=os.getenv("LECTIO_AUTH_BROWSER_IMAGE", "better-lectio-auth-browser:local"),
        container_name=os.getenv(
            "LECTIO_AUTH_BROWSER_CONTAINER",
            "better-lectio-ha-display-lectio-auth-browser",
        ),
        runtime_network=os.getenv(
            "LECTIO_AUTH_RUNTIME_NETWORK",
            "better-lectio-ha-display_lectio-auth-runtime",
        ),
        timezone=os.getenv("TZ", "Europe/Copenhagen"),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.lifecycle = _create_lifecycle()
    app.state.lifecycle_lock = asyncio.Lock()
    await asyncio.to_thread(app.state.lifecycle.stop)
    try:
        yield
    finally:
        await asyncio.to_thread(app.state.lifecycle.stop)


app = FastAPI(title="Better Lectio Auth Lifecycle", lifespan=lifespan)


def _lifecycle(request: Request) -> BrowserLifecycle:
    return request.app.state.lifecycle


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "lectio-auth-lifecycle"}


@app.post("/browser/start", status_code=202)
async def start_browser(request: Request) -> dict[str, str]:
    async with request.app.state.lifecycle_lock:
        try:
            await asyncio.to_thread(_lifecycle(request).start)
        except BrowserLifecycleError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"state": "starting"}


@app.post("/browser/stop")
async def stop_browser(request: Request) -> dict[str, str]:
    async with request.app.state.lifecycle_lock:
        try:
            await asyncio.to_thread(_lifecycle(request).stop)
        except BrowserLifecycleError as exc:
            raise HTTPException(status_code=503, detail="The temporary browser could not be stopped") from exc
    return {"state": "stopped"}
