from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from lectio_auth_browser.control import BrowserControl


class StartRequest(BaseModel):
    login_url: str
    timeout_seconds: int = Field(default=900, ge=60, le=3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.browser_control = BrowserControl()
    yield
    await app.state.browser_control.stop()


app = FastAPI(title="Better Lectio Auth Browser", lifespan=lifespan)


def _control(request: Request) -> BrowserControl:
    return request.app.state.browser_control


@app.get("/health", include_in_schema=False)
async def health(request: Request) -> dict[str, str]:
    control = _control(request)
    return {"status": "ok", "service": "lectio-auth-browser", "browser": control.state}


@app.post("/session/start", status_code=status.HTTP_202_ACCEPTED)
async def start_session(body: StartRequest, request: Request) -> dict[str, str]:
    try:
        await _control(request).start(body.login_url, body.timeout_seconds)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail="A browser login is already active") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Only the Lectio homepage is allowed") from exc
    return {"state": "starting"}


@app.get("/session/status")
async def session_status(request: Request) -> dict[str, object]:
    return await _control(request).snapshot()


@app.post("/session/complete")
async def complete_session(request: Request) -> dict[str, str]:
    try:
        await _control(request).complete()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail="No validated browser session is ready") from exc
    return {"state": "idle"}


@app.post("/session/stop")
async def stop_session(request: Request) -> dict[str, str]:
    await _control(request).stop()
    return {"state": "idle"}
