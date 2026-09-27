from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from display_service.model_builder import build_display_model, display_window

TZ = ZoneInfo("Europe/Copenhagen")


def test_display_window_spans_three_copenhagen_days_across_dst():
    start, end, days = display_window(datetime(2026, 3, 28, 12, tzinfo=TZ))
    assert days == (date(2026, 3, 28), date(2026, 3, 29), date(2026, 3, 30))
    assert start.isoformat() == "2026-03-28T00:00:00+01:00"
    assert end.isoformat() == "2026-03-31T00:00:00+02:00"
    assert (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() == 71 * 3600


def test_lessons_use_normalized_lectio_subject_and_are_sorted_across_days():
    model = build_display_model(
        now=datetime(2026, 9, 25, 7, tzinfo=TZ),
        lectio_events=[
            {"id": "late", "subject": "History", "teacher": "AB", "room": "1.2", "start": "2026-09-26T09:00:00+02:00", "end": "2026-09-26T09:45:00+02:00"},
            {"id": "second", "subject": "Danish", "start": "2026-09-25T10:00:00+02:00", "end": "2026-09-25T10:45:00+02:00"},
            {"id": "first", "subject": "Math", "start": "2026-09-25T08:00:00+02:00", "end": "2026-09-25T08:45:00+02:00"},
        ],
    )
    assert [[event.title for event in day.events] for day in model.days] == [["Math", "Danish"], ["History"], []]
    assert model.days[1].events[0].id == "lectio:late"
    assert (model.days[1].events[0].teacher, model.days[1].events[0].room) == ("AB", "1.2")


def test_sidebar_prioritizes_cancellations_then_assignments_then_upcoming_homework():
    model = build_display_model(
        now=datetime(2026, 9, 25, 12, tzinfo=TZ),
        cancellations=[{"id": "cancel", "subject": "History", "reason": "Teacher absent", "start": "2026-09-25T14:00:00+02:00"}],
        assignments=[{"id": "essay", "title": "Essay", "subject": "English", "due": "2026-09-26T16:00:00+02:00", "status": "open"},
                     {"id": "done", "title": "Done", "status": "completed"},
                     {"id": "submitted", "title": "Submitted", "status": "Afleveret"}],
        homework=[{"id": "old", "subject": "Math", "description": "Past", "target_lesson_start": "2026-09-25T10:00:00+02:00"},
                  {"id": "read", "subject": "Math", "description": "Read pages 20–30", "target_lesson_start": "2026-09-25T13:00:00+02:00"}],
    )
    assert [item.id for item in model.sidebar] == ["cancel:cancel", "assignment:essay", "homework:read"]
    assert model.sidebar[-1].title == "Math"
    assert model.sidebar[-1].subtitle == "Read pages 20–30"


def test_overnight_lessons_appear_on_both_visible_days():
    model = build_display_model(
        now=datetime(2026, 9, 25, 12, tzinfo=TZ),
        lectio_events=[{"id": "overnight", "subject": "Study", "start": "2026-09-25T23:00:00+02:00", "end": "2026-09-26T01:00:00+02:00"}],
    )
    assert [len(day.events) for day in model.days] == [1, 1, 0]
    assert model.days[0].events[0].id == model.days[1].events[0].id


def test_cancelled_schedule_lesson_is_only_shown_as_a_cancellation():
    model = build_display_model(
        now=datetime(2026, 9, 25, 7, tzinfo=TZ),
        lectio_events=[
            {"id": "cancelled-lesson", "subject": "History", "status": "cancelled", "start": "2026-09-25T09:00:00+02:00", "end": "2026-09-25T09:45:00+02:00"},
            {"id": "active-lesson", "subject": "Math", "status": "normal", "start": "2026-09-25T10:00:00+02:00", "end": "2026-09-25T10:45:00+02:00"},
        ],
        cancellations=[{"id": "cancelled-lesson", "subject": "History", "start": "2026-09-25T09:00:00+02:00"}],
    )
    assert [event.title for event in model.days[0].events] == ["Math"]
    assert [item.title for item in model.sidebar] == ["History"]
