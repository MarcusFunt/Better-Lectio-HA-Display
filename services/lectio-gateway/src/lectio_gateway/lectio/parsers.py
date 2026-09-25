import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from lectio_gateway.lectio.errors import LectioResponseChanged
from lectio_gateway.lectio.models import LectioAssignment, LectioHomework, LectioLesson

LECTIO_BASE_URL = "https://www.lectio.dk"
COPENHAGEN = ZoneInfo("Europe/Copenhagen")
_TIME_RANGE = re.compile(r"(?P<start>\d{1,2}:\d{2})\s*(?:til|-)\s*(?P<end>\d{1,2}:\d{2})")
_ABS_ID = re.compile(r"absid=(\d+)")
_DATE = re.compile(
    r"(?P<day>\d{1,2})[./-](?P<month>\d{1,2})[./-](?P<year>\d{2,4})"
    r"(?:[ T]+(?:kl\.\s*)?(?P<hour>\d{1,2})[:.](?P<minute>\d{2}))?",
    re.IGNORECASE,
)


def _localize(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=COPENHAGEN)
    return value.astimezone(COPENHAGEN)


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _localize(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        return _localize(parsed)
    match = _DATE.search(text)
    if not match:
        return None
    year = int(match.group("year"))
    if year < 100:
        year += 2000
    try:
        return datetime(
            year,
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour") or 0),
            int(match.group("minute") or 0),
            tzinfo=COPENHAGEN,
        )
    except ValueError:
        return None


def _date_for_header(text: str, expected: date) -> date:
    match = re.search(r"\((\d{1,2})/(\d{1,2})\)", text)
    if not match:
        return expected
    day, month = map(int, match.groups())
    candidates = []
    for year in (expected.year - 1, expected.year, expected.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        raise LectioResponseChanged("Schedule day header has an invalid date")
    candidate = min(candidates, key=lambda value: abs((value - expected).days))
    if abs((candidate - expected).days) > 7:
        raise LectioResponseChanged("Schedule day header does not match its ISO week")
    return candidate


def _tooltip_fields(tooltip: str):
    title = None
    fields: dict[str, str] = {}
    time_range = None
    for line in tooltip.replace("\r", "").splitlines():
        line = line.strip()
        if not line:
            continue
        time_match = _TIME_RANGE.search(line)
        if time_match:
            try:
                time_range = (
                    time(*map(int, time_match.group("start").split(":"))),
                    time(*map(int, time_match.group("end").split(":"))),
                )
            except ValueError as exc:
                raise LectioResponseChanged("Schedule contains an invalid lesson time") from exc
        elif ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().casefold()] = value.strip()
        elif title is None:
            title = line
        else:
            fields.setdefault("details", line)
    return title, fields, time_range


def _lesson_status(classes: list[str]) -> str:
    values = {value.casefold() for value in classes}
    if any("cancel" in value for value in values):
        return "cancelled"
    if any("changed" in value for value in values):
        return "changed"
    if any("eksamen" in value or "exam" in value for value in values):
        return "exam"
    if any("normal" in value for value in values):
        return "normal"
    return "unknown"


def _stable_id(value: Any) -> str:
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def parse_schedule_html(
    html: str, *, school_id: str, iso_year: int, iso_week: int
) -> list[LectioLesson]:
    soup = BeautifulSoup(html, "html.parser")
    header = soup.find("tr", class_="s2dayHeader")
    title = soup.find("div", id="s_m_HeaderContent_MainTitle")
    containers = soup.find_all("div", class_="s2skemabrikcontainer")
    if header is None or title is None or not containers:
        raise LectioResponseChanged("Schedule page is missing expected Lectio markers")

    header_cells = header.find_all("td")
    week_monday = date.fromisocalendar(iso_year, iso_week, 1)
    lessons = []
    for day_offset, container in enumerate(containers[1:]):
        expected_day = week_monday + timedelta(days=day_offset)
        cell_index = day_offset + 1
        header_text = (
            header_cells[cell_index].get_text(" ", strip=True)
            if cell_index < len(header_cells)
            else ""
        )
        lesson_day = _date_for_header(header_text, expected_day)
        for card in container.find_all("a", class_="s2skemabrik"):
            tooltip = card.get("data-tooltip")
            if not isinstance(tooltip, str) or not tooltip.strip():
                raise LectioResponseChanged("Schedule lesson is missing its tooltip")
            subject_title, fields, time_range = _tooltip_fields(tooltip)
            if time_range is None:
                raise LectioResponseChanged("Schedule lesson is missing its start/end time")

            start_time, end_time = time_range
            start = datetime.combine(lesson_day, start_time, tzinfo=COPENHAGEN)
            end_day = lesson_day + timedelta(days=1) if end_time <= start_time else lesson_day
            end = datetime.combine(end_day, end_time, tzinfo=COPENHAGEN)
            href = card.get("href")
            match = _ABS_ID.search(href or "")
            source_id = match.group(1) if match else card.get("data-absid")
            lesson_id = str(source_id or _stable_id((lesson_day.isoformat(), tooltip)))
            source_url = urljoin(LECTIO_BASE_URL, href) if href else None
            subject = fields.get("fag") or fields.get("subject") or subject_title
            details = fields.get("aflyst") or fields.get("årsag") or fields.get("details")
            lessons.append(
                LectioLesson(
                    id=lesson_id,
                    start=start,
                    end=end,
                    subject=subject,
                    teacher=fields.get("lærer") or fields.get("laerer") or fields.get("teacher"),
                    room=fields.get("lokale") or fields.get("room"),
                    status=_lesson_status(card.get("class", [])),
                    source_url=source_url,
                    source_id=str(source_id) if source_id is not None else None,
                    details=details,
                )
            )
    return lessons


def normalize_assignments(rows: Any, *, school_id: str) -> list[LectioAssignment]:
    if not isinstance(rows, list):
        raise LectioResponseChanged("python-lectio assignments were not a list")
    assignments = []
    for row in rows:
        if not isinstance(row, dict):
            raise LectioResponseChanged("python-lectio returned an invalid assignment row")
        source_id = row.get("exerciseid") or row.get("exercise_id") or row.get("id")
        title = row.get("opgavetitel") or row.get("title")
        if not title:
            raise LectioResponseChanged("Assignment is missing its title")
        identifier = str(source_id or _stable_id(row))
        source_url = (
            urljoin(
                LECTIO_BASE_URL,
                f"/lectio/{school_id}/ElevAflevering.aspx?exerciseid={identifier}",
            )
            if source_id
            else None
        )
        due = parse_datetime(
            row.get("afleveringsfrist") or row.get("due") or row.get("frist")
        )
        assignments.append(
            LectioAssignment(
                id=identifier,
                title=str(title).strip(),
                description=(
                    str(row.get("opgavebeskrivelse") or row.get("description") or "").strip()
                    or None
                ),
                due=due,
                subject=(str(row.get("hold") or row.get("fag") or "").strip() or None),
                status=str(row.get("status") or "unknown").strip(),
                source_url=source_url,
                source_id=str(source_id) if source_id is not None else None,
            )
        )
    return assignments


def normalize_homework(
    rows: Any, *, school_id: str, lessons: list[LectioLesson] | None = None
) -> list[LectioHomework]:
    if not isinstance(rows, list):
        raise LectioResponseChanged("python-lectio homework was not a list")
    lessons_by_id = {lesson.source_id: lesson for lesson in (lessons or []) if lesson.source_id}
    homework_items = []
    for row in rows:
        if not isinstance(row, dict):
            raise LectioResponseChanged("python-lectio returned an invalid homework row")
        activity = row.get("aktivitet")
        activity = activity if isinstance(activity, dict) else {}
        homework = row.get("lektier")
        homework = homework if isinstance(homework, dict) else {}
        source_id = activity.get("absid")
        source_id = str(source_id) if source_id is not None else None
        lesson = lessons_by_id.get(source_id)
        target_start = lesson.start if lesson else parse_datetime(row.get("dato"))
        description = str(
            homework.get("beskrivelse")
            or homework.get("description")
            or row.get("note")
            or ""
        ).strip()
        if not description:
            raise LectioResponseChanged("Homework row is missing its description")
        identifier = source_id or _stable_id(row)
        link = homework.get("link")
        if isinstance(link, str) and link and link != "se modul siden":
            source_url = urljoin(LECTIO_BASE_URL, link)
        elif source_id:
            source_url = urljoin(
                LECTIO_BASE_URL,
                f"/lectio/{school_id}/aktivitet/aktivitetforside2.aspx?absid={source_id}",
            )
        else:
            source_url = None
        homework_items.append(
            LectioHomework(
                id=identifier,
                subject=(
                    str(
                        activity.get("fag")
                        or activity.get("hold")
                        or activity.get("navn")
                        or ""
                    ).strip()
                    or None
                ),
                description=description,
                target_lesson_start=target_start,
                source_url=source_url,
            )
        )
    return homework_items
