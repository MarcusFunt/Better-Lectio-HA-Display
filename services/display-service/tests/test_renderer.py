from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO

import display_service.renderer as renderer
from display_service.model import DisplayDay, DisplayEvent, DisplayModel, SidebarItem
from PIL import Image, ImageChops


def _model(title: str = "Mathematics") -> DisplayModel:
    today = date(2026, 9, 25)
    event = DisplayEvent(
        id="lesson-1",
        start=datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 25, 8, 45, tzinfo=timezone.utc),
        title=title,
        source="lectio",
        all_day=False,
        teacher="AB",
        room="2.27",
    )
    return DisplayModel(
        generated_at=datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc),
        days=(
            DisplayDay(date=today, events=(event,)),
            DisplayDay(date=date(2026, 9, 26), events=()),
            DisplayDay(date=date(2026, 9, 27), events=()),
        ),
        sidebar=(),
    )


def _render_display():
    render = getattr(renderer, "render_display", None)
    assert callable(render), "renderer.render_display must return a hashed BMP artifact"
    return render


def test_render_display_returns_hashed_mono_bmp_artifact():
    artifact = _render_display()(_model())

    assert isinstance(artifact.bmp, bytes)
    digest = sha256(artifact.bmp).hexdigest()
    assert artifact.content_hash == digest
    assert artifact.filename == f"{digest}.bmp"
    with Image.open(BytesIO(artifact.bmp)) as image:
        assert image.format == "BMP"
        assert image.size == (800, 480)
        assert image.mode == "1"


def test_render_display_is_deterministic_and_hashes_visible_changes():
    render = _render_display()

    first = render(_model("Mathematics"))
    repeated = render(_model("Mathematics"))
    changed = render(_model("History"))

    assert repeated.bmp == first.bmp
    assert repeated.content_hash == first.content_hash
    assert changed.bmp != first.bmp
    assert changed.content_hash != first.content_hash


def test_schedule_sidebar_layout_keeps_the_approved_gutter_clear():
    artifact = _render_display()(_model())
    with Image.open(BytesIO(artifact.bmp)) as image:
        assert all(image.getpixel((646, y)) == 0 for y in range(60, 460))
        assert image.getpixel((646, 459)) == 0
        assert image.getpixel((646, 460)) == 255
        assert all(image.getpixel((647, y)) == 255 for y in range(60, 460))
        assert all(image.getpixel((659, y)) == 255 for y in range(60, 460))


def test_dense_day_keeps_event_times_and_shows_overflow_count():
    model = _model()
    base_event = model.days[0].events[0]
    events = tuple(
        replace(
            base_event,
            id=f"lesson-{index}",
            start=datetime(2026, 9, 25, 8 + index, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 25, 8 + index, 45, tzinfo=timezone.utc),
            title=f"Lesson {index}",
        )
        for index in range(4)
    )
    dense_day = replace(model.days[0], events=events)
    dense_model = replace(model, days=(dense_day, *model.days[1:]))
    artifact = _render_display()(dense_model)

    with Image.open(BytesIO(artifact.bmp)) as image:
        assert _contains_ink(image, (26, 88, 132, 109))
        assert _contains_ink(image, (132, 150, 646, 180))


def test_long_title_does_not_remove_teacher_and_room_details():
    model = _model()
    event = replace(
        model.days[0].events[0],
        title="A very long subject title " * 20,
        teacher="AB",
        room="2.27",
    )
    day = replace(model.days[0], events=(event,))
    artifact = _render_display()(replace(model, days=(day, *model.days[1:])))

    with Image.open(BytesIO(artifact.bmp)) as image:
        assert _contains_ink(image, (26, 88, 132, 109))
        assert _contains_ink(image, (132, 104, 646, 128))


def test_all_day_and_timed_events_render_when_optional_details_are_missing():
    model = _model()
    timed_event = replace(
        model.days[0].events[0],
        id="private-event",
        start=datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
        title="Appointment",
        source="private",
        teacher=None,
        room=None,
    )
    all_day_event = replace(
        model.days[0].events[0],
        id="all-day",
        start=date(2026, 9, 25),
        end=date(2026, 9, 26),
        title="School holiday",
        all_day=True,
        teacher=None,
        room=None,
    )
    day = replace(model.days[0], events=(all_day_event, timed_event))
    artifact = _render_display()(replace(model, days=(day, *model.days[1:])))

    with Image.open(BytesIO(artifact.bmp)) as image:
        assert _contains_ink(image, (26, 88, 132, 109))
        assert _contains_ink(image, (26, 123, 132, 144))
        assert not _contains_ink(image, (132, 140, 646, 160))


def test_multiword_teacher_and_room_are_abbreviated_in_schedule_details():
    model = _model()
    event = model.days[0].events[0]
    verbose_event = replace(
        event,
        teacher="Alexander Montgomery",
        room="Auditorium North Wing",
    )
    abbreviated_event = replace(
        event,
        teacher="A. Montgomery",
        room="A. N. Wing",
    )

    def with_event(updated_event):
        day = replace(model.days[0], events=(updated_event,))
        return replace(model, days=(day, *model.days[1:]))

    render = _render_display()
    verbose = render(with_event(verbose_event))
    abbreviated = render(with_event(abbreviated_event))

    assert verbose.bmp == abbreviated.bmp


def test_sidebar_indicates_overflow_when_lower_priority_rows_are_hidden():
    render = _render_display()
    twelve_items = render(_with_sidebar(_model(), _sidebar_items("cancellation", 0, 12)))
    thirteen_items = render(_with_sidebar(_model(), _sidebar_items("cancellation", 0, 13)))

    assert twelve_items.bmp != thirteen_items.bmp


def test_sidebar_keeps_cancellations_and_assignments_ahead_of_homework():
    items = (
        *_sidebar_items("cancellation", 0, 6),
        *_sidebar_items("assignment", 1, 6),
        *_sidebar_items("homework", 2, 6),
    )
    model = _with_sidebar(_model(), items)
    render = _render_display()
    original = render(model)
    changed_cancellation = render(
        _with_sidebar(model, (replace(items[0], title="Urgent cancellation"), *items[1:]))
    )
    changed_homework = render(
        _with_sidebar(model, (*items[:-1], replace(items[-1], title="Different hidden homework")))
    )

    assert changed_cancellation.bmp != original.bmp
    assert changed_homework.bmp == original.bmp


def test_homework_uses_due_date_when_it_has_no_subtitle():
    item = SidebarItem(
        id="homework-1",
        kind="homework",
        priority=2,
        title="Read chapter",
        subtitle=None,
        when=date(2026, 9, 26),
        source="lectio",
    )
    next_day = replace(item, when=date(2026, 9, 27))
    render = _render_display()

    assert render(_with_sidebar(_model(), (item,))).bmp != render(
        _with_sidebar(_model(), (next_day,))
    ).bmp


def _contains_ink(image: Image.Image, box: tuple[int, int, int, int]) -> bool:
    region = image.crop(box).convert("L")
    return ImageChops.invert(region).getbbox() is not None


def _sidebar_items(kind: str, priority: int, count: int) -> tuple[SidebarItem, ...]:
    return tuple(
        SidebarItem(
            id=f"{kind}-{index}",
            kind=kind,
            priority=priority,
            title=f"{kind.title()} {index}",
            subtitle="Short detail",
            when=None,
            source="test",
        )
        for index in range(count)
    )


def _with_sidebar(model: DisplayModel, items: tuple[SidebarItem, ...]) -> DisplayModel:
    return replace(model, sidebar=items)
