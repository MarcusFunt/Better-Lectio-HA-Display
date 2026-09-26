from datetime import datetime, timedelta, timezone

from homeassistant.components.calendar import CalendarEvent
from homeassistant.components.todo import TodoItemStatus

from custom_components.better_lectio.calendar import calendar_event_from_lesson
from custom_components.better_lectio.todo import (
    assignment_to_todo,
    homework_to_todo,
)


def test_calendar_event_maps_teacher_room_and_stable_uid():
    lesson = {
        "id": "lesson-123",
        "start": "2026-09-25T08:15:00+02:00",
        "end": "2026-09-25T09:00:00+02:00",
        "subject": "Matematik",
        "teacher": "AB",
        "room": "2.27",
        "status": "normal",
        "details": "Kapitel 4",
    }

    event = calendar_event_from_lesson(lesson)

    assert isinstance(event, CalendarEvent)
    assert event.uid == "lesson-123"
    assert event.summary == "Matematik"
    assert event.location == "2.27"
    assert "AB" in event.description
    assert "Kapitel 4" in event.description
    assert event.start.astimezone(timezone.utc) == datetime(
        2026, 9, 25, 6, 15, tzinfo=timezone.utc
    )
    assert event.start.utcoffset() == timedelta(hours=2)


def test_assignment_todo_preserves_due_and_completion_state():
    item = assignment_to_todo(
        {
            "id": "assignment-1",
            "title": "Aflevering",
            "description": "Skriv rapport",
            "due": "2026-09-30T20:00:00+02:00",
            "subject": "Dansk",
            "status": "completed",
            "source_url": "https://www.lectio.dk/assignment/1",
        }
    )

    assert item.uid == "assignment-1"
    assert item.summary == "Aflevering"
    assert item.status is TodoItemStatus.COMPLETED
    assert item.due == datetime(2026, 9, 30, 18, tzinfo=timezone.utc)
    assert "Dansk" in item.description


def test_homework_todo_uses_target_lesson_as_due():
    item = homework_to_todo(
        {
            "id": "homework-1",
            "subject": "Fysik",
            "description": "Læs afsnit 2",
            "target_lesson_start": "2026-09-28T10:30:00+02:00",
        }
    )

    assert item.uid == "homework-1"
    assert item.summary == "Fysik"
    assert item.description == "Læs afsnit 2"
    assert item.status is TodoItemStatus.NEEDS_ACTION
    assert item.due == datetime(2026, 9, 28, 8, 30, tzinfo=timezone.utc)
