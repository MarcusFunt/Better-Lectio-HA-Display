import html
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

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
from lectio_gateway.lectio.models import (
    LectioAssignment,
    LectioCancellation,
    LectioHomework,
    LectioLesson,
    LectioSourceResponse,
)

_DISPLAY_CONTENT_HASH = re.compile(r"^[a-f0-9]{64}$")


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
    data_service = LectioDataService(
        session_provider=lambda: manager.session,
        cache_path=Path(os.getenv("LECTIO_DATA_DIR", "/var/lib/better-lectio"))
        / "lectio-data-cache.json",
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


def _status_payload(current: AuthStatus) -> dict[str, object]:
    payload = current.model_dump(mode="json", exclude={"student_id"})
    payload["student_id_available"] = current.student_id is not None
    return payload


def _check_same_host(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc.casefold() != request.headers.get("host", "").casefold():
        raise HTTPException(status_code=403, detail="Cross-origin action rejected")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "lectio-gateway"}


@app.get("/", include_in_schema=False)
async def home() -> RedirectResponse:
    return RedirectResponse(url="/auth/browser")


@app.get("/auth/status")
async def auth_status(request: Request) -> dict[str, object]:
    return _status_payload(_manager(request).status())


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
    iframe {{ border: 1px solid #777; width: 100%; height: min(70vh, 800px); }}
    pre {{ background: #f1f1f1; padding: 1rem; white-space: pre-wrap; }}
    .diagnostics {{ margin: 1.5rem 0; padding: 1rem; border: 1px solid #bbb; border-radius: .5rem; }}
    .diagnostics-grid {{ display: grid; grid-template-columns: minmax(16rem, 1fr) minmax(20rem, 1.4fr); gap: 1.5rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #ddd; padding: .45rem; text-align: left; vertical-align: top; }}
    #display-preview {{ border: 1px solid #777; display: block; height: auto; image-rendering: pixelated; max-width: 100%; }}
    #display-hash {{ overflow-wrap: anywhere; }}
    @media (max-width: 720px) {{ .diagnostics-grid {{ grid-template-columns: 1fr; }} }}
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
