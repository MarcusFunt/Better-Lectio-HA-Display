import html
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

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
async def auth_diagnostics(request: Request) -> dict[str, bool]:
    session = _manager(request).session
    return {
        "student_id_available": (
            session is not None and session.student_id is not None
        )
    }


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
      }}
    }}
    refresh();
    setInterval(refresh, 2000);
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
