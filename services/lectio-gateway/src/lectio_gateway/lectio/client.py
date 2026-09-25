import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from lectio_gateway.lectio.errors import (
    LectioAdapterError,
    LectioResponseChanged,
    LectioSessionExpired,
)
from lectio_gateway.lectio.models import (
    AuthenticatedLectioSession,
    LectioAssignment,
    LectioCancellation,
    LectioHomework,
    LectioLesson,
)
from lectio_gateway.lectio.parsers import (
    LECTIO_BASE_URL,
    normalize_assignments,
    normalize_homework,
    parse_schedule_html,
)

_COPENHAGEN = ZoneInfo("Europe/Copenhagen")


class LectioClient:
    """Async application boundary around the synchronous python-lectio SDK."""

    def __init__(
        self,
        authenticated_session: AuthenticatedLectioSession,
        *,
        sdk_client: Any | None = None,
        timeout_seconds: float = 15.0,
    ):
        self.authenticated_session = authenticated_session
        self._sdk = (
            sdk_client
            if sdk_client is not None
            else self._restore_sdk(authenticated_session)
        )
        self._timeout_seconds = timeout_seconds
        self._session_lock = RLock()

    @staticmethod
    def _restore_sdk(authenticated_session: AuthenticatedLectioSession) -> Any:
        try:
            import lectio
        except ImportError as exc:
            raise RuntimeError(
                "python-lectio is required by the Lectio gateway"
            ) from exc

        if authenticated_session.student_id is None:
            # The temporary browser can expose a valid own-schedule session without
            # either LastLogin identity cookie. The SDK constructor requires both,
            # so initialize only the session fields needed for schedule requests.
            client = lectio.sdk.__new__(lectio.sdk)
            client.session = requests.Session()
            client.skoleId = authenticated_session.school_id
            client.elevId = None
        else:
            identity_cookies = [
                {
                    "name": "LastLoginExamno",
                    "value": authenticated_session.school_id,
                    "for": "www.lectio.dk/",
                },
                {
                    "name": "LastLoginElevId",
                    "value": authenticated_session.student_id,
                    "for": "www.lectio.dk/",
                },
            ]
            cookie_payload = base64.b64encode(
                json.dumps(identity_cookies, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
            client = lectio.sdk(base64Cookie=cookie_payload)

        # python-lectio 1.31.0 reads the combined domain/path value as a domain.
        # Rebuild the cookie jar with the browser-provided attributes.
        client.session.cookies.clear()
        now = int(datetime.now(tz=timezone.utc).timestamp())
        for cookie in authenticated_session.cookies:
            if cookie.expires is not None and cookie.expires <= now:
                continue
            client.session.cookies.set(
                cookie.name,
                cookie.value.get_secret_value(),
                domain=cookie.domain,
                path=cookie.path,
                secure=cookie.secure,
                expires=cookie.expires,
            )
        client.session.cookies.set(
            "LastLoginExamno",
            authenticated_session.school_id,
            domain="www.lectio.dk",
            path="/",
        )
        if authenticated_session.student_id is not None:
            client.session.cookies.set(
                "LastLoginElevId",
                authenticated_session.student_id,
                domain="www.lectio.dk",
                path="/",
            )
        client.session.cookies.set("isloggedin3", "Y", domain="www.lectio.dk", path="/")
        return client

    def _schedule_url(self, iso_year: int, iso_week: int) -> str:
        url = (
            f"{LECTIO_BASE_URL}/lectio/{self.authenticated_session.school_id}/SkemaNy.aspx"
        )
        if self.authenticated_session.student_id is None:
            return f"{url}?week={iso_week:02d}{iso_year}"
        return (
            f"{url}?type=elev&elevid={self.authenticated_session.student_id}"
            f"&week={iso_week:02d}{iso_year}"
        )

    @staticmethod
    def _is_login_response(response: Any) -> bool:
        status_code = getattr(response, "status_code", 200)
        if status_code in (401, 403):
            return True
        response_url = getattr(response, "url", "") or ""
        if urlparse(response_url).path.lower().endswith("/login.aspx"):
            return True
        soup = BeautifulSoup(getattr(response, "text", ""), "html.parser")
        return (
            soup.find("input", {"name": "m$Content$username"}) is not None
            and soup.find("input", {"name": "m$Content$password"}) is not None
        )

    def _fetch_schedule_html(self, iso_year: int, iso_week: int) -> str:
        url = self._schedule_url(iso_year, iso_week)
        with self._session_lock:
            response = self._sdk.session.get(url, timeout=self._timeout_seconds)
        if self._is_login_response(response):
            raise LectioSessionExpired("Lectio redirected to the login page")
        response.raise_for_status()
        return response.text

    def _validate_session(self) -> bool:
        now = datetime.now(_COPENHAGEN).date()
        iso_year, iso_week, _ = now.isocalendar()
        url = self._schedule_url(iso_year, iso_week)
        with self._session_lock:
            response = self._sdk.session.get(url, timeout=self._timeout_seconds)
        if self._is_login_response(response):
            return False
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        if (
            soup.find("tr", class_="s2dayHeader") is None
            or soup.find("div", id="s_m_HeaderContent_MainTitle") is None
        ):
            raise LectioResponseChanged("Lectio response is not a recognized schedule page")
        return True

    async def validate_session(self) -> bool:
        return await asyncio.to_thread(self._validate_session)

    @staticmethod
    def _validate_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
        if (
            start.tzinfo is None
            or start.utcoffset() is None
            or end.tzinfo is None
            or end.utcoffset() is None
            or start >= end
        ):
            raise ValueError("Range must contain ordered timezone-aware datetimes")
        return start.astimezone(_COPENHAGEN), end.astimezone(_COPENHAGEN)

    def _get_schedule(self, start: datetime, end: datetime) -> list[LectioLesson]:
        start, end = self._validate_range(start, end)
        first_day = start.date()
        last_day = (end - timedelta(microseconds=1)).date()
        monday = first_day - timedelta(days=first_day.weekday())
        lessons: list[LectioLesson] = []
        while monday <= last_day:
            iso_year, iso_week, _ = monday.isocalendar()
            lessons.extend(self._get_schedule_week(iso_year, iso_week))
            monday += timedelta(days=7)
        return [
            lesson for lesson in lessons if lesson.start < end and lesson.end > start
        ]

    def _get_schedule_week(self, iso_year: int, iso_week: int) -> list[LectioLesson]:
        """Fetch and normalize exactly one ISO-week schedule page."""
        datetime.fromisocalendar(iso_year, iso_week, 1)
        html = self._fetch_schedule_html(iso_year, iso_week)
        return parse_schedule_html(
            html,
            school_id=self.authenticated_session.school_id,
            iso_year=iso_year,
            iso_week=iso_week,
        )

    async def get_schedule_week(
        self, iso_year: int, iso_week: int
    ) -> list[LectioLesson]:
        """Fetch one normalized Lectio ISO-week page."""
        return await asyncio.to_thread(self._get_schedule_week, iso_year, iso_week)

    async def get_schedule(self, start: datetime, end: datetime) -> list[LectioLesson]:
        return await asyncio.to_thread(self._get_schedule, start, end)

    def _invoke_sdk(self, method_name: str, *args: Any) -> Any:
        if self.authenticated_session.student_id is None and method_name in {
            "lektier",
            "opgaver",
            "opgave",
        }:
            raise LectioAdapterError(
                "This Lectio session did not expose a student ID required for this resource"
            )
        try:
            with self._session_lock:
                return getattr(self._sdk, method_name)(*args)
        except Exception as exc:
            if "lectio-cookie udløbet" in str(exc).casefold():
                raise LectioSessionExpired(
                    "python-lectio reports an expired session"
                ) from exc
            if isinstance(exc, (AttributeError, IndexError, KeyError, TypeError)):
                raise LectioResponseChanged(
                    f"python-lectio could not parse the {method_name} response"
                ) from exc
            raise LectioAdapterError(
                f"python-lectio {method_name} request failed"
            ) from exc

    def _get_assignments(self, start: datetime, end: datetime) -> list[LectioAssignment]:
        start, end = self._validate_range(start, end)
        rows = self._invoke_sdk("opgaver")
        assignments = normalize_assignments(
            rows, school_id=self.authenticated_session.school_id
        )
        in_range = [
            item for item in assignments
            if item.due is None or start <= item.due < end
        ]
        enriched = []
        for assignment in in_range:
            if assignment.description is None and assignment.source_id is not None:
                try:
                    details = self._invoke_sdk("opgave", assignment.source_id)
                except LectioSessionExpired:
                    raise
                except LectioAdapterError:
                    # Assignment details are optional. Keep the normalized list row
                    # when Lectio changes its detail markup or that page is unavailable.
                    details = None
                oplysninger = (
                    details.get("oplysninger", {})
                    if isinstance(details, dict)
                    else {}
                )
                description = (
                    str(oplysninger.get("opgavebeskrivelse") or "").strip() or None
                    if isinstance(oplysninger, dict)
                    else None
                )
                assignment = assignment.model_copy(update={"description": description})
            enriched.append(assignment)
        return enriched

    async def get_assignments(
        self, start: datetime, end: datetime
    ) -> list[LectioAssignment]:
        return await asyncio.to_thread(self._get_assignments, start, end)

    async def get_homework(self, start: datetime, end: datetime) -> list[LectioHomework]:
        start, end = self._validate_range(start, end)
        lessons = await self.get_schedule(start, end)
        rows = await asyncio.to_thread(self._invoke_sdk, "lektier")
        items = normalize_homework(
            rows,
            school_id=self.authenticated_session.school_id,
            lessons=lessons,
        )
        return [
            item for item in items
            if item.target_lesson_start is None or start <= item.target_lesson_start < end
        ]

    async def get_cancellations(
        self, start: datetime, end: datetime
    ) -> list[LectioCancellation]:
        lessons = await self.get_schedule(start, end)
        cancellations = []
        for lesson in lessons:
            if lesson.status != "cancelled":
                continue
            cancellations.append(
                LectioCancellation(
                    id=lesson.id,
                    original_lesson=lesson,
                    start=lesson.start,
                    end=lesson.end,
                    subject=lesson.subject,
                    teacher=lesson.teacher,
                    room=lesson.room,
                    reason=lesson.details,
                    details=lesson.details,
                    source_url=lesson.source_url,
                )
            )
        return cancellations
