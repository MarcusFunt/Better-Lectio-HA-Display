import html
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from lectio_gateway.auth.manager import AuthFlowInProgress, AuthManager


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
    try:
        yield
    finally:
        await manager.shutdown()


app = FastAPI(title="Better Lectio Gateway", lifespan=lifespan)


def _manager(request: Request) -> AuthManager:
    return request.app.state.auth_manager


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
    return _manager(request).status().model_dump(mode="json")


@app.post("/auth/start", status_code=status.HTTP_202_ACCEPTED)
async def auth_start(request: Request) -> dict[str, object]:
    _check_same_host(request)
    try:
        current = await _manager(request).start()
    except AuthFlowInProgress as exc:
        raise HTTPException(status_code=409, detail="Authentication is already in progress") from exc
    return current.model_dump(mode="json")


@app.post("/auth/cancel")
async def auth_cancel(request: Request) -> dict[str, object]:
    _check_same_host(request)
    current = await _manager(request).cancel()
    return current.model_dump(mode="json")


@app.post("/auth/logout")
async def auth_logout(request: Request) -> dict[str, object]:
    _check_same_host(request)
    current = await _manager(request).logout()
    return current.model_dump(mode="json")


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
