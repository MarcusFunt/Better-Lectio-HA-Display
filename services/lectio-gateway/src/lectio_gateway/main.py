import html
import ipaddress
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from lectio_gateway.auth.manager import (
    AuthFlowInProgress,
    AuthManager,
    AuthSessionPersistenceFailed,
    AuthSessionUnavailable,
    AuthStatus,
    StudentIdRejected,
    StudentIdVerificationUnavailable,
)
from lectio_gateway.data_service import (
    LectioAuthenticationRequired,
    LectioDataService,
    LectioSourceUnavailable,
    LectioStudentIdRequired,
)
from lectio_gateway.ha_api_auth import GatewayApiTokenStore
from lectio_gateway.lectio.models import (
    LectioAssignment,
    LectioCancellation,
    LectioHomework,
    LectioLesson,
    LectioSourceResponse,
)
from lectio_gateway.setup_config import (
    HomeAssistantEntityRoles,
    HomeAssistantSetupStore,
)

_DISPLAY_CONTENT_HASH = re.compile(r"^[a-f0-9]{64}$")
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


class DisplayDiagnosticsClient:
    """Read the latest display artifact through the private Compose API."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def current(self) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self._base_url}/diagnostics/current")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return {"available": False, "state": "unavailable"}

        if not isinstance(payload, dict):
            return {"available": False, "state": "unavailable"}
        if payload.get("available") is False and payload.get("state") == "not_rendered":
            return {"available": False, "state": "not_rendered"}
        content_hash = payload.get("content_hash")
        generated_at = payload.get("generated_at")
        if (
            payload.get("available") is not True
            or not isinstance(content_hash, str)
            or _DISPLAY_CONTENT_HASH.fullmatch(content_hash) is None
            or not isinstance(generated_at, str)
        ):
            return {"available": False, "state": "unavailable"}
        try:
            datetime.fromisoformat(generated_at)
        except ValueError:
            return {"available": False, "state": "unavailable"}
        return {
            "available": True,
            "content_hash": content_hash,
            "generated_at": generated_at,
        }

    async def setup(self) -> dict[str, str]:
        """Read an allowlisted aggregate state for display-service HA access."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self._base_url}/diagnostics/setup")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return {"state": "unavailable"}
        state = payload.get("state") if isinstance(payload, dict) else None
        if (
            not isinstance(state, str)
            or state not in _HOME_ASSISTANT_SETUP_STATES
        ):
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

    async def image(self, content_hash: str) -> bytes | None:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(
                    f"{self._base_url}/diagnostics/image/{content_hash}.bmp"
                )
        except httpx.HTTPError:
            return None
        if response.status_code != 200 or response.headers.get(
            "content-type", ""
        ).split(";", 1)[0].casefold() != "image/bmp":
            return None
        return response.content


def _display_diagnostics_client(request: Request) -> DisplayDiagnosticsClient:
    configured = getattr(request.app.state, "display_diagnostics_client", None)
    if configured is not None:
        return configured
    return DisplayDiagnosticsClient(
        os.getenv("DISPLAY_DIAGNOSTICS_URL", "http://display-diagnostics:8001")
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager = AuthManager(
        data_dir=Path(os.getenv("LECTIO_DATA_DIR", "/var/lib/better-lectio")),
        browser_url=os.getenv(
            "LECTIO_AUTH_BROWSER_URL",
            "http://better-lectio-ha-display-lectio-auth-browser:8765",
        ),
        browser_view_url=os.getenv(
            "LECTIO_AUTH_BROWSER_VIEW_URL",
            "http://localhost:6080/vnc.html?autoconnect=true&resize=scale",
        ),
        lifecycle_url=os.getenv(
            "LECTIO_AUTH_LIFECYCLE_URL", "http://lectio-auth-lifecycle:8766"
        ),
        login_url=os.getenv("LECTIO_LOGIN_URL", "https://www.lectio.dk/"),
        timeout_seconds=int(os.getenv("LECTIO_AUTH_TIMEOUT_SECONDS", "900")),
    )
    await manager.initialize()
    app.state.auth_manager = manager
    data_dir = Path(os.getenv("LECTIO_DATA_DIR", "/var/lib/better-lectio"))
    app.state.home_assistant_setup_store = HomeAssistantSetupStore(
        Path(
            os.getenv(
                "HOME_ASSISTANT_CONFIG_DIR", "/var/lib/better-lectio-ha-setup"
            )
        ),
        data_dir,
    )
    app.state.home_assistant_api_token_store = GatewayApiTokenStore(
        Path(os.getenv("LECTIO_HA_API_AUTH_DIR", "/var/lib/better-lectio-api-auth"))
    )
    data_service = LectioDataService(
        session_provider=lambda: manager.session,
        cache_path=data_dir / "lectio-data-cache.json",
        ttl_seconds=int(os.getenv("LECTIO_CACHE_TTL_SECONDS", "300")),
        on_session_expired=manager.mark_session_expired,
    )
    await data_service.initialize()
    app.state.lectio_data_service = data_service
    try:
        yield
    finally:
        await manager.shutdown()


app = FastAPI(title="Better Lectio Gateway", lifespan=lifespan)


def _manager(request: Request) -> AuthManager:
    return request.app.state.auth_manager


def _data_service(request: Request) -> LectioDataService:
    return request.app.state.lectio_data_service


def _home_assistant_setup_store(request: Request) -> HomeAssistantSetupStore:
    configured = getattr(request.app.state, "home_assistant_setup_store", None)
    if configured is not None:
        return configured
    data_dir = Path(os.getenv("LECTIO_DATA_DIR", "/var/lib/better-lectio"))
    return HomeAssistantSetupStore(
        Path(
            os.getenv(
                "HOME_ASSISTANT_CONFIG_DIR", "/var/lib/better-lectio-ha-setup"
            )
        ),
        data_dir,
    )


def _home_assistant_api_token_store(request: Request) -> GatewayApiTokenStore:
    configured = getattr(request.app.state, "home_assistant_api_token_store", None)
    if configured is not None:
        return configured
    return GatewayApiTokenStore(
        Path(os.getenv("LECTIO_HA_API_AUTH_DIR", "/var/lib/better-lectio-api-auth"))
    )


def _api_bind_scope(bind_address: str) -> str:
    if bind_address.casefold() == "localhost":
        return "loopback"
    try:
        address = ipaddress.ip_address(bind_address)
    except ValueError:
        return "host"
    if address.is_loopback:
        return "loopback"
    if address.is_unspecified:
        return "wildcard"
    return "host"


def _status_payload(current: AuthStatus) -> dict[str, object]:
    payload = current.model_dump(mode="json", exclude={"student_id"})
    payload["student_id_available"] = current.student_id is not None
    return payload


def _check_same_host(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        return

    def origin_identity(value: str, *, strict_origin: bool) -> tuple[str, str, int] | None:
        try:
            parsed = urlsplit(value)
            scheme = parsed.scheme.casefold()
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if (
            scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or (strict_origin and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment))
        ):
            return None
        return (
            scheme,
            hostname.casefold(),
            port if port is not None else (443 if scheme == "https" else 80),
        )

    if origin_identity(origin, strict_origin=True) != origin_identity(
        str(request.url), strict_origin=False
    ):
        raise HTTPException(status_code=403, detail="Cross-origin action rejected")


async def _setup_request_body(request: Request) -> dict[str, object]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid Home Assistant setup request") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid Home Assistant setup request")
    return payload


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "lectio-gateway"}


@app.get("/", include_in_schema=False)
async def home() -> RedirectResponse:
    return RedirectResponse(url="/auth/browser")


@app.get("/auth/status")
async def auth_status(request: Request) -> dict[str, object]:
    return _status_payload(_manager(request).status())


@app.get("/auth/home-assistant/setup", include_in_schema=False)
async def home_assistant_setup(
    request: Request, response: Response
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    store = _home_assistant_setup_store(request)
    settings = None
    display_config_valid = True
    try:
        settings = store.load_display_settings()
    except ValueError:
        display_config_valid = False
    try:
        gateway_api_url = store.load_gateway_api_url()
    except ValueError:
        gateway_api_url = None
    bind_address = os.getenv("LECTIO_HA_API_BIND_ADDRESS", "127.0.0.1").strip()
    try:
        host_port: int | None = int(os.getenv("LECTIO_HA_API_HOST_PORT", "8002"))
    except ValueError:
        host_port = None
    display_setup = await _display_diagnostics_client(request).setup()
    return {
        "display_configured": settings is not None,
        "display_config_valid": display_config_valid,
        "ha_url": settings.ha_url if settings is not None else "",
        "entities": (
            settings.entity_roles.model_dump(mode="json")
            if settings is not None
            else HomeAssistantEntityRoles().model_dump(mode="json")
        ),
        "display_state": display_setup.get("state", "unavailable"),
        "display_checked_at": display_setup.get("last_checked_at"),
        "gateway_api_url": gateway_api_url or "",
        "api_token_configured": _home_assistant_api_token_store(
            request
        ).managed_digest_exists,
        "api_bind_address": bind_address,
        "api_host_port": host_port,
        "api_bind_scope": _api_bind_scope(bind_address),
    }


@app.post("/auth/home-assistant/display-settings", include_in_schema=False)
async def save_home_assistant_display_settings(request: Request) -> dict[str, object]:
    _check_same_host(request)
    payload = await _setup_request_body(request)
    ha_url = payload.get("ha_url")
    ha_token = payload.get("ha_token", "")
    entities = payload.get("entities", {})
    if not isinstance(ha_url, str) or not isinstance(ha_token, str) or not isinstance(entities, dict):
        raise HTTPException(status_code=422, detail="Invalid Home Assistant settings")
    try:
        entity_roles = HomeAssistantEntityRoles.model_validate(entities)
        settings = _home_assistant_setup_store(request).update_display_settings(
            ha_url=ha_url,
            ha_token=ha_token,
            entity_roles=entity_roles,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid Home Assistant settings") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Home Assistant settings could not be saved") from exc
    return {
        "saved": True,
        "ha_url": settings.ha_url,
        "entities": settings.entity_roles.model_dump(mode="json"),
    }


@app.post("/auth/home-assistant/gateway-api-url", include_in_schema=False)
async def save_home_assistant_gateway_api_url(request: Request) -> dict[str, object]:
    _check_same_host(request)
    payload = await _setup_request_body(request)
    url = payload.get("url")
    if not isinstance(url, str):
        raise HTTPException(status_code=422, detail="Enter a Home Assistant reachable API URL")
    try:
        _home_assistant_setup_store(request).save_gateway_api_url(url)
        normalized_url = _home_assistant_setup_store(request).load_gateway_api_url()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Enter a valid Home Assistant reachable API URL") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Gateway API URL could not be saved") from exc
    return {"saved": True, "url": normalized_url}


@app.post("/auth/home-assistant/gateway-api-token", include_in_schema=False)
async def create_home_assistant_gateway_api_token(
    request: Request, response: Response
) -> dict[str, object]:
    _check_same_host(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        token = _home_assistant_api_token_store(request).create_or_rotate()
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Gateway API token could not be created") from exc
    return {"token": token, "show_once": True}


@app.post("/auth/home-assistant/disconnect", include_in_schema=False)
async def disconnect_home_assistant_display(request: Request) -> dict[str, bool]:
    _check_same_host(request)
    try:
        _home_assistant_setup_store(request).clear_display_settings()
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Home Assistant settings could not be cleared") from exc
    return {"disconnected": True}


@app.get("/auth/diagnostics", include_in_schema=False)
async def auth_diagnostics(request: Request) -> dict[str, object]:
    manager = _manager(request)
    current = manager.status()
    session = manager.session
    sources = {
        source: sync.model_dump(
            mode="json",
            include={
                "state",
                "is_stale",
                "last_attempt_at",
                "last_successful_sync",
            },
        )
        for source, sync in _data_service(request).statuses().items()
    }
    display = await _display_diagnostics_client(request).current()
    home_assistant_setup = await _display_diagnostics_client(request).setup()
    if display.get("available") is True:
        content_hash = display["content_hash"]
        display = {
            "available": True,
            "content_hash": content_hash,
            "generated_at": display["generated_at"],
            "image_url": f"/auth/diagnostics/bitmap/{content_hash}.bmp",
        }
    elif display.get("state") == "not_rendered":
        display = {"available": False, "state": "not_rendered"}
    else:
        display = {"available": False, "state": "unavailable"}
    return {
        "auth": {
            "state": current.state.value,
            "student_id_available": session is not None
            and session.student_id is not None,
        },
        "sources": sources,
        "display": display,
        "home_assistant_setup": home_assistant_setup,
    }


@app.get("/auth/diagnostics/bitmap/{content_hash}.bmp", include_in_schema=False)
async def auth_diagnostics_bitmap(content_hash: str, request: Request) -> Response:
    if _DISPLAY_CONTENT_HASH.fullmatch(content_hash) is None:
        raise HTTPException(status_code=404, detail="Display image not found")
    bmp = await _display_diagnostics_client(request).image(content_hash)
    if bmp is None:
        raise HTTPException(status_code=404, detail="Display image not found")
    return Response(
        content=bmp,
        media_type="image/bmp",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/v1/status")
async def api_status(request: Request) -> dict[str, object]:
    manager = _manager(request)
    current = manager.status()
    session = manager.session
    return {
        "auth": {
            "state": current.state.value,
            "student_id_available": (
                session is not None and session.student_id is not None
            ),
            "last_verified_at": (
                current.last_verified_at.isoformat()
                if current.last_verified_at is not None
                else None
            ),
            "error": current.error,
        },
        "sources": {
            source: sync.model_dump(mode="json")
            for source, sync in _data_service(request).statuses().items()
        },
    }


async def _source_response(
    request: Request,
    source: str,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    if (
        start.tzinfo is None
        or start.utcoffset() is None
        or end.tzinfo is None
        or end.utcoffset() is None
        or start >= end
    ):
        raise HTTPException(
            status_code=422,
            detail="Use ordered start and end datetimes with timezone offsets.",
        )
    try:
        items, sync = await _data_service(request).get_source(source, start, end)
    except LectioAuthenticationRequired as exc:
        raise HTTPException(
            status_code=401, detail="Lectio sign-in is required."
        ) from exc
    except LectioStudentIdRequired as exc:
        raise HTTPException(
            status_code=409, detail="Configure a student ID for this Lectio source."
        ) from exc
    except LectioSourceUnavailable as exc:
        raise HTTPException(
            status_code=502, detail="The Lectio source is unavailable."
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Use ordered start and end datetimes with timezone offsets.",
        ) from exc
    return {
        "items": [item.model_dump(mode="json") for item in items],
        "sync": sync.model_dump(mode="json"),
    }


@app.get("/api/v1/schedule")
async def api_schedule(
    request: Request, start: datetime, end: datetime
) -> LectioSourceResponse[LectioLesson]:
    return await _source_response(request, "schedule", start, end)


@app.get("/api/v1/assignments")
async def api_assignments(
    request: Request, start: datetime, end: datetime
) -> LectioSourceResponse[LectioAssignment]:
    return await _source_response(request, "assignments", start, end)


@app.get("/api/v1/homework")
async def api_homework(
    request: Request, start: datetime, end: datetime
) -> LectioSourceResponse[LectioHomework]:
    return await _source_response(request, "homework", start, end)


@app.get("/api/v1/cancellations")
async def api_cancellations(
    request: Request, start: datetime, end: datetime
) -> LectioSourceResponse[LectioCancellation]:
    return await _source_response(request, "cancellations", start, end)


@app.post("/auth/start", status_code=status.HTTP_202_ACCEPTED)
async def auth_start(request: Request) -> dict[str, object]:
    _check_same_host(request)
    try:
        current = await _manager(request).start()
    except AuthFlowInProgress as exc:
        raise HTTPException(status_code=409, detail="Authentication is already in progress") from exc
    return _status_payload(current)


@app.post("/auth/cancel")
async def auth_cancel(request: Request) -> dict[str, object]:
    _check_same_host(request)
    current = await _manager(request).cancel()
    return _status_payload(current)


@app.post("/auth/logout")
async def auth_logout(request: Request) -> dict[str, object]:
    _check_same_host(request)
    current = await _manager(request).logout()
    return _status_payload(current)


@app.post("/auth/student-id", include_in_schema=False)
async def configure_student_id(request: Request) -> dict[str, bool]:
    _check_same_host(request)
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Enter a numeric student ID.") from exc
    student_id = body.get("student_id") if isinstance(body, dict) else None
    if not isinstance(student_id, str) or re.fullmatch(r"[0-9]+", student_id) is None:
        raise HTTPException(status_code=422, detail="Enter a numeric student ID.")

    try:
        await _manager(request).configure_student_id(student_id)
    except (AuthFlowInProgress, AuthSessionUnavailable) as exc:
        raise HTTPException(
            status_code=409,
            detail="Complete Lectio sign-in before configuring a student ID.",
        ) from exc
    except StudentIdRejected as exc:
        raise HTTPException(
            status_code=422,
            detail="Lectio could not use that ID with the current session. Check it and try again.",
        ) from exc
    except StudentIdVerificationUnavailable as exc:
        raise HTTPException(
            status_code=502,
            detail="Lectio could not verify the student ID right now. Try again later.",
        ) from exc
    except AuthSessionPersistenceFailed as exc:
        raise HTTPException(
            status_code=500,
            detail="The verified student ID could not be saved. Try again.",
        ) from exc
    return {"student_id_available": True}


@app.get("/auth/browser", response_class=HTMLResponse)
async def auth_browser(request: Request) -> HTMLResponse:
    view_url = html.escape(_manager(request).browser_view_url, quote=True)
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lectio sign-in</title>
  <style>
    body {{ font: 16px system-ui, sans-serif; margin: 2rem auto; max-width: 1100px; padding: 0 1rem; }}
    button {{ margin: 0 .4rem .5rem 0; padding: .65rem 1rem; }}
    input {{ box-sizing: border-box; max-width: 100%; padding: .5rem; width: 100%; }}
    label {{ display: block; font-weight: 600; margin: .75rem 0 .25rem; }}
    .setup {{ margin: 1.5rem 0; padding: 1rem; border: 1px solid #bbb; border-radius: .5rem; }}
    .setup-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 1rem; }}
    .setup-note {{ background: #fff6d8; padding: .75rem; border-radius: .35rem; }}
    #gateway-api-token {{ font-family: ui-monospace, monospace; }}
    iframe {{ border: 1px solid #777; width: 100%; height: min(70vh, 800px); }}
    pre {{ background: #f1f1f1; padding: 1rem; white-space: pre-wrap; }}
    .diagnostics {{ margin: 1.5rem 0; padding: 1rem; border: 1px solid #bbb; border-radius: .5rem; }}
    .diagnostics-grid {{ display: grid; grid-template-columns: minmax(16rem, 1fr) minmax(20rem, 1.4fr); gap: 1.5rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #ddd; padding: .45rem; text-align: left; vertical-align: top; }}
    #display-preview {{ border: 1px solid #777; display: block; height: auto; image-rendering: pixelated; max-width: 100%; }}
    #display-hash {{ overflow-wrap: anywhere; }}
    @media (max-width: 720px) {{ .diagnostics-grid {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 720px) {{ .setup-grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <h1>Lectio sign-in</h1>
  <p>Start a temporary browser, then complete the Lectio and MitID steps yourself. This service does not automate MitID.</p>
  <button onclick="action('/auth/start')">Start browser login</button>
  <button onclick="action('/auth/cancel')">Cancel</button>
  <button onclick="action('/auth/logout')">Log out</button>
  <form id="student-id-form" onsubmit="configureStudentId(event)">
    <label for="student-id">Your Lectio student ID</label>
    <input id="student-id" name="student_id" type="text" inputmode="numeric" pattern="[0-9]+" autocomplete="off" required>
    <button type="submit">Save and check</button>
    <span id="student-id-result" aria-live="polite"></span>
  </form>
  <p>Enter your own ID. Lectio checks schedule access before it is saved; this page only reports whether an ID was saved.</p>
  <section class="setup" aria-labelledby="ha-setup-heading">
    <h2 id="ha-setup-heading">Home Assistant setup</h2>
    <p>Configure the display connection here, then use Home Assistant to install and configure the Better Lectio integration.</p>
    <p id="ha-setup-status" role="status">Loading Home Assistant setup…</p>
    <form id="ha-setup-form" onsubmit="configureHomeAssistant(event)">
      <h3>Display connection</h3>
      <div class="setup-grid">
        <div>
          <label for="ha-url">Home Assistant URL reachable from this server</label>
          <input id="ha-url" type="url" placeholder="http://homeassistant.local:8123" required>
        </div>
        <div>
          <label for="ha-token">Home Assistant long-lived access token</label>
          <input id="ha-token" type="password" autocomplete="new-password" placeholder="Leave blank to keep the saved token">
        </div>
        <div>
          <label for="ha-lectio-calendar">Lectio calendar entity</label>
          <input id="ha-lectio-calendar" value="calendar.lectio" required>
        </div>
        <div>
          <label for="ha-private-calendars">Private calendar entities (comma-separated, optional)</label>
          <input id="ha-private-calendars" value="calendar.private">
        </div>
        <div>
          <label for="ha-assignments-todo">Assignments to-do entity</label>
          <input id="ha-assignments-todo" value="todo.lectio_assignments" required>
        </div>
        <div>
          <label for="ha-homework-todo">Homework to-do entity</label>
          <input id="ha-homework-todo" value="todo.lectio_homework" required>
        </div>
        <div>
          <label for="ha-cancellations-sensor">Cancellations sensor entity</label>
          <input id="ha-cancellations-sensor" value="sensor.lectio_cancellations" required>
        </div>
      </div>
      <p>Create a long-lived access token from your Home Assistant user profile under <strong>Security</strong>. Home Assistant shows it once. This page keeps its input blank after saving; the token is stored in a dedicated configuration volume and is never included in status or diagnostics.</p>
      <button type="submit">Save display settings</button>
      <button type="button" onclick="disconnectHomeAssistant()">Disconnect display</button>
    </form>
    <form id="gateway-api-url-form" onsubmit="saveGatewayApiUrl(event)">
      <h3>Lectio API address for Home Assistant</h3>
      <p id="api-binding-warning" class="setup-note" role="status">Checking the server's API network binding…</p>
      <label for="gateway-api-url">URL that Home Assistant can reach</label>
      <input id="gateway-api-url" type="url" placeholder="http://192.168.1.20:8002" required>
      <p>Use the server's reserved LAN address and port. This page cannot change Docker's host binding or firewall; keep access limited to your Home Assistant host.</p>
      <button type="submit">Save API address</button>
      <p id="gateway-api-url-status" role="status"></p>
    </form>
    <section id="hacs-setup-instructions" aria-labelledby="hacs-setup-heading">
      <h3 id="hacs-setup-heading">Finish in Home Assistant</h3>
      <ol>
        <li>Install <strong>Better Lectio</strong> through HACS. Add <code>MarcusFunt/Better-Lectio-HA-Display</code> as a custom repository with category <strong>Integration</strong>, install it, then restart Home Assistant. See the <a href="https://www.hacs.xyz/docs/faq/custom_repositories/" target="_blank" rel="noreferrer">HACS custom repository guide</a>.</li>
        <li>Generate a scoped API token below. Copy it now; this page cannot show it again. Rotation invalidates the previous token immediately.</li>
        <li>In Home Assistant, add the <strong>Better Lectio</strong> integration using the saved API URL and one-time token. To rotate credentials later, open the existing integration, choose <strong>Reconfigure</strong>, and enter the new token; its entities stay attached.</li>
        <li>Create or select any private calendars in Home Assistant, then enter their entity IDs in the display settings above.</li>
      </ol>
      <p>The generated API token is separate from the Home Assistant long-lived token and permits read-only Lectio data access. Do not reuse the Home Assistant token in the integration form.</p>
      <button id="rotate-gateway-api-token" type="button" onclick="generateGatewayApiToken()">Generate or rotate API token</button>
      <div id="gateway-api-token-result" hidden>
        <p class="setup-note">Copy this token into the Better Lectio integration now. The raw value is shown once. For an existing entry, use its Reconfigure action.</p>
        <label for="gateway-api-token">One-time API token</label>
        <input id="gateway-api-token" type="password" readonly autocomplete="off">
        <button type="button" onclick="copyGatewayApiToken()">Copy token</button>
        <span id="gateway-api-token-copy-status" aria-live="polite"></span>
      </div>
    </section>
  </section>
  <pre id="status">Loading status…</pre>
  <section class="diagnostics" aria-labelledby="diagnostics-heading">
    <h2 id="diagnostics-heading">System diagnostics</h2>
    <p id="diagnostics-message" role="status">Loading diagnostics…</p>
    <div class="diagnostics-grid">
      <div>
        <h3>Lectio sources</h3>
        <table>
          <thead><tr><th>Source</th><th>State</th><th>Last successful sync</th></tr></thead>
          <tbody id="source-diagnostics"></tbody>
        </table>
      </div>
      <div>
        <h3>Display service → Home Assistant</h3>
        <p id="home-assistant-setup-info">Waiting for connection diagnostics…</p>
      </div>
      <div>
        <h3>Newest display bitmap</h3>
        <p id="display-info">Waiting for display diagnostics…</p>
        <p id="display-hash"></p>
        <img id="display-preview" alt="Newest rendered 800 by 480 display bitmap" hidden>
      </div>
    </div>
  </section>
  <iframe id="browser-view" title="Temporary Lectio browser" src="about:blank" data-view-url="{view_url}" allow="clipboard-read; clipboard-write"></iframe>
  <script>
    async function refresh() {{
      const response = await fetch('/auth/status', {{ cache: 'no-store' }});
      const status = await response.json();
      document.getElementById('status').textContent = JSON.stringify(status, null, 2);
      const browserView = document.getElementById('browser-view');
      if (status.state === 'WAITING_FOR_USER') {{
        if (browserView.src === 'about:blank') browserView.src = browserView.dataset.viewUrl;
      }} else if (browserView.src !== 'about:blank') {{
        browserView.src = 'about:blank';
      }}
    }}
    async function action(path) {{
      const response = await fetch(path, {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }} }});
      if (!response.ok) document.getElementById('status').textContent = await response.text();
      await refresh();
      await refreshDiagnostics();
    }}
    async function configureStudentId(event) {{
      event.preventDefault();
      const input = document.getElementById('student-id');
      const result = document.getElementById('student-id-result');
      result.textContent = 'Checking with Lectio…';
      try {{
        const response = await fetch('/auth/student-id', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ student_id: input.value }})
        }});
        const payload = await response.json();
        result.textContent = response.ok
          ? 'Student ID saved; Lectio accepted the schedule request.'
          : payload.detail || 'The student ID could not be verified.';
      }} catch {{
        result.textContent = 'Lectio could not be reached. Try again later.';
      }} finally {{
        input.value = '';
        await refresh();
        await refreshDiagnostics();
      }}
    }}
    let haDisplayConfigured = false;
    async function loadHomeAssistantSetup() {{
      const status = document.getElementById('ha-setup-status');
      try {{
        const response = await fetch('/auth/home-assistant/setup', {{ cache: 'no-store' }});
        if (!response.ok) throw new Error('Setup status unavailable');
        const setup = await response.json();
        haDisplayConfigured = setup.display_configured === true;
        document.getElementById('ha-url').value = setup.ha_url || '';
        document.getElementById('ha-token').value = '';
        const entities = setup.entities || {{}};
        document.getElementById('ha-lectio-calendar').value = entities.lectio_calendar || 'calendar.lectio';
        document.getElementById('ha-private-calendars').value = (entities.private_calendars || []).join(', ');
        document.getElementById('ha-assignments-todo').value = entities.assignments_todo || 'todo.lectio_assignments';
        document.getElementById('ha-homework-todo').value = entities.homework_todo || 'todo.lectio_homework';
        document.getElementById('ha-cancellations-sensor').value = entities.cancellations_sensor || 'sensor.lectio_cancellations';
        document.getElementById('gateway-api-url').value = setup.gateway_api_url || '';
        if (!setup.display_config_valid) {{
          status.textContent = 'Saved display settings could not be read. Check the configuration volume before saving new values.';
        }} else if (haDisplayConfigured) {{
          status.textContent = 'Display settings are saved · Home Assistant state: ' + (setup.display_state || 'unknown');
        }} else {{
          status.textContent = 'No managed display settings are saved · display state: ' + (setup.display_state || 'unknown') + '. Save a URL and token below to configure the connection here.';
        }}
        const warning = document.getElementById('api-binding-warning');
        if (setup.api_bind_scope === 'loopback') {{
          warning.textContent = 'The API currently binds to ' + setup.api_bind_address + '. Home Assistant on another host cannot reach this loopback address. Set LECTIO_HA_API_BIND_ADDRESS in the Compose host .env to the server LAN address, then apply it with docker compose up -d lectio-ha-api.';
        }} else if (setup.api_bind_scope === 'wildcard') {{
          warning.textContent = 'The API binds to all host interfaces. Enter the server LAN address below and restrict port ' + setup.api_host_port + ' with the host firewall to Home Assistant.';
        }} else {{
          warning.textContent = 'API listener: ' + setup.api_bind_address + ':' + setup.api_host_port + '. Confirm the host firewall allows Home Assistant to reach this reserved LAN address.';
        }}
      }} catch {{
        status.textContent = 'Home Assistant setup status is temporarily unavailable.';
      }}
    }}
    async function configureHomeAssistant(event) {{
      event.preventDefault();
      const status = document.getElementById('ha-setup-status');
      const tokenInput = document.getElementById('ha-token');
      const token = tokenInput.value;
      if (!haDisplayConfigured && !token) {{
        status.textContent = 'Enter a Home Assistant long-lived access token for initial setup.';
        tokenInput.focus();
        return;
      }}
      const entities = {{
        lectio_calendar: document.getElementById('ha-lectio-calendar').value.trim(),
        private_calendars: document.getElementById('ha-private-calendars').value.split(',').map(value => value.trim()).filter(Boolean),
        assignments_todo: document.getElementById('ha-assignments-todo').value.trim(),
        homework_todo: document.getElementById('ha-homework-todo').value.trim(),
        cancellations_sensor: document.getElementById('ha-cancellations-sensor').value.trim()
      }};
      try {{
        const response = await fetch('/auth/home-assistant/display-settings', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ ha_url: document.getElementById('ha-url').value.trim(), ha_token: token, entities }})
        }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || 'Settings could not be saved.');
        tokenInput.value = '';
        haDisplayConfigured = true;
        status.textContent = 'Display settings saved. The display service applies changes within 30 seconds.';
        await loadHomeAssistantSetup();
        await refreshDiagnostics();
      }} catch (error) {{
        status.textContent = error.message || 'Settings could not be saved.';
      }}
    }}
    async function disconnectHomeAssistant() {{
      if (!window.confirm('Remove the saved display connection and its Home Assistant token?')) return;
      const status = document.getElementById('ha-setup-status');
      try {{
        const response = await fetch('/auth/home-assistant/disconnect', {{ method: 'POST' }});
        if (!response.ok) throw new Error('Display connection could not be removed.');
        document.getElementById('ha-token').value = '';
        haDisplayConfigured = false;
        status.textContent = 'Display connection removed. The last rendered bitmap remains available until a new one is published.';
        await loadHomeAssistantSetup();
        await refreshDiagnostics();
      }} catch (error) {{
        status.textContent = error.message || 'Display connection could not be removed.';
      }}
    }}
    async function saveGatewayApiUrl(event) {{
      event.preventDefault();
      const status = document.getElementById('gateway-api-url-status');
      try {{
        const response = await fetch('/auth/home-assistant/gateway-api-url', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ url: document.getElementById('gateway-api-url').value.trim() }})
        }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || 'API address could not be saved.');
        document.getElementById('gateway-api-url').value = payload.url;
        status.textContent = 'API address saved. Use this URL in the Better Lectio integration in Home Assistant.';
      }} catch (error) {{
        status.textContent = error.message || 'API address could not be saved.';
      }}
    }}
    async function generateGatewayApiToken() {{
      if (!window.confirm('Generate a new API token? Any older managed or environment token stops working immediately. Reconfigure the existing Better Lectio entry in Home Assistant with the new token; its entities will stay attached.')) return;
      const result = document.getElementById('gateway-api-token-result');
      const input = document.getElementById('gateway-api-token');
      const copyStatus = document.getElementById('gateway-api-token-copy-status');
      input.value = '';
      input.type = 'password';
      result.hidden = true;
      copyStatus.textContent = '';
      try {{
        const response = await fetch('/auth/home-assistant/gateway-api-token', {{ method: 'POST' }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || 'API token could not be generated.');
        input.value = payload.token;
        input.type = 'text';
        result.hidden = false;
      }} catch (error) {{
        copyStatus.textContent = error.message || 'API token could not be generated.';
      }}
    }}
    async function copyGatewayApiToken() {{
      const input = document.getElementById('gateway-api-token');
      const copyStatus = document.getElementById('gateway-api-token-copy-status');
      try {{
        await navigator.clipboard.writeText(input.value);
        copyStatus.textContent = 'Copied.';
      }} catch {{
        input.focus();
        input.select();
        copyStatus.textContent = document.execCommand('copy') ? 'Copied.' : 'Select and copy the token manually.';
      }}
    }}
    async function refreshDiagnostics() {{
      const message = document.getElementById('diagnostics-message');
      try {{
        const response = await fetch('/auth/diagnostics', {{ cache: 'no-store' }});
        if (!response.ok) throw new Error('Diagnostics unavailable');
        const diagnostics = await response.json();
        const auth = diagnostics.auth || {{}};
        message.textContent = 'Authentication: ' + (auth.state || 'unknown')
          + ' · student ID ' + (auth.student_id_available ? 'configured' : 'not configured');

        const sourceNames = {{
          schedule: 'Schedule',
          assignments: 'Assignments',
          homework: 'Homework',
          cancellations: 'Cancellations'
        }};
        const rows = document.getElementById('source-diagnostics');
        rows.replaceChildren();
        for (const name of Object.keys(sourceNames)) {{
          const source = (diagnostics.sources || {{}})[name] || {{}};
          const row = document.createElement('tr');
          const label = document.createElement('th');
          const state = document.createElement('td');
          const lastSuccess = document.createElement('td');
          label.scope = 'row';
          label.textContent = sourceNames[name];
          state.textContent = (source.state || 'unknown') + (source.is_stale ? ' (stale)' : '');
          lastSuccess.textContent = source.last_successful_sync
            ? new Date(source.last_successful_sync).toLocaleString()
            : 'Never';
          row.append(label, state, lastSuccess);
          rows.append(row);
        }}

        const display = diagnostics.display || {{}};
        const setup = diagnostics.home_assistant_setup || {{}};
        const setupLabels = {{
          not_configured: 'Not configured',
          invalid_configuration: 'Check the Home Assistant URL and token settings',
          checking: 'Checking Home Assistant connection',
          connected: 'Connected',
          unauthorized: 'Home Assistant rejected the token',
          unreachable: 'Home Assistant host could not be reached',
          entity_problem: 'Home Assistant is reachable, but an entity is missing or unavailable',
          partial_error: 'Some Home Assistant data could not be read',
          unavailable: 'Connection diagnostics are unavailable'
        }};
        const setupInfo = document.getElementById('home-assistant-setup-info');
        setupInfo.textContent = setupLabels[setup.state] || setupLabels.unavailable;
        if (setup.last_checked_at) {{
          setupInfo.textContent += ' · checked ' + new Date(setup.last_checked_at).toLocaleString();
        }}
        const info = document.getElementById('display-info');
        const hash = document.getElementById('display-hash');
        const preview = document.getElementById('display-preview');
        if (display.available && display.image_url && display.content_hash) {{
          info.textContent = 'Rendered ' + new Date(display.generated_at).toLocaleString();
          hash.textContent = 'SHA-256: ' + display.content_hash;
          preview.hidden = false;
          if (preview.dataset.hash !== display.content_hash) {{
            preview.src = display.image_url + '?v=' + encodeURIComponent(display.content_hash);
            preview.dataset.hash = display.content_hash;
          }}
        }} else {{
          info.textContent = display.state === 'not_rendered'
            ? 'No display bitmap has been rendered yet.'
            : 'Display diagnostics are unavailable.';
          hash.textContent = '';
          preview.removeAttribute('src');
          preview.dataset.hash = '';
          preview.hidden = true;
        }}
      }} catch {{
        message.textContent = 'System diagnostics are temporarily unavailable.';
      }}
    }}
    refresh();
    refreshDiagnostics();
    loadHomeAssistantSetup();
    setInterval(refresh, 2000);
    setInterval(refreshDiagnostics, 10000);
  </script>
</body>
</html>"""
    return HTMLResponse(
        page,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
