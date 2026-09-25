import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

_LOGGER = logging.getLogger(__name__)
_LECTIO_ORIGIN = "https://www.lectio.dk"
_IDENTITY_COOKIE_NAMES = {
    "lastloginexamno": "school_id",
    "lastloginelevid": "student_id",
}
_NUMERIC_ID = re.compile(r"^\d+$")


class BrowserControl:
    """Runs one isolated, non-persistent browser context at a time."""

    def __init__(self) -> None:
        self.state = "idle"
        self._candidate: dict[str, object] | None = None
        self._task: asyncio.Task[None] | None = None
        self._complete_event: asyncio.Event | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._browser: Browser | None = None
        self._playwright: Playwright | None = None
        self._lock = asyncio.Lock()

    async def start(self, login_url: str, timeout_seconds: int) -> None:
        parsed = urlparse(login_url)
        if parsed.scheme != "https" or parsed.hostname != "www.lectio.dk":
            raise ValueError("The login URL must be the Lectio homepage")
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise RuntimeError("A browser login is already active")
            self._candidate = None
            self._complete_event = asyncio.Event()
            self.state = "starting"
            self._task = asyncio.create_task(self._run(login_url, timeout_seconds))

    async def snapshot(self) -> dict[str, object]:
        return {"state": self.state, "session": self._candidate}

    async def diagnostics(self) -> dict[str, object]:
        context = self._context
        if context is None:
            return {
                "state": self.state,
                "available": False,
                "lectio_cookie_count": None,
                "school_id_cookie": "unavailable",
                "student_id_cookie": "unavailable",
                "school_id_in_page_url": "unavailable",
                "student_id_in_page_url": "unavailable",
                "candidate_available": False,
            }

        cookies = await context.cookies()
        page_url = self._page.url if self._page is not None else None
        _, page_identity_states = self._identity_from_page_url(page_url)
        identity_cookie_states = {
            "school_id": "missing",
            "student_id": "missing",
        }
        lectio_cookie_count = 0
        for cookie in cookies:
            domain = str(cookie.get("domain") or "").lower().lstrip(".")
            if domain != "lectio.dk" and not domain.endswith(".lectio.dk"):
                continue

            lectio_cookie_count += 1
            name = str(cookie.get("name") or "")
            identity_field = _IDENTITY_COOKIE_NAMES.get(name.casefold())
            if identity_field is None:
                continue

            value = str(cookie.get("value") or "")
            if _NUMERIC_ID.fullmatch(value):
                identity_cookie_states[identity_field] = "numeric"
            elif identity_cookie_states[identity_field] == "missing":
                identity_cookie_states[identity_field] = "non_numeric"

        return {
            "state": self.state,
            "available": True,
            "lectio_cookie_count": lectio_cookie_count,
            "school_id_cookie": identity_cookie_states["school_id"],
            "student_id_cookie": identity_cookie_states["student_id"],
            "school_id_in_page_url": page_identity_states["school_id"],
            "student_id_in_page_url": page_identity_states["student_id"],
            "candidate_available": self._make_candidate(cookies, page_url) is not None,
        }

    async def complete(self) -> None:
        if self.state != "candidate" or self._complete_event is None:
            raise RuntimeError("There is no validated browser session to complete")
        self._complete_event.set()
        task = self._task
        if task is not None:
            await task

    async def stop(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._close_browser()
        self._task = None
        self._candidate = None
        self.state = "idle"

    async def _run(self, login_url: str, timeout_seconds: int) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=False)
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800}
            )
            page = await self._context.new_page()
            self._page = page
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
            self.state = "waiting_for_user"
            while asyncio.get_running_loop().time() < deadline:
                cookies = await self._context.cookies()
                candidate = self._make_candidate(cookies, page.url)
                if candidate is not None:
                    if self._candidate is None or any(
                        candidate[key] != self._candidate.get(key)
                        for key in ("school_id", "student_id", "cookies")
                    ):
                        candidate["created_at"] = datetime.now(timezone.utc).isoformat()
                        self._candidate = candidate
                    self.state = "candidate"
                    if self._complete_event is None:
                        return
                    try:
                        await asyncio.wait_for(self._complete_event.wait(), timeout=1)
                    except TimeoutError:
                        continue
                    self.state = "idle"
                    return
                self.state = "waiting_for_user"
                await asyncio.sleep(1)
            self.state = "expired"
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("The temporary Lectio browser failed")
            self.state = "error"
        finally:
            await self._close_browser()
            if self._task is asyncio.current_task():
                self._task = None

    @staticmethod
    def _make_candidate(
        cookies: list[dict[str, object]], page_url: str | None = None
    ) -> dict[str, object] | None:
        lectio_cookies: list[dict[str, object]] = []
        identities: dict[str, str] = {}
        for cookie in cookies:
            domain = str(cookie.get("domain") or "").lower().lstrip(".")
            if domain != "lectio.dk" and not domain.endswith(".lectio.dk"):
                continue
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            identity_field = _IDENTITY_COOKIE_NAMES.get(name.casefold())
            if identity_field and _NUMERIC_ID.fullmatch(value):
                identities[identity_field] = value
            expires = cookie.get("expires")
            lectio_cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": str(cookie.get("domain")),
                    "path": str(cookie.get("path") or "/"),
                    "secure": bool(cookie.get("secure", True)),
                    "expires": float(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
                }
            )
        school_id = identities.get("school_id")
        student_id = identities.get("student_id")
        page_identities, _ = BrowserControl._identity_from_page_url(page_url)
        school_id = school_id or page_identities.get("school_id")
        student_id = student_id or page_identities.get("student_id")
        if not school_id or not student_id or not lectio_cookies:
            return None
        return {
            "school_id": school_id,
            "student_id": student_id,
            "cookies": lectio_cookies,
        }

    @staticmethod
    def _identity_from_page_url(
        page_url: str | None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        identities: dict[str, str] = {}
        states = {"school_id": "missing", "student_id": "missing"}
        if not page_url:
            return identities, states

        parsed = urlparse(page_url)
        if parsed.scheme != "https" or parsed.hostname != "www.lectio.dk":
            return identities, states

        path_parts = parsed.path.split("/")
        if len(path_parts) > 2 and path_parts[1].casefold() == "lectio":
            school_id = path_parts[2]
            states["school_id"] = (
                "numeric" if _NUMERIC_ID.fullmatch(school_id) else "non_numeric"
            )
            if states["school_id"] == "numeric":
                identities["school_id"] = school_id

        if parsed.path.casefold().endswith("/skemany.aspx"):
            query = parse_qs(parsed.query, keep_blank_values=True)
            types = query.get("type", [])
            student_ids = query.get("elevid", [])
            if types and types[0].casefold() == "elev" and student_ids:
                student_id = student_ids[0]
                states["student_id"] = (
                    "numeric" if _NUMERIC_ID.fullmatch(student_id) else "non_numeric"
                )
                if states["student_id"] == "numeric":
                    identities["student_id"] = student_id
        return identities, states

    async def _close_browser(self) -> None:
        self._page = None
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
