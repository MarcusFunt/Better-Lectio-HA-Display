from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from display_service.model_builder import build_display_model, display_window

TZ = ZoneInfo("Europe/Copenhagen")


def test_display_window_uses_three_copenhagen_days_across_dst_change():
    now = datetime(2026, 3, 28, 12, 0, tzinfo=TZ)

    start, end, days = display_window(now)

    assert days == (date(2026, 3, 28), date(2026, 3, 29), date(2026, 3, 30))
    assert start == datetime(2026, 3, 28, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 3, 31, 0, 0, tzinfo=TZ)
    assert (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() == 71 * 3600


def test_lectio_and_private_events_share_one_sorted_timeline():
    now = datetime(2026, 9, 25, 7, 0, tzinfo=TZ)
    model = build_display_model(
        now=now,
        lectio_events=[
            {
                "summary": "Mathematics",
                "description": "Teacher: AB\nChapter 4",
                "start": "2026-09-25T09:00:00+02:00",
                "end": "2026-09-25T09:45:00+02:00",
                "location": "2.27",
            }
        ],
        private_events=[
            {
                "summary": "Dentist",
                "start": "2026-09-25T08:00:00+02:00",
                "end": "2026-09-25T08:30:00+02:00",
            },
            {
                "summary": "Birthday",
                "start": "2026-09-26",
                "end": "2026-09-27",
            },
        ],
    )

    assert [day.date for day in model.days] == [
        date(2026, 9, 25),
        date(2026, 9, 26),
        date(2026, 9, 27),
    ]
    assert [event.title for event in model.days[0].events] == [
        "Dentist",
        "Mathematics",
    ]
    lesson = model.days[0].events[1]
    assert (lesson.teacher, lesson.room, lesson.source, lesson.all_day) == (
        "AB",
        "2.27",
        "lectio",
        False,
    )
    birthday = model.days[1].events[0]
    assert birthday.all_day is True
    assert birthday.start == date(2026, 9, 26)
    assert birthday.end == date(2026, 9, 27)


def test_repeated_builds_with_identical_input_are_deterministic():
    now = datetime(2026, 9, 25, 7, 0, tzinfo=TZ)
    inputs = {
        "now": now,
        "lectio_events": [
            {
                "summary": "Physics",
                "start": "2026-09-25T08:00:00+02:00",
                "end": "2026-09-25T08:45:00+02:00",
            }
        ],
    }

    assert build_display_model(**inputs) == build_display_model(**inputs)


def test_sidebar_uses_category_priority_then_urgency_and_respects_limit():
    now = datetime(2026, 9, 25, 8, 0, tzinfo=TZ)
    model = build_display_model(
        now=now,
        cancellations=[
            {"id": "cancel-tomorrow", "subject": "History", "start": "2026-09-26T09:00:00+02:00"},
            {"id": "cancel-today", "subject": "Math", "start": "2026-09-25T10:00:00+02:00"},
        ],
        assignments=[
            {"uid": "assignment-future", "summary": "Project", "status": "needs_action", "due": "2026-09-28"},
            {"uid": "assignment-overdue", "summary": "Essay", "description": "Write the essay", "subject": "English", "status": "needs_action", "due": "2026-09-24"},
            {"uid": "assignment-done", "summary": "Submitted", "status": "completed", "due": "2026-09-25"},
            {"uid": "assignment-today", "summary": "Worksheet", "description": "Practice problem 4", "status": "needs_action", "due": "2026-09-25"},
        ],
        homework=[
            {"uid": "homework-next", "summary": "Read chapter", "status": "needs_action", "due": "2026-09-25T11:00:00+02:00"}
        ],
        max_sidebar_items=5,
    )

    assert [item.id for item in model.sidebar] == [
        "cancel:cancel-today",
        "cancel:cancel-tomorrow",
        "assignment:assignment-overdue",
        "assignment:assignment-today",
        "assignment:assignment-future",
    ]
    assert [item.priority for item in model.sidebar] == [0, 0, 1, 1, 1]
    assert [item.kind for item in model.sidebar] == [
        "cancellation",
        "cancellation",
        "assignment",
        "assignment",
        "assignment",
    ]
    assert model.sidebar[2].subtitle == "English"
    assert model.sidebar[3].subtitle == "Practice problem 4"


def test_events_overlapping_midnight_are_visible_on_each_affected_day():
    model = build_display_model(
        now=datetime(2026, 9, 25, 12, 0, tzinfo=TZ),
        private_events=[
            {
                "summary": "Overnight event",
                "start": "2026-09-25T23:00:00+02:00",
                "end": "2026-09-26T01:00:00+02:00",
            }
        ],
    )

    assert [len(day.events) for day in model.days] == [1, 1, 0]
    assert model.days[0].events[0].id == model.days[1].events[0].id


def test_repeated_hour_events_are_valid_and_sorted_by_absolute_time():
    model = build_display_model(
        now=datetime(2026, 10, 25, 0, 30, tzinfo=TZ),
        lectio_events=[
            {
                "uid": "before-fallback",
                "summary": "Before fallback",
                "start": "2026-10-25T02:45:00+02:00",
                "end": "2026-10-25T02:15:00+01:00",
            },
            {
                "uid": "after-fallback",
                "summary": "After fallback",
                "start": "2026-10-25T02:10:00+01:00",
                "end": "2026-10-25T02:40:00+01:00",
            },
        ],
    )

    events = model.days[0].events
    assert [event.title for event in events] == [
        "Before fallback",
        "After fallback",
    ]
    assert events[0].start.astimezone(timezone.utc) < events[0].end.astimezone(
        timezone.utc
    )


def test_homework_shows_instructions_and_skips_past_lessons():
    model = build_display_model(
        now=datetime(2026, 9, 25, 12, 0, tzinfo=TZ),
        homework=[
            {
                "uid": "yesterday",
                "summary": "History",
                "description": "Past homework",
                "due": "2026-09-24T10:00:00+02:00",
            },
            {
                "uid": "earlier-today",
                "summary": "Math",
                "description": "Already had this lesson",
                "due": "2026-09-25T10:00:00+02:00",
            },
            {
                "uid": "next-lesson",
                "summary": "Math",
                "description": "Read pages 20–30",
                "due": "2026-09-25T13:00:00+02:00",
            },
            {
                "uid": "tomorrow",
                "summary": "Danish",
                "description": "Prepare a short presentation",
                "due": "2026-09-26T09:00:00+02:00",
            },
        ],
        max_sidebar_items=2,
    )

    assert [item.id for item in model.sidebar] == [
        "homework:next-lesson",
        "homework:tomorrow",
    ]
    assert model.sidebar[0].title == "Math"
    assert model.sidebar[0].subtitle == "Read pages 20–30"
