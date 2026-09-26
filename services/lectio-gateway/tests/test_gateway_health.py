import asyncio
from datetime import datetime, timezone
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from lectio_gateway.auth.manager import AuthManager, AuthState
from lectio_gateway.lectio.client import LectioClient
from lectio_gateway.lectio.models import (
    AuthenticatedLectioSession,
    LectioCookie,
    LectioSyncStatus,
)
from lectio_gateway.main import DisplayDiagnosticsClient, app, httpx


def test_health_endpoint_reports_gateway_identity():
    async def request_health():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/health")

    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "lectio-gateway"}


def test_home_assistant_setup_diagnostics_client_allowlists_sidecar_payload(
    monkeypatch,
):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "state": "connected",
                "last_checked_at": "2026-09-26T08:30:00+00:00",
                "token": "private-token",
                "url": "http://private-host:8123",
            }

    class FakeHttpClient:
        def __init__(self, *, timeout):
            assert timeout == 2.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            assert url == "http://display-diagnostics:8001/diagnostics/setup"
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeHttpClient)

    result = asyncio.run(
        DisplayDiagnosticsClient("http://display-diagnostics:8001").setup()
    )

    assert result == {
        "state": "connected",
        "last_checked_at": "2026-09-26T08:30:00+00:00",
    }


def test_auth_diagnostics_reports_safe_runtime_and_bitmap_details_without_secrets(
    tmp_path,
):
    async def request_diagnostics():
        manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://localhost:6080/vnc.html",
            login_url="https://www.lectio.dk/",
        )
        manager.session = AuthenticatedLectioSession(
            school_id="123",
            student_id="456789",
            cookies=[
                LectioCookie(
                    name="ASP.NET_SessionId",
                    value="cookie-value-secret",
                    domain="www.lectio.dk",
                )
            ],
        )
        manager._set_state(AuthState.AUTHENTICATED)
        app.state.auth_manager = manager
        app.state.lectio_data_service = FakeDiagnosticsDataService()
        app.state.display_diagnostics_client = FakeDisplayDiagnosticsClient(
            {
                "available": True,
                "content_hash": "a" * 64,
                "generated_at": "2026-09-25T06:00:00+00:00",
            }
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/auth/diagnostics")

    response = asyncio.run(request_diagnostics())

    assert response.status_code == 200
    payload = response.json()
    assert payload["auth"] == {
        "state": "AUTHENTICATED",
        "student_id_available": True,
    }
    assert payload["sources"]["schedule"] == {
        "state": "valid",
        "is_stale": False,
        "last_attempt_at": "2026-09-25T05:29:00Z",
        "last_successful_sync": "2026-09-25T05:30:00Z",
    }
    assert payload["display"] == {
        "available": True,
        "content_hash": "a" * 64,
        "generated_at": "2026-09-25T06:00:00+00:00",
        "image_url": "/auth/diagnostics/bitmap/" + "a" * 64 + ".bmp",
    }
    assert payload["home_assistant_setup"] == {"state": "connected"}
    assert "456789" not in response.text
    assert "cookie-value-secret" not in response.text
    assert "student-secret-cookie" not in response.text


def test_auth_diagnostics_handles_missing_bitmap_without_leaking_error_details(
    tmp_path,
):
    async def request_diagnostics():
        manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://localhost:6080/vnc.html",
            login_url="https://www.lectio.dk/",
        )
        app.state.auth_manager = manager
        app.state.lectio_data_service = FakeDiagnosticsDataService()
        app.state.display_diagnostics_client = FakeDisplayDiagnosticsClient(
            {"available": False, "state": "not_rendered"}
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/auth/diagnostics")

    response = asyncio.run(request_diagnostics())

    assert response.status_code == 200
    assert response.json()["display"] == {
        "available": False,
        "state": "not_rendered",
    }
    assert "student-secret-cookie" not in response.text


def test_auth_diagnostics_reports_false_when_session_has_no_student_id(tmp_path):
    async def request_diagnostics():
        manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://localhost:6080/vnc.html",
            login_url="https://www.lectio.dk/",
        )
        manager.session = AuthenticatedLectioSession(
            school_id="123",
            student_id=None,
            cookies=[
                LectioCookie(
                    name="ASP.NET_SessionId",
                    value="cookie-value-secret",
                    domain="www.lectio.dk",
                )
            ],
        )
        app.state.auth_manager = manager
        app.state.lectio_data_service = FakeDiagnosticsDataService()
        app.state.display_diagnostics_client = FakeDisplayDiagnosticsClient(
            {"available": False, "state": "not_rendered"}
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/auth/diagnostics")

    response = asyncio.run(request_diagnostics())

    assert response.status_code == 200
    assert response.json()["auth"]["student_id_available"] is False
    assert "cookie-value-secret" not in response.text


def test_manual_student_id_is_validated_saved_and_not_returned(monkeypatch, tmp_path):
    async def validate_session(self):
        assert self.authenticated_session.student_id == "456789"
        return True

    monkeypatch.setattr(LectioClient, "validate_session", validate_session)

    async def configure_id():
        manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://localhost:6080/vnc.html",
            login_url="https://www.lectio.dk/",
        )
        manager.session = AuthenticatedLectioSession(
            school_id="123",
            student_id=None,
            cookies=[
                LectioCookie(
                    name="ASP.NET_SessionId",
                    value="cookie-value-secret",
                    domain="www.lectio.dk",
                )
            ],
        )
        app.state.auth_manager = manager

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            configured = await client.post(
                "/auth/student-id",
                json={"student_id": "456789"},
                headers={"origin": "http://test"},
            )
            status = await client.get("/auth/status")
            return manager, configured, status

    manager, response, status = asyncio.run(configure_id())

    assert response.status_code == 200
    assert response.json() == {"student_id_available": True}
    assert "456789" not in response.text
    assert "cookie-value-secret" not in response.text
    assert manager.session.student_id == "456789"
    saved = AuthenticatedLectioSession.from_json(
        (Path(tmp_path) / "lectio-session.json").read_text(encoding="utf-8")
    )
    assert saved.student_id == "456789"
    assert status.json()["student_id_available"] is True
    assert "student_id" not in status.json()
    assert "456789" not in status.text


def test_manual_student_id_is_not_saved_when_lectio_rejects_it(monkeypatch, tmp_path):
    async def validate_session(self):
        return False

    monkeypatch.setattr(LectioClient, "validate_session", validate_session)

    async def configure_id():
        manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://localhost:6080/vnc.html",
            login_url="https://www.lectio.dk/",
        )
        manager.session = AuthenticatedLectioSession(
            school_id="123",
            student_id=None,
            cookies=[
                LectioCookie(
                    name="ASP.NET_SessionId",
                    value="cookie-value-secret",
                    domain="www.lectio.dk",
                )
            ],
        )
        app.state.auth_manager = manager

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/auth/student-id",
                json={"student_id": "456789"},
                headers={"origin": "http://test"},
            )
            return manager, response

    manager, response = asyncio.run(configure_id())

    assert response.status_code == 422
    assert "456789" not in response.text
    assert "cookie-value-secret" not in response.text
    assert manager.session.student_id is None


class FakeDiagnosticsDataService:
    def statuses(self):
        return {
            "schedule": LectioSyncStatus(
                state="valid",
                is_stale=False,
                last_attempt_at=datetime(2026, 9, 25, 5, 29, tzinfo=timezone.utc),
                last_successful_sync=datetime(2026, 9, 25, 5, 30, tzinfo=timezone.utc),
            ),
            "assignments": LectioSyncStatus(
                state="error",
                is_stale=True,
                error="student-secret-cookie",
            ),
        }


class FakeDisplayDiagnosticsClient:
    def __init__(self, response):
        self.response = response

    async def current(self):
        return self.response

    async def setup(self):
        return {"state": "connected"}

    async def image(self, content_hash):
        assert content_hash == "a" * 64
        return b"BM newest display"


def test_auth_diagnostics_bitmap_route_serves_current_image_without_caching(tmp_path):
    async def request_bitmap():
        manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://localhost:6080/vnc.html",
            login_url="https://www.lectio.dk/",
        )
        app.state.auth_manager = manager
        app.state.display_diagnostics_client = FakeDisplayDiagnosticsClient(
            {"available": True, "content_hash": "a" * 64}
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            good = await client.get(
                "/auth/diagnostics/bitmap/" + "a" * 64 + ".bmp"
            )
            invalid = await client.get("/auth/diagnostics/bitmap/not-a-hash.bmp")
            return good, invalid

    good, invalid = asyncio.run(request_bitmap())

    assert good.status_code == 200
    assert good.headers["content-type"] == "image/bmp"
    assert good.headers["cache-control"] == "no-store"
    assert good.content == b"BM newest display"
    assert invalid.status_code == 404


def test_login_page_contains_source_diagnostics_and_bitmap_preview(tmp_path):
    async def request_page():
        app.state.auth_manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://localhost:6080/vnc.html",
            login_url="https://www.lectio.dk/",
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/auth/browser")

    response = asyncio.run(request_page())

    assert response.status_code == 200
    assert 'id="source-diagnostics"' in response.text
    assert 'id="display-preview"' in response.text
    assert 'id="home-assistant-setup-info"' in response.text
    assert "refreshDiagnostics" in response.text
