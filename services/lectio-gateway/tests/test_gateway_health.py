import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from lectio_gateway.auth.manager import AuthManager
from lectio_gateway.lectio.client import LectioClient
from lectio_gateway.lectio.models import (
    AuthenticatedLectioSession,
    LectioCookie,
)
from lectio_gateway.main import app


def test_health_endpoint_reports_gateway_identity():
    async def request_health():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/health")

    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "lectio-gateway"}


def test_auth_diagnostics_reports_student_id_presence_without_returning_secrets(tmp_path):
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
        app.state.auth_manager = manager

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/auth/diagnostics")

    response = asyncio.run(request_diagnostics())

    assert response.status_code == 200
    assert response.json() == {"student_id_available": True}
    assert "456789" not in response.text
    assert "cookie-value-secret" not in response.text


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

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/auth/diagnostics")

    response = asyncio.run(request_diagnostics())

    assert response.status_code == 200
    assert response.json() == {"student_id_available": False}
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
