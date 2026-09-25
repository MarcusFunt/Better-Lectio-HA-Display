import asyncio
from urllib.parse import urlparse

import lectio_auth_browser.control as control_module
from httpx import ASGITransport, AsyncClient
from lectio_auth_browser.control import BrowserControl
from lectio_auth_browser.main import app

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
    def __init__(self, cookies=LECTIO_COOKIES):
        self._cookies = cookies

    async def cookies(self, *urls):
        if not urls:
            return self._cookies

        host = urlparse(urls[0]).hostname
        return [
            cookie
            for cookie in self._cookies
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
    def __init__(self, url="about:blank"):
        self.url = url

    async def goto(self, url, *, wait_until, timeout):
        self.url = url


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


def test_candidate_uses_student_schedule_url_when_identity_cookies_are_absent():
    candidate = BrowserControl._make_candidate(
        [LECTIO_COOKIES[2]],
        "https://www.lectio.dk/lectio/681/SkemaNy.aspx?type=elev&elevid=24680",
    )

    assert candidate is not None
    assert candidate["school_id"] == "681"
    assert candidate["student_id"] == "24680"
    assert [cookie["name"] for cookie in candidate["cookies"]] == [
        "ASP.NET_SessionId"
    ]


def test_candidate_accepts_the_authenticated_default_schedule_without_student_id():
    candidate = BrowserControl._make_candidate(
        [LECTIO_COOKIES[2]],
        "https://www.lectio.dk/lectio/681/SkemaNy.aspx",
    )

    assert candidate is not None
    assert candidate["school_id"] == "681"
    assert candidate["student_id"] is None


def test_candidate_rejects_untrusted_or_non_student_schedule_urls():
    urls = [
        "https://accounts.example/lectio/681/SkemaNy.aspx?type=elev&elevid=24680",
        "https://www.lectio.dk/lectio/681/SkemaNy.aspx?type=laerer&laererid=24680",
        "https://www.lectio.dk/lectio/681/other.aspx?type=elev&elevid=24680",
    ]

    for url in urls:
        assert BrowserControl._make_candidate([LECTIO_COOKIES[2]], url) is None


def test_cookie_diagnostics_redacts_values_and_reports_identity_formats():
    async def request_diagnostics():
        cookies = [
            {
                "name": "LastLoginExamno",
                "value": "123",
                "domain": "lectio.dk",
            },
            {
                "name": "LastLoginElevId",
                "value": "student-secret-value",
                "domain": ".lectio.dk",
            },
            {
                "name": "ASP.NET_SessionId",
                "value": "session-secret-value",
                "domain": "lectio.dk",
            },
            {
                "name": "external",
                "value": "external-value",
                "domain": "analytics.example",
            },
        ]
        browser_control = BrowserControl()
        browser_control.state = "waiting_for_user"
        browser_control._context = FakeContext(cookies)
        browser_control._page = FakePage(
            "https://www.lectio.dk/lectio/681/SkemaNy.aspx?type=elev&elevid=321"
        )
        app.state.browser_control = browser_control

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/session/diagnostics")

        assert response.status_code == 200
        assert response.json() == {
            "state": "waiting_for_user",
            "available": True,
            "lectio_cookie_count": 3,
            "school_id_cookie": "numeric",
            "student_id_cookie": "non_numeric",
            "school_id_in_page_url": "numeric",
            "student_id_in_page_url": "numeric",
            "default_schedule_url": False,
            "candidate_available": True,
        }
        assert "student-secret-value" not in response.text
        assert "session-secret-value" not in response.text
        assert "external-value" not in response.text

    asyncio.run(request_diagnostics())


def test_cookie_diagnostics_distinguishes_missing_identity_cookies():
    async def request_diagnostics():
        browser_control = BrowserControl()
        browser_control.state = "waiting_for_user"
        browser_control._context = FakeContext(
            [
                {
                    "name": "ASP.NET_SessionId",
                    "value": "session-secret-value",
                    "domain": "lectio.dk",
                }
            ]
        )
        app.state.browser_control = browser_control

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/session/diagnostics")

        assert response.status_code == 200
        assert response.json() == {
            "state": "waiting_for_user",
            "available": True,
            "lectio_cookie_count": 1,
            "school_id_cookie": "missing",
            "student_id_cookie": "missing",
            "school_id_in_page_url": "missing",
            "student_id_in_page_url": "missing",
            "default_schedule_url": False,
            "candidate_available": False,
        }
        assert "session-secret-value" not in response.text

    asyncio.run(request_diagnostics())


def test_cookie_diagnostics_reports_default_schedule_without_student_id():
    async def request_diagnostics():
        browser_control = BrowserControl()
        browser_control.state = "waiting_for_user"
        browser_control._context = FakeContext([LECTIO_COOKIES[2]])
        browser_control._page = FakePage(
            "https://www.lectio.dk/lectio/681/SkemaNy.aspx"
        )
        app.state.browser_control = browser_control

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/session/diagnostics")

        assert response.status_code == 200
        assert response.json() == {
            "state": "waiting_for_user",
            "available": True,
            "lectio_cookie_count": 1,
            "school_id_cookie": "missing",
            "student_id_cookie": "missing",
            "school_id_in_page_url": "numeric",
            "student_id_in_page_url": "missing",
            "default_schedule_url": True,
            "candidate_available": True,
        }

    asyncio.run(request_diagnostics())


def test_cookie_diagnostics_reports_unavailable_without_browser_context():
    async def request_diagnostics():
        browser_control = BrowserControl()
        app.state.browser_control = browser_control

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/session/diagnostics")

        assert response.status_code == 200
        assert response.json() == {
            "state": "idle",
            "available": False,
            "lectio_cookie_count": None,
            "school_id_cookie": "unavailable",
            "student_id_cookie": "unavailable",
            "school_id_in_page_url": "unavailable",
            "student_id_in_page_url": "unavailable",
            "default_schedule_url": False,
            "candidate_available": False,
        }

    asyncio.run(request_diagnostics())
