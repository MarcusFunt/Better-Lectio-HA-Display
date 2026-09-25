import asyncio
from urllib.parse import urlparse

import lectio_auth_browser.control as control_module
from lectio_auth_browser.control import BrowserControl

LECTIO_COOKIES = [
    {
        "name": "LastLoginExamno",
        "value": "123",
        "domain": "lectio.dk",
        "path": "/",
        "secure": True,
        "expires": -1,
    },
    {
        "name": "LastLoginElevId",
        "value": "456",
        "domain": "lectio.dk",
        "path": "/",
        "secure": True,
        "expires": -1,
    },
    {
        "name": "ASP.NET_SessionId",
        "value": "synthetic-session",
        "domain": "lectio.dk",
        "path": "/",
        "secure": True,
        "expires": -1,
    },
    {
        "name": "tracking",
        "value": "synthetic-external-value",
        "domain": "analytics.example",
        "path": "/",
        "secure": True,
        "expires": -1,
    },
]


class FakeContext:
    async def cookies(self, *urls):
        if not urls:
            return LECTIO_COOKIES

        host = urlparse(urls[0]).hostname
        return [
            cookie
            for cookie in LECTIO_COOKIES
            if cookie["domain"].startswith(".")
            and host is not None
            and host.endswith(cookie["domain"])
            or cookie["domain"] == host
        ]

    async def new_page(self):
        return FakePage()

    async def close(self):
        return None


class FakePage:
    async def goto(self, url, *, wait_until, timeout):
        return None


class FakeBrowser:
    def __init__(self, context):
        self._context = context

    async def new_context(self, *, viewport):
        return self._context

    async def close(self):
        return None


class FakePlaywright:
    def __init__(self, context):
        self.chromium = self
        self._context = context

    async def launch(self, *, headless):
        return FakeBrowser(self._context)

    async def stop(self):
        return None


class FakePlaywrightManager:
    def __init__(self, context):
        self._playwright = FakePlaywright(context)

    async def start(self):
        return self._playwright


def test_browser_captures_host_only_lectio_identity_cookies(monkeypatch):
    async def run_login():
        context = FakeContext()
        monkeypatch.setattr(
            control_module,
            "async_playwright",
            lambda: FakePlaywrightManager(context),
        )
        browser_control = BrowserControl()
        browser_control._complete_event = asyncio.Event()
        browser_control._complete_event.set()

        await browser_control._run("https://www.lectio.dk/", timeout_seconds=0.1)
        snapshot = await browser_control.snapshot()

        assert snapshot["state"] == "idle"
        assert snapshot["session"] is not None
        assert snapshot["session"]["school_id"] == "123"
        assert snapshot["session"]["student_id"] == "456"
        assert all(
            cookie["domain"].lower().lstrip(".").endswith("lectio.dk")
            for cookie in snapshot["session"]["cookies"]
        )

    asyncio.run(run_login())
