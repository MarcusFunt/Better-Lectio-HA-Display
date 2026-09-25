import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from lectio_gateway.lectio.client import LectioClient
from lectio_gateway.lectio.errors import (
    LectioAdapterError,
    LectioResponseChanged,
    LectioSessionExpired,
)
from lectio_gateway.lectio.models import AuthenticatedLectioSession, LectioCookie
from lectio_gateway.lectio.parsers import (
    normalize_assignments,
    normalize_homework,
    parse_schedule_html,
)
from requests import Request

FIXTURES = Path(__file__).parent / "fixtures"
COPENHAGEN = ZoneInfo("Europe/Copenhagen")


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text())


def make_session():
    return AuthenticatedLectioSession(
        school_id="123",
        student_id="456",
        cookies=[
            LectioCookie(
                name="ASP.NET_SessionId",
                value="synthetic-secret-cookie",
                domain=".lectio.dk",
            )
        ],
    )


def make_session_without_student_id():
    return AuthenticatedLectioSession(
        school_id="123",
        student_id=None,
        cookies=[
            LectioCookie(
                name="ASP.NET_SessionId",
                value="synthetic-secret-cookie",
                domain=".lectio.dk",
            )
        ],
    )


def test_session_json_restores_cookie_without_leaking_repr():
    session = make_session()
    assert "synthetic-secret-cookie" not in repr(session)
    assert "synthetic-secret-cookie" not in session.model_dump_json()
    restored = AuthenticatedLectioSession.from_json(session.to_json())
    assert restored.cookies[0].value.get_secret_value() == "synthetic-secret-cookie"
    assert LectioClient(restored).authenticated_session == restored


def test_schedule_fixture_normalizes_dates_fields_and_cancellation_status():
    lessons = parse_schedule_html(
        (FIXTURES / "schedule.html").read_text(),
        school_id="123",
        iso_year=2026,
        iso_week=39,
    )
    assert len(lessons) == 2
    assert lessons[0].start == datetime(2026, 9, 21, 8, 15, tzinfo=COPENHAGEN)
    assert lessons[0].end == datetime(2026, 9, 21, 9, 0, tzinfo=COPENHAGEN)
    assert (lessons[0].subject, lessons[0].teacher, lessons[0].room) == (
        "Matematik", "Ada Example", "A1"
    )
    assert lessons[0].status == "normal"
    assert lessons[1].status == "cancelled"
    assert lessons[1].details == "Lærer fraværende"
    assert lessons[1].source_id == "9002"


def test_schedule_parser_accepts_single_digit_hour_values():
    html = (FIXTURES / "schedule.html").read_text().replace(
        "08:15 til 09:00", "8:15 til 9:00"
    )
    lessons = parse_schedule_html(html, school_id="123", iso_year=2026, iso_week=39)
    assert lessons[0].start.hour == 8
    assert lessons[0].end.hour == 9


def test_assignment_fixture_normalizes_due_status_and_source_link():
    assignments = normalize_assignments(load_json("assignments.json"), school_id="123")
    assert len(assignments) == 1
    assignment = assignments[0]
    assert assignment.id == "7001"
    assert assignment.title == "Essay om klima"
    assert assignment.description == "Skriv en kort analyse."
    assert assignment.due == datetime(2026, 9, 30, 23, 59, tzinfo=COPENHAGEN)
    assert assignment.subject == "Dansk A"
    assert assignment.status == "Ikke afleveret"
    assert assignment.source_url.endswith("exerciseid=7001")


def test_homework_fixture_links_to_normalized_schedule_lesson():
    lesson = parse_schedule_html(
        (FIXTURES / "schedule.html").read_text(),
        school_id="123",
        iso_year=2026,
        iso_week=39,
    )[0]
    homework = normalize_homework(
        load_json("homework.json"), school_id="123", lessons=[lesson]
    )
    assert len(homework) == 1
    assert homework[0].id == "9001"
    assert homework[0].subject == "2x"
    assert homework[0].description == "Læs sider 10-12."
    assert homework[0].target_lesson_start == lesson.start
    assert homework[0].source_url.endswith("absid=9001")


def test_changed_schedule_markup_fails_loudly():
    with pytest.raises(LectioResponseChanged):
        parse_schedule_html(
            (FIXTURES / "malformed_schedule.html").read_text(),
            school_id="123",
            iso_year=2026,
            iso_week=39,
        )


class FakeResponse:
    def __init__(self, *, url: str, text: str, status_code: int = 200):
        self.url = url
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttpSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.requested = []

    def get(self, url: str, **kwargs):
        self.requested.append((url, kwargs))
        return self.response


class FakeSdk:
    def __init__(self, *, session, homework=None, assignments=None, assignment_details=None):
        self.session = session
        self._homework = homework or []
        self._assignments = assignments or []
        self._assignment_details = assignment_details or {}

    def lektier(self):
        return self._homework

    def opgaver(self):
        return self._assignments

    def opgave(self, exercise_id):
        return self._assignment_details[exercise_id]


class ExpiredSdk(FakeSdk):
    def opgaver(self):
        raise RuntimeError("lectio-cookie udløbet")


def test_client_restores_cookies_with_correct_host_and_path():
    client = LectioClient(make_session())
    prepared = client._sdk.session.prepare_request(
        Request("GET", "https://www.lectio.dk/lectio/123/SkemaNy.aspx")
    )
    assert "ASP.NET_SessionId=synthetic-secret-cookie" in prepared.headers["Cookie"]
    assert "LastLoginExamno=123" in prepared.headers["Cookie"]
    assert "LastLoginElevId=456" in prepared.headers["Cookie"]


def test_client_restores_session_without_synthesizing_student_identity():
    client = LectioClient(make_session_without_student_id())

    assert client._sdk.elevId is None
    assert client._sdk.session.cookies.get("ASP.NET_SessionId") == "synthetic-secret-cookie"
    assert client._sdk.session.cookies.get("LastLoginExamno") == "123"
    assert client._sdk.session.cookies.get("LastLoginElevId") is None


def test_schedule_url_uses_authenticated_default_schedule_without_student_id():
    client = LectioClient(
        make_session_without_student_id(),
        sdk_client=FakeSdk(
            session=FakeHttpSession(FakeResponse(url="", text="")),
        ),
    )

    assert client._schedule_url(2026, 39) == (
        "https://www.lectio.dk/lectio/123/SkemaNy.aspx?week=392026"
    )


def test_validate_session_accepts_default_schedule_without_student_id():
    http = FakeHttpSession(
        FakeResponse(
            url="https://www.lectio.dk/lectio/123/SkemaNy.aspx?week=392026",
            text=(FIXTURES / "schedule.html").read_text(),
        )
    )
    client = LectioClient(
        make_session_without_student_id(), sdk_client=FakeSdk(session=http)
    )

    assert asyncio.run(client.validate_session()) is True
    assert "elevid=" not in http.requested[0][0]
    assert "type=elev" not in http.requested[0][0]


def test_student_resources_fail_clearly_when_session_has_no_student_id():
    client = LectioClient(
        make_session_without_student_id(),
        sdk_client=FakeSdk(
            session=FakeHttpSession(FakeResponse(url="", text="")),
        ),
    )

    with pytest.raises(LectioAdapterError, match="did not expose a student ID"):
        client._invoke_sdk("opgaver")


def test_session_model_rejects_non_lectio_cookies():
    with pytest.raises(ValueError):
        LectioCookie(name="idp", value="third-party", domain="login.example.org")


def test_validate_session_returns_false_for_expired_login_fixture():
    http = FakeHttpSession(
        FakeResponse(
            url="https://www.lectio.dk/lectio/123/login.aspx",
            text=(FIXTURES / "expired_session.html").read_text(),
        )
    )
    client = LectioClient(make_session(), sdk_client=FakeSdk(session=http))
    assert asyncio.run(client.validate_session()) is False
    assert http.requested[0][1]["timeout"] > 0


def test_validate_session_accepts_schedule_fixture():
    http = FakeHttpSession(
        FakeResponse(
            url="https://www.lectio.dk/lectio/123/SkemaNy.aspx",
            text=(FIXTURES / "schedule.html").read_text(),
        )
    )
    client = LectioClient(make_session(), sdk_client=FakeSdk(session=http))
    assert asyncio.run(client.validate_session()) is True


def test_client_normalizes_assignments_from_the_pinned_sdk():
    sdk = FakeSdk(
        session=FakeHttpSession(FakeResponse(url="", text="")),
        assignments=load_json("assignments.json"),
    )
    client = LectioClient(make_session(), sdk_client=sdk)
    start = datetime(2026, 9, 1, tzinfo=COPENHAGEN)
    end = datetime(2026, 10, 1, tzinfo=COPENHAGEN)
    assert [item.id for item in asyncio.run(client.get_assignments(start, end))] == ["7001"]


def test_client_enriches_missing_assignment_description_from_sdk_details():
    sdk = FakeSdk(
        session=FakeHttpSession(FakeResponse(url="", text="")),
        assignments=[{
            "exerciseid": "7001",
            "opgavetitel": "Essay om klima",
            "afleveringsfrist": "30-09-2026 23:59",
            "hold": "Dansk A",
            "status": "Ikke afleveret",
        }],
        assignment_details={
            "7001": {"oplysninger": {"opgavebeskrivelse": "Skriv en kort analyse."}}
        },
    )
    client = LectioClient(make_session(), sdk_client=sdk)
    start = datetime(2026, 9, 1, tzinfo=COPENHAGEN)
    end = datetime(2026, 10, 1, tzinfo=COPENHAGEN)
    result = asyncio.run(client.get_assignments(start, end))
    assert result[0].description == "Skriv en kort analyse."


def test_client_translates_upstream_expired_session_error():
    client = LectioClient(
        make_session(),
        sdk_client=ExpiredSdk(session=FakeHttpSession(FakeResponse(url="", text=""))),
    )
    start = datetime(2026, 9, 1, tzinfo=COPENHAGEN)
    end = datetime(2026, 10, 1, tzinfo=COPENHAGEN)
    with pytest.raises(LectioSessionExpired):
        asyncio.run(client.get_assignments(start, end))


def test_client_normalizes_homework_and_cancellations_from_fixtures():
    http = FakeHttpSession(
        FakeResponse(
            url="https://www.lectio.dk/lectio/123/SkemaNy.aspx",
            text=(FIXTURES / "schedule.html").read_text(),
        )
    )
    sdk = FakeSdk(session=http, homework=load_json("homework.json"))
    client = LectioClient(make_session(), sdk_client=sdk)
    start = datetime(2026, 9, 21, tzinfo=COPENHAGEN)
    end = datetime(2026, 9, 23, tzinfo=COPENHAGEN)
    homework = asyncio.run(client.get_homework(start, end))
    cancellations = asyncio.run(client.get_cancellations(start, end))
    assert homework[0].target_lesson_start == datetime(
        2026, 9, 21, 8, 15, tzinfo=COPENHAGEN
    )
    assert cancellations[0].original_lesson.source_id == "9002"
    assert cancellations[0].reason == "Lærer fraværende"
