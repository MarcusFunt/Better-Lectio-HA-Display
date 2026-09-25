import asyncio
from datetime import datetime, timezone
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from lectio_gateway.auth.manager import AuthManager, AuthState
from lectio_gateway.data_service import (
    LectioAuthenticationRequired,
    LectioSourceUnavailable,
    LectioStudentIdRequired,
)
from lectio_gateway.lectio.models import (
    AuthenticatedLectioSession,
    LectioAssignment,
    LectioCancellation,
    LectioCookie,
    LectioHomework,
    LectioLesson,
    LectioSyncStatus,
)
from lectio_gateway.main import app


class FakeLectioDataService:
    def __init__(self):
        self.lesson = LectioLesson(
            id="lesson-1",
            start=datetime(2026, 9, 25, 8, 15, tzinfo=timezone.utc),
            end=datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc),
            subject="Matematik",
            teacher="Ada Example",
            room="A1",
        )
        self.sync_status = LectioSyncStatus(
            state="valid",
            last_successful_sync=datetime(2026, 9, 24, tzinfo=timezone.utc),
        )
        self.items = {
            "schedule": [self.lesson],
            "assignments": [
                LectioAssignment(id="assignment-1", title="Dansk aflevering")
            ],
            "homework": [
                LectioHomework(id="homework-1", description="Læs kapitel 2")
            ],
            "cancellations": [
                LectioCancellation(
                    id="cancellation-1",
                    original_lesson=self.lesson,
                    start=self.lesson.start,
                    end=self.lesson.end,
                )
            ],
        }
        self.requested = []
        self.failure = None

    async def get_source(self, source, start, end):
        self.requested.append((source, start, end))
        if self.failure is not None:
            raise self.failure
        return self.items[source], self.sync_status

    def statuses(self):
        return {"schedule": self.sync_status}


def _authenticated_manager(tmp_path):
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
                value="synthetic-secret-cookie",
                domain="www.lectio.dk",
            )
        ],
    )
    manager._set_state(AuthState.AUTHENTICATED)
    return manager


def test_schedule_api_returns_normalized_lessons_with_freshness(tmp_path):
    async def request_schedule():
        service = FakeLectioDataService()
        app.state.auth_manager = _authenticated_manager(tmp_path)
        app.state.lectio_data_service = service
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/schedule",
                params={
                    "start": "2026-09-25T00:00:00+02:00",
                    "end": "2026-09-26T00:00:00+02:00",
                },
            )
        return response, service

    response, service = asyncio.run(request_schedule())

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["id"] == "lesson-1"
    assert payload["items"][0]["subject"] == "Matematik"
    assert payload["sync"]["state"] == "valid"
    assert service.requested[0][0] == "schedule"
    assert service.requested[0][1].utcoffset().total_seconds() == 7200
    assert "456789" not in response.text
    assert "synthetic-secret-cookie" not in response.text


def test_api_status_reports_id_presence_and_per_source_sync_without_id(tmp_path):
    async def request_status():
        app.state.auth_manager = _authenticated_manager(tmp_path)
        app.state.lectio_data_service = FakeLectioDataService()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/api/v1/status")

    response = asyncio.run(request_status())

    assert response.status_code == 200
    payload = response.json()
    assert payload["auth"]["state"] == "AUTHENTICATED"
    assert payload["auth"]["student_id_available"] is True
    assert payload["sources"]["schedule"]["state"] == "valid"
    assert "student_id" not in payload["auth"]
    assert "456789" not in response.text
    assert "synthetic-secret-cookie" not in response.text


def test_each_lectio_source_has_its_own_normalized_endpoint(tmp_path):
    async def request_sources():
        service = FakeLectioDataService()
        app.state.auth_manager = _authenticated_manager(tmp_path)
        app.state.lectio_data_service = service
        routes = {
            "/api/v1/schedule": "schedule",
            "/api/v1/assignments": "assignments",
            "/api/v1/homework": "homework",
            "/api/v1/cancellations": "cancellations",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            responses = {
                source: await client.get(
                    path,
                    params={
                        "start": "2026-09-25T00:00:00+02:00",
                        "end": "2026-09-26T00:00:00+02:00",
                    },
                )
                for path, source in routes.items()
            }
        return service, responses

    service, responses = asyncio.run(request_sources())

    assert set(service.requested[i][0] for i in range(4)) == {
        "schedule",
        "assignments",
        "homework",
        "cancellations",
    }
    assert all(response.status_code == 200 for response in responses.values())
    assert responses["schedule"].json()["items"][0]["subject"] == "Matematik"
    assert responses["assignments"].json()["items"][0]["title"] == "Dansk aflevering"
    assert responses["homework"].json()["items"][0]["description"] == "Læs kapitel 2"
    assert responses["cancellations"].json()["items"][0]["id"] == "cancellation-1"


def test_schedule_api_rejects_reversed_date_range(tmp_path):
    async def request_schedule():
        app.state.auth_manager = _authenticated_manager(tmp_path)
        app.state.lectio_data_service = FakeLectioDataService()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(
                "/api/v1/schedule",
                params={
                    "start": "2026-09-26T00:00:00+02:00",
                    "end": "2026-09-25T00:00:00+02:00",
                },
            )

    response = asyncio.run(request_schedule())

    assert response.status_code == 422


def test_data_api_reports_missing_authentication_without_exposing_identifiers(tmp_path):
    async def request_schedule():
        service = FakeLectioDataService()
        service.failure = LectioAuthenticationRequired("private details")
        app.state.auth_manager = _authenticated_manager(tmp_path)
        app.state.lectio_data_service = service
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(
                "/api/v1/schedule",
                params={
                    "start": "2026-09-25T00:00:00+02:00",
                    "end": "2026-09-26T00:00:00+02:00",
                },
            )

    response = asyncio.run(request_schedule())

    assert response.status_code == 401
    assert "456789" not in response.text
    assert "private details" not in response.text


def test_assignment_api_reports_missing_student_id_without_echoing_it(tmp_path):
    async def request_assignments():
        service = FakeLectioDataService()
        service.failure = LectioStudentIdRequired("private details")
        app.state.auth_manager = _authenticated_manager(tmp_path)
        app.state.lectio_data_service = service
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(
                "/api/v1/assignments",
                params={
                    "start": "2026-09-25T00:00:00+02:00",
                    "end": "2026-09-26T00:00:00+02:00",
                },
            )

    response = asyncio.run(request_assignments())

    assert response.status_code == 409
    assert "456789" not in response.text
    assert "private details" not in response.text


def test_data_api_returns_gateway_error_when_source_has_no_good_cache(tmp_path):
    async def request_schedule():
        service = FakeLectioDataService()
        service.failure = LectioSourceUnavailable("private upstream details")
        app.state.auth_manager = _authenticated_manager(tmp_path)
        app.state.lectio_data_service = service
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(
                "/api/v1/schedule",
                params={
                    "start": "2026-09-25T00:00:00+02:00",
                    "end": "2026-09-26T00:00:00+02:00",
                },
            )

    response = asyncio.run(request_schedule())

    assert response.status_code == 502
    assert "private upstream details" not in response.text
