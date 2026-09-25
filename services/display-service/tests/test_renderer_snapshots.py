from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from display_service.model import DisplayDay, DisplayEvent, DisplayModel, SidebarItem
from display_service.renderer import render_display
from PIL import Image, ImageChops

_TODAY = date(2026, 9, 25)
_DATES = (_TODAY, date(2026, 9, 26), date(2026, 9, 27))
_SNAPSHOTS = Path(__file__).with_name("snapshots")


@pytest.mark.parametrize(
    "scenario",
    (
        "normal",
        "dense",
        "no_events",
        "long_labels",
        "mixed_calendars",
        "cancellation_heavy",
        "assignment_heavy",
        "homework_fallback",
    ),
)
def test_renderer_matches_visual_snapshot(scenario: str):
    artifact = render_display(_scenarios()[scenario])
    snapshot_path = _SNAPSHOTS / f"{scenario}.png"

    assert snapshot_path.is_file(), f"Missing renderer snapshot: {snapshot_path.name}"
    with Image.open(snapshot_path) as expected, Image.open(BytesIO(artifact.bmp)) as actual:
        assert expected.size == (800, 480)
        assert expected.mode == "1"
        difference = ImageChops.difference(expected.convert("L"), actual.convert("L"))
        assert difference.getbbox() is None, f"Renderer changed the {scenario} snapshot"


def _scenarios() -> dict[str, DisplayModel]:
    return {
        "normal": _normal_model(),
        "dense": _dense_model(),
        "no_events": _model(),
        "long_labels": _long_label_model(),
        "mixed_calendars": _mixed_model(),
        "cancellation_heavy": _sidebar_model("cancellation"),
        "assignment_heavy": _sidebar_model("assignment"),
        "homework_fallback": _homework_model(),
    }


def _normal_model() -> DisplayModel:
    return _model(
        (
            _day(
                _DATES[0],
                _event("math", 8, 0, "Mathematics", teacher="A. Nielsen", room="1.65"),
                _event("danish", 10, 15, "Danish", teacher="Sofie Jensen", room="2.21"),
            ),
            _day(
                _DATES[1],
                _event("dentist", 14, 0, "Dentist", source="private", event_date=_DATES[1]),
            ),
            _day(
                _DATES[2],
                _event(
                    "exam",
                    0,
                    0,
                    "Study day",
                    all_day=True,
                    start=_DATES[2],
                    end=date(2026, 9, 28),
                ),
            ),
        ),
        (
            _item("cancellation", 0, "English", "Room 2.27", datetime(2026, 9, 25, 9, tzinfo=timezone.utc)),
            _item("assignment", 1, "SOP outline", "English", date(2026, 9, 28)),
            _item("homework", 2, "Read chapter 4", None, date(2026, 9, 26)),
        ),
    )


def _dense_model() -> DisplayModel:
    days = []
    for day_index, day_date in enumerate(_DATES):
        events = tuple(
            _event(
                f"dense-{day_index}-{event_index}",
                7 + event_index,
                0,
                f"Lesson {event_index + 1} with a longer subject name",
                teacher="M. Teacher",
                room="2.27",
                event_date=day_date,
            )
            for event_index in range(6)
        )
        days.append(_day(day_date, *events))
    return _model(tuple(days), ())


def _long_label_model() -> DisplayModel:
    return _model(
        (
            _day(
                _DATES[0],
                _event(
                    "long-label",
                    8,
                    0,
                    "An extraordinarily long subject title that must be shortened to fit the available schedule row " * 3,
                    teacher="Alexander Montgomery",
                    room="Auditorium North Wing",
                ),
            ),
            _day(_DATES[1]),
            _day(_DATES[2]),
        ),
        (
            _item(
                "assignment",
                1,
                "A very long assignment name that exceeds the narrow sidebar by a wide margin",
                "A long subtitle that must be fitted inside the same narrow column",
                date(2026, 9, 28),
            ),
        ),
    )


def _mixed_model() -> DisplayModel:
    return _model(
        (
            _day(
                _DATES[0],
                _event("private-1", 8, 0, "Dentist", source="private"),
                _event("lectio-1", 9, 0, "History", teacher="M. Jensen", room="3.12"),
                _event("private-2", 12, 30, "Family appointment", source="private"),
                _event("lectio-2", 14, 0, "Physics", teacher="A. Nielsen", room="1.65"),
            ),
            _day(_DATES[1]),
            _day(_DATES[2]),
        ),
        (),
    )


def _sidebar_model(kind: str) -> DisplayModel:
    if kind == "cancellation":
        items = tuple(
            _item(kind, 0, f"Cancelled class {index}", "Teacher unavailable", None)
            for index in range(15)
        ) + (
            _item("assignment", 1, "SOP draft", None, date(2026, 9, 29)),
            _item("homework", 2, "Read chapter", None, date(2026, 9, 27)),
        )
    else:
        items = (
            _item("cancellation", 0, "Math", "Room 1.65", None),
            *(
                _item(
                    "assignment",
                    1,
                    f"Assignment {index}",
                    "English",
                    date(2026, 9, 28) + timedelta(days=index),
                )
                for index in range(9)
            ),
            _item("homework", 2, "Read chapter", None, date(2026, 9, 26)),
        )
    return _model(tuple(_day(day_date) for day_date in _DATES), tuple(items))


def _homework_model() -> DisplayModel:
    return _model(
        tuple(_day(day_date) for day_date in _DATES),
        tuple(
            _item("homework", 2, title, None, due)
            for title, due in (
                ("Read the next chapter", _DATES[0]),
                ("Prepare vocabulary", _DATES[1]),
                ("Solve exercise 12", _DATES[2]),
            )
        ),
    )


def _model(
    days: tuple[DisplayDay, ...] | None = None,
    sidebar: tuple[SidebarItem, ...] = (),
) -> DisplayModel:
    return DisplayModel(
        generated_at=datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc),
        days=days or tuple(_day(day_date) for day_date in _DATES),
        sidebar=sidebar,
    )


def _day(day_date: date, *events: DisplayEvent) -> DisplayDay:
    return DisplayDay(date=day_date, events=tuple(events))


def _event(
    identifier: str,
    hour: int,
    minute: int,
    title: str,
    *,
    source: str = "lectio",
    teacher: str | None = None,
    room: str | None = None,
    all_day: bool = False,
    event_date: date = _TODAY,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
) -> DisplayEvent:
    start_value = start or datetime(
        event_date.year,
        event_date.month,
        event_date.day,
        hour,
        minute,
        tzinfo=timezone.utc,
    )
    if end is not None:
        end_value = end
    elif isinstance(start_value, datetime):
        end_value = start_value + timedelta(minutes=45)
    else:
        end_value = start_value + timedelta(days=1)
    return DisplayEvent(
        id=identifier,
        start=start_value,
        end=end_value,
        title=title,
        source=source,
        all_day=all_day,
        teacher=teacher,
        room=room,
    )


def _item(
    kind: str,
    priority: int,
    title: str,
    subtitle: str | None,
    when: date | datetime | None,
) -> SidebarItem:
    return SidebarItem(
        id=f"{kind}:{title}",
        kind=kind,
        priority=priority,
        title=title,
        subtitle=subtitle,
        when=when,
        source="test",
    )
