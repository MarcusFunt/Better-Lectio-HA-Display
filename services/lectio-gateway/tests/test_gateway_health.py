import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from lectio_gateway.auth.manager import AuthManager
from lectio_gateway.lectio.models import AuthenticatedLectioSession, LectioCookie
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
