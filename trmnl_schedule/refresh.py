import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .events import Event, fetch_ics_events
from .lectio import fetch_week
from .rendering import render_schedule
from .server import load_settings


def _fingerprint(*values):
    data = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _read_cache(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _serialize(events):
    return [{
        "title": event.title, "starts_at": event.starts_at.isoformat(),
        "ends_at": event.ends_at.isoformat(), "source": event.source,
        "location": event.location, "details": event.details,
        "all_day": event.all_day, "uid": event.uid,
    } for event in events]


def _deserialize(items, timezone):
    events = []
    for item in items:
        events.append(Event(
            title=item["title"],
            starts_at=datetime.fromisoformat(item["starts_at"]).astimezone(timezone),
            ends_at=datetime.fromisoformat(item["ends_at"]).astimezone(timezone),
            source=item["source"], location=item.get("location", ""),
            details=item.get("details", ""), all_day=bool(item.get("all_day", False)),
            uid=item.get("uid", ""),
        ))
    return events


def _cache_is_fresh(entry, fingerprint, now, ttl):
    if not isinstance(entry, dict) or entry.get("fingerprint") != fingerprint:
        return False
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"]).astimezone(now.tzinfo)
        age = (now - fetched).total_seconds()
        return 0 <= age < ttl
    except (KeyError, TypeError, ValueError):
        return False


def _store_cache(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _source_events(cache, name, fingerprint, now, ttl, timezone, fetch):
    entry = cache.get(name)
    if _cache_is_fresh(entry, fingerprint, now, ttl):
        try:
            return _deserialize(entry["events"], timezone), False
        except (KeyError, TypeError, ValueError):
            pass
    events = list(fetch())
    cache[name] = {"fingerprint": fingerprint, "fetched_at": now.isoformat(), "events": _serialize(events)}
    return events, True


def run_refresh(config=None, now=None, lectio_fetcher=fetch_week, calendar_fetcher=fetch_ics_events):
    settings = load_settings(config)
    timezone = ZoneInfo(settings["TIMEZONE"])
    now = now or datetime.now(timezone)
    now = now.replace(tzinfo=timezone) if now.tzinfo is None else now.astimezone(timezone)
    events = []
    cache = _read_cache(settings["CACHE_PATH"])
    changed = False
    credentials = (settings["LECTIO_USERNAME"], settings["LECTIO_PASSWORD"], settings["LECTIO_SCHOOL_ID"])
    if any(credentials) and not all(credentials):
        raise ValueError("Set all three Lectio settings, or leave all three blank")
    if all(credentials):
        dates = (now.date(), now.date() + timedelta(days=1))
        weeks = sorted({(item.isocalendar().year, item.isocalendar().week) for item in dates})
        identity = _fingerprint(*credentials, weeks)

        def fetch_lectio():
            result = []
            for year, week in weeks:
                result.extend(lectio_fetcher(
                    username=settings["LECTIO_USERNAME"], password=settings["LECTIO_PASSWORD"],
                    school_id=settings["LECTIO_SCHOOL_ID"], iso_year=year, iso_week=week,
                    timezone_name=settings["TIMEZONE"],
                ))
            return result

        source, refreshed = _source_events(
            cache, "lectio", identity, now, settings["LECTIO_CACHE_TTL_SECONDS"], timezone, fetch_lectio,
        )
        events.extend(source)
        changed |= refreshed
    if settings["CALENDAR_ICS_URL"]:
        identity = _fingerprint(settings["CALENDAR_ICS_URL"])
        source, refreshed = _source_events(
            cache, "calendar", identity, now, settings["CALENDAR_CACHE_TTL_SECONDS"], timezone,
            lambda: calendar_fetcher(settings["CALENDAR_ICS_URL"], settings["TIMEZONE"]),
        )
        events.extend(source)
        changed |= refreshed
    if changed:
        _store_cache(settings["CACHE_PATH"], cache)
    return render_schedule(events, settings["IMAGE_PATH"], now, settings["TIMEZONE"])


def main():
    print(f"Updated TRMNL image: {run_refresh()}")


if __name__ == "__main__":
    main()
