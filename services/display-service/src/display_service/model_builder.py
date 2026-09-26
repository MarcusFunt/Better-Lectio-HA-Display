"""Pure normalization and prioritization for the renderer input model."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .model import DisplayDay, DisplayEvent, DisplayModel, DisplayTime, SidebarItem

DISPLAY_TIMEZONE = ZoneInfo("Europe/Copenhagen")


def display_window(
    now: datetime | None = None,
) -> tuple[datetime, datetime, tuple[date, date, date]]:
    """Return Copenhagen midnight bounds and the three visible local dates."""
    local_now = _local_datetime(now or datetime.now(DISPLAY_TIMEZONE))
    today = local_now.date()
    days = (today, today + timedelta(days=1), today + timedelta(days=2))
    start = datetime.combine(days[0], time.min, tzinfo=DISPLAY_TIMEZONE)
    end = datetime.combine(days[-1] + timedelta(days=1), time.min, tzinfo=DISPLAY_TIMEZONE)
    return start, end, days


def build_display_model(
    *,
    now: datetime | None = None,
    lectio_events: Sequence[Mapping[str, Any]] = (),
    private_events: Sequence[Mapping[str, Any]] = (),
    assignments: Sequence[Mapping[str, Any]] = (),
    homework: Sequence[Mapping[str, Any]] = (),
    cancellations: Sequence[Mapping[str, Any]] = (),
    max_sidebar_items: int = 8,
) -> DisplayModel:
    """Create a stable three-day model from normalized HA response data."""
    if max_sidebar_items < 0:
        raise ValueError("max_sidebar_items cannot be negative")
    local_now = _local_datetime(now or datetime.now(DISPLAY_TIMEZONE))
    _, _, days = display_window(local_now)
    event_by_day: dict[date, list[DisplayEvent]] = {day: [] for day in days}
    all_events = [
        *(_calendar_event(item, "lectio") for item in lectio_events),
        *(_calendar_event(item, "private") for item in private_events),
    ]
    for event in all_events:
        if event is None:
            continue
        for day in days:
            if _overlaps_day(event, day):
                event_by_day[day].append(event)

    sidebar = _build_sidebar(
        assignments=assignments,
        homework=homework,
        cancellations=cancellations,
        days=days,
        now=local_now,
        max_items=max_sidebar_items,
    )
    return DisplayModel(
        generated_at=local_now,
        days=tuple(
            DisplayDay(
                date=day,
                events=tuple(sorted(event_by_day[day], key=_event_sort_key)),
            )
            for day in days
        ),
        sidebar=tuple(sidebar),
    )


def _calendar_event(
    item: Mapping[str, Any], source: str
) -> DisplayEvent | None:
    start = _parse_time(item.get("start"))
    end = _parse_time(item.get("end"))
    if start is None or end is None or type(start) is not type(end):
        return None
    if _instant(end) <= _instant(start):
        return None

    title = _text(item.get("summary")) or "Untitled event"
    description = _text(item.get("description"))
    teacher = _text(item.get("teacher")) or _teacher_from_description(description)
    room = _text(item.get("room")) or _text(item.get("location"))
    source_id = _text(item.get("uid")) or _text(item.get("id"))
    if source_id is None:
        identity = "\x1f".join(
            (
                _text(item.get("_display_calendar_entity_id")) or "",
                source,
                title,
                _temporal_key(start),
                _temporal_key(end),
                description or "",
                room or "",
            )
        )
        source_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    calendar_entity_id = _text(item.get("_display_calendar_entity_id"))
    stable_id = (
        f"{source}:{calendar_entity_id}:{source_id}"
        if calendar_entity_id
        else f"{source}:{source_id}"
    )
    return DisplayEvent(
        id=stable_id,
        start=start,
        end=end,
        title=title,
        source=source,  # type: ignore[arg-type]
        all_day=isinstance(start, date) and not isinstance(start, datetime),
        teacher=teacher,
        room=room,
    )


def _build_sidebar(
    *,
    assignments: Sequence[Mapping[str, Any]],
    homework: Sequence[Mapping[str, Any]],
    cancellations: Sequence[Mapping[str, Any]],
    days: tuple[date, date, date],
    now: datetime,
    max_items: int,
) -> list[SidebarItem]:
    candidates: list[tuple[tuple[Any, ...], SidebarItem]] = []
    for item in cancellations:
        cancellation = _cancellation_item(item, days)
        if cancellation is not None:
            candidates.append(((_time_sort_key(cancellation.when), cancellation.title.casefold(), cancellation.id), cancellation))

    for item in assignments:
        if _is_completed(item):
            continue
        sidebar_item = _todo_sidebar_item(item, kind="assignment")
        if sidebar_item is not None:
            candidates.append(((_time_sort_key(sidebar_item.when), sidebar_item.title.casefold(), sidebar_item.id), sidebar_item))

    for item in homework:
        if _is_completed(item):
            continue
        sidebar_item = _homework_sidebar_item(item, now=now, days=days)
        if sidebar_item is not None:
            candidates.append(((_time_sort_key(sidebar_item.when), sidebar_item.title.casefold(), sidebar_item.id), sidebar_item))

    candidates.sort(key=lambda candidate: (candidate[1].priority, *candidate[0]))
    return [item for _, item in candidates[:max_items]]


def _cancellation_item(
    item: Mapping[str, Any], days: tuple[date, date, date]
) -> SidebarItem | None:
    when = _parse_time(item.get("start"))
    if when is None or _as_datetime(when).date() not in days:
        return None
    original = item.get("original_lesson")
    if not isinstance(original, Mapping):
        original = {}
    title = (
        _text(item.get("subject"))
        or _text(original.get("subject"))
        or "Class cancelled"
    )
    subtitle = _text(item.get("reason")) or _text(item.get("details"))
    room = _text(item.get("room")) or _text(original.get("room"))
    if room:
        subtitle = f"{subtitle} · {room}" if subtitle else room
    source_id = _source_id(item)
    return SidebarItem(
        id=f"cancel:{source_id}",
        kind="cancellation",
        priority=0,
        title=title,
        subtitle=subtitle,
        when=when,
        source="cancellations",
    )


def _todo_sidebar_item(
    item: Mapping[str, Any], *, kind: str
) -> SidebarItem | None:
    when = _parse_time(item.get("due"))
    title = (
        _text(item.get("summary"))
        or _text(item.get("title"))
        or _text(item.get("subject"))
        or _text(item.get("description"))
    )
    if title is None:
        return None
    uid = _source_id(item)
    if kind == "assignment":
        subtitle = _text(item.get("subject")) or _text(item.get("description"))
        priority = 1
        source = "assignments"
    else:
        subtitle = _text(item.get("subject")) or _text(item.get("description"))
        priority = 2
        source = "homework"
    return SidebarItem(
        id=f"{kind}:{uid}",
        kind=kind,  # type: ignore[arg-type]
        priority=priority,
        title=title,
        subtitle=subtitle,
        when=when,
        source=source,
    )


def _homework_sidebar_item(
    item: Mapping[str, Any], *, now: datetime, days: tuple[date, date, date]
) -> SidebarItem | None:
    """Keep only homework for a remaining lesson in the visible three-day window."""
    sidebar_item = _todo_sidebar_item(item, kind="homework")
    when = sidebar_item.when if sidebar_item is not None else None
    if when is None or _as_datetime(when).date() not in days:
        return None
    if isinstance(when, datetime) and _instant(when) < _instant(now):
        return None
    return sidebar_item


def _is_completed(item: Mapping[str, Any]) -> bool:
    status = _text(item.get("status"))
    return status is not None and status.casefold() in {"completed", "done"}


def _source_id(item: Mapping[str, Any]) -> str:
    identifier = _text(item.get("uid")) or _text(item.get("id")) or _text(
        item.get("source_id")
    )
    if identifier is not None:
        return identifier
    stable_fields = "\x1f".join(
        str(item.get(key) or "")
        for key in ("summary", "title", "subject", "description", "due", "start")
    )
    return hashlib.sha256(stable_fields.encode("utf-8")).hexdigest()[:20]


def _teacher_from_description(description: str | None) -> str | None:
    if description is None:
        return None
    match = re.search(r"(?:^|\n)Teacher:\s*([^\n]+)", description, re.IGNORECASE)
    return _text(match.group(1)) if match else None


def _parse_time(value: Any) -> DisplayTime | None:
    if isinstance(value, datetime):
        return _local_datetime(value)
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    try:
        parsed_datetime = datetime.fromisoformat(value)
    except ValueError:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return _local_datetime(parsed_datetime)


def _local_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=DISPLAY_TIMEZONE)
    return value.astimezone(DISPLAY_TIMEZONE)


def _as_datetime(value: DisplayTime) -> datetime:
    if isinstance(value, datetime):
        return _local_datetime(value)
    return datetime.combine(value, time.min, tzinfo=DISPLAY_TIMEZONE)


def _temporal_key(value: DisplayTime) -> str:
    return value.isoformat()


def _overlaps_day(event: DisplayEvent, day: date) -> bool:
    day_start = datetime.combine(day, time.min, tzinfo=DISPLAY_TIMEZONE)
    day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=DISPLAY_TIMEZONE)
    return _instant(event.start) < _instant(day_end) and _instant(event.end) > _instant(day_start)


def _event_sort_key(event: DisplayEvent) -> tuple[datetime, int, str, str, str]:
    return (
        _instant(event.start),
        0 if event.all_day else 1,
        event.title.casefold(),
        event.source,
        event.id,
    )


def _time_sort_key(value: DisplayTime | None) -> tuple[int, datetime]:
    if value is None:
        return (1, datetime.max.replace(tzinfo=timezone.utc))
    return (0, _instant(value))


def _instant(value: DisplayTime) -> datetime:
    """Represent a display time as an absolute UTC instant for ordering."""
    return _as_datetime(value).astimezone(timezone.utc)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
