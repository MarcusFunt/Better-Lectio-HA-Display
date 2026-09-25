import asyncio
import logging

import httpx
from lectio_gateway.auth.browser_client import AuthBrowserClient


def test_start_opens_the_lifecycle_container_before_starting_login(monkeypatch):
    requests = []

    def handle(request):
        requests.append((request.method, str(request.url)))
        return httpx.Response(200, json={"status": "ok"})

    class Client(httpx.AsyncClient):
        def __init__(self, *, timeout):
            super().__init__(timeout=timeout, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    browser = AuthBrowserClient(
        "http://lectio-auth-browser:8765",
        "https://www.lectio.dk/",
        900,
        lifecycle_url="http://lectio-auth-lifecycle:8766",
    )

    asyncio.run(browser.start())

    assert requests == [
        ("POST", "http://lectio-auth-lifecycle:8766/browser/start"),
        ("GET", "http://lectio-auth-browser:8765/health"),
        ("POST", "http://lectio-auth-browser:8765/session/start"),
    ]


def test_stop_stops_the_browser_then_removes_its_container(monkeypatch):
    requests = []
    lifecycle_stop_attempts = 0

    def handle(request):
        nonlocal lifecycle_stop_attempts
        requests.append((request.method, str(request.url)))
        if request.url.path == "/browser/stop":
            lifecycle_stop_attempts += 1
            if lifecycle_stop_attempts == 1:
                return httpx.Response(503)
        return httpx.Response(200, json={"state": "stopped"})

    class Client(httpx.AsyncClient):
        def __init__(self, *, timeout):
            super().__init__(timeout=timeout, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    browser = AuthBrowserClient(
        "http://lectio-auth-browser:8765",
        "https://www.lectio.dk/",
        900,
        lifecycle_url="http://lectio-auth-lifecycle:8766",
    )

    asyncio.run(browser.stop())

    assert requests == [
        ("POST", "http://lectio-auth-browser:8765/session/stop"),
        ("POST", "http://lectio-auth-lifecycle:8766/browser/stop"),
        ("POST", "http://lectio-auth-lifecycle:8766/browser/stop"),
    ]


def test_persistent_lifecycle_cleanup_failure_is_logged(monkeypatch, caplog):
    requests = []

    def handle(request):
        requests.append(str(request.url))
        if request.url.path == "/browser/stop":
            return httpx.Response(503)
        return httpx.Response(200, json={"state": "stopped"})

    class Client(httpx.AsyncClient):
        def __init__(self, *, timeout):
            super().__init__(timeout=timeout, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    browser = AuthBrowserClient(
        "http://lectio-auth-browser:8765",
        "https://www.lectio.dk/",
        900,
        lifecycle_url="http://lectio-auth-lifecycle:8766",
    )

    with caplog.at_level(logging.ERROR, logger="lectio_gateway.auth.browser_client"):
        asyncio.run(browser.stop())

    assert requests.count("http://lectio-auth-lifecycle:8766/browser/stop") == 2
    assert "Could not stop the temporary auth-browser after two attempts" in caplog.text
