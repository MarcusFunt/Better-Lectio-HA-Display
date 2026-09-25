from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

import requests
from dateutil.rrule import rruleset, rrulestr
from icalendar import Calendar


@dataclass(frozen=True)
class Event:
    title: str
    starts_at: datetime
    ends_at: datetime
    source: str
    location: str = ""
    details: str = ""
    all_day: bool = False
    uid: str = ""


def _as_local_datetime(value, timezone):
    if isinstance(value, datetime):
        return (value.replace(tzinfo=timezone) if value.tzinfo is None else value.astimezone(timezone)), False
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone), True
    raise ValueError(f"Unsupported iCalendar date value: {value!r}")


def _as_recurrence_datetime(value):
    if isinstance(value, datetime):
        return value, False
    if isinstance(value, date):
        return datetime.combine(value, time.min), True
    raise ValueError(f"Unsupported iCalendar date value: {value!r}")


def _is_floating(value):
    return isinstance(value, datetime) and value.tzinfo is None


def _window_datetime(value, timezone, fallback):
    return fallback if value is None else _as_local_datetime(value, timezone)[0]


def _listed_dates(component, name):
    value = component.get(name)
    if value is None:
        return []
    properties = value if isinstance(value, list) else [value]
    output = []
    for prop in properties:
        output.extend(_as_recurrence_datetime(item.dt)[0] for item in getattr(prop, "dts", [prop]))
    return output


def _duration(start, end, all_day, floating=False):
    if all_day or floating:
        return end - start
    return end.astimezone(dt_timezone.utc) - start.astimezone(dt_timezone.utc)


def _add_duration(start, duration, all_day, floating=False):
    if all_day or floating:
        return start + duration
    return (start.astimezone(dt_timezone.utc) + duration).astimezone(start.tzinfo)


def _display_datetime(value, timezone, all_day, floating=False):
    return value.replace(tzinfo=timezone) if all_day or floating else value.astimezone(timezone)


def _parse_component(component, timezone, window_start, window_end, *, excluded=(), fallback=None, expand=True):
    if str(component.get("STATUS", "")).upper() == "CANCELLED":
        return []
    start_prop = component.get("DTSTART") or component.get("RECURRENCE-ID")
    if start_prop is None:
        return []
    start, all_day = _as_recurrence_datetime(start_prop.dt)
    floating = _is_floating(start_prop.dt)

    end_prop = component.get("DTEND")
    if end_prop is not None:
        end, _ = _as_recurrence_datetime(end_prop.dt)
        duration = _duration(start, end, all_day, floating)
    elif component.get("DURATION") is not None:
        duration = component.get("DURATION").dt
    elif fallback is not None:
        master_start_prop = fallback.get("DTSTART")
        master_end_prop = fallback.get("DTEND")
        if master_start_prop is not None and master_end_prop is not None:
            master_start, master_all_day = _as_recurrence_datetime(master_start_prop.dt)
            master_end, _ = _as_recurrence_datetime(master_end_prop.dt)
            duration = _duration(master_start, master_end, master_all_day, _is_floating(master_start_prop.dt))
        elif fallback.get("DURATION") is not None:
            duration = fallback.get("DURATION").dt
        else:
            duration = timedelta(days=1) if all_day else timedelta(minutes=30)
    else:
        duration = timedelta(days=1) if all_day else timedelta(minutes=30)

    if not expand:
        starts = [start]
    else:
        rule_prop = component.get("RRULE")
        rdates = _listed_dates(component, "RDATE")
        exdates = _listed_dates(component, "EXDATE") + list(excluded)
        if rule_prop is None and not rdates and not exdates:
            starts = [start]
        else:
            instances = rruleset()
            instances.rdate(start)
            if rule_prop is not None:
                instances.rrule(rrulestr(rule_prop.to_ical().decode("utf-8"), dtstart=start))
            for item in rdates:
                instances.rdate(item)
            for item in exdates:
                instances.exdate(item)
            if all_day or floating:
                query_start = window_start.replace(tzinfo=None) - duration
                query_end = window_end.replace(tzinfo=None)
            else:
                query_start = (window_start.astimezone(dt_timezone.utc) - duration).astimezone(start.tzinfo)
                query_end = window_end.astimezone(start.tzinfo)
            starts = instances.between(query_start, query_end, inc=True)

    def prop(name):
        return component.get(name) if component.get(name) is not None else (fallback.get(name) if fallback else None)

    summary = prop("SUMMARY")
    title = str(summary or "Untitled event").strip() or "Untitled event"
    events = []
    for instance_start in starts:
        instance_end = _add_duration(instance_start, duration, all_day, floating)
        display_start = _display_datetime(instance_start, timezone, all_day, floating)
        display_end = _display_datetime(instance_end, timezone, all_day, floating)
        if display_start < window_end and display_end > window_start:
            events.append(Event(
                title=title,
                starts_at=display_start,
                ends_at=display_end,
                source="Calendar",
                location=str(prop("LOCATION") or "").strip(),
                details=str(prop("DESCRIPTION") or "").strip(),
                all_day=all_day,
                uid=str(component.get("UID", "")),
            ))
    return events


def parse_ics(content, timezone_name="Europe/Copenhagen", window_start=None, window_end=None):
    timezone = ZoneInfo(timezone_name)
    now = datetime.now(timezone)
    window_start = _window_datetime(window_start, timezone, now - timedelta(days=1))
    window_end = _window_datetime(window_end, timezone, now + timedelta(days=30))
    components = list(Calendar.from_ical(content).walk("VEVENT"))
    masters, overrides, orphaned = [], {}, []
    for component in components:
        recurrence_id = component.get("RECURRENCE-ID")
        uid = str(component.get("UID", ""))
        if recurrence_id is None:
            masters.append(component)
        elif uid:
            overrides.setdefault(uid, []).append(component)
        else:
            orphaned.append(component)
    events, handled = [], set()
    for master in masters:
        uid = str(master.get("UID", ""))
        series = overrides.get(uid, [])
        exclusions = [_as_recurrence_datetime(item.get("RECURRENCE-ID").dt)[0] for item in series]
        events.extend(_parse_component(master, timezone, window_start, window_end, excluded=exclusions))
        if str(master.get("STATUS", "")).upper() == "CANCELLED":
            handled.update(id(item) for item in series)
            continue
        for override in series:
            handled.add(id(override))
            events.extend(_parse_component(
                override, timezone, window_start, window_end, fallback=master, expand=False,
            ))
    for item in orphaned:
        events.extend(_parse_component(item, timezone, window_start, window_end, expand=False))
    for series in overrides.values():
        for item in series:
            if id(item) not in handled:
                events.extend(_parse_component(item, timezone, window_start, window_end, expand=False))
    return sorted(events, key=lambda event: event.starts_at)


def fetch_ics_events(url, timezone_name="Europe/Copenhagen"):
    if not url:
        return []
    try:
        response = requests.get(url, timeout=(5, 20))
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError(f"Calendar feed request failed ({type(error).__name__})") from None
    return parse_ics(response.content, timezone_name)
