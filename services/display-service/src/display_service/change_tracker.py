"""Persist privacy-minimized changes since the display was last acknowledged."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from .model import DisplayEvent, DisplayModel, SidebarItem
from .model_service import SourceStatus

_ACTIVE_SOURCES = {"schedule", "assignments", "homework", "cancellations"}


@dataclass(frozen=True, slots=True)
class DisplayChangeSummary:
    added: int = 0
    changed: int = 0
    removed: int = 0
    acknowledged_at: str | None = None
    available: bool = True

    @property
    def total(self) -> int:
        return self.added + self.changed + self.removed


class DisplayChangeTracker:
    """Compare fresh Lectio data with a persistent, per-source acknowledged view.

    The state file stores one-way hashes of item identities and content, never
    event titles or descriptions. Stale sources are not compared, so cached or
    missing data cannot turn into false deletion notices.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            self._state = _load_state(path)
            self.available = True
        except ValueError:
            # Preserve an unreadable review file and do not silently establish
            # a new baseline that could hide pending changes.
            self._state = _empty_state()
            self.available = False
        for group in ("acknowledged", "current"):
            self._state[group] = {
                key: value
                for key, value in self._state[group].items()
                if key in _ACTIVE_SOURCES
            }
        self._fresh_sources: set[str] = set()

    @property
    def summary(self) -> DisplayChangeSummary:
        return self._summary()

    @property
    def can_acknowledge(self) -> bool:
        return self.available and bool(self._fresh_sources)

    def update(
        self,
        model: DisplayModel,
        sources: dict[str, SourceStatus],
    ) -> DisplayChangeSummary:
        if not self.available:
            raise RuntimeError("Plan review state is unavailable")
        current = _snapshot(model)
        fresh_sources = {
            entity_id
            for entity_id, status in sources.items()
            if status.state == "valid" and not status.is_stale
        }
        acknowledged = self._state["acknowledged"]
        stored_current = self._state["current"]
        today = model.days[0].date if model.days else model.generated_at.date()

        for entity_id in fresh_sources:
            entries = current.get(entity_id, {})
            if entity_id not in acknowledged:
                # The first trustworthy view establishes the starting point.
                acknowledged[entity_id] = entries
            else:
                acknowledged[entity_id] = {
                    key: item
                    for key, item in acknowledged[entity_id].items()
                    if not _is_expired(item, today)
                }
            stored_current[entity_id] = entries

        self._fresh_sources = fresh_sources
        self._write()
        return self._summary(today=today)

    def acknowledge(self) -> DisplayChangeSummary | None:
        """Accept only source snapshots confirmed fresh in the latest refresh."""
        if not self.available or not self._fresh_sources:
            return None
        for entity_id in self._fresh_sources:
            if entity_id in self._state["current"]:
                self._state["acknowledged"][entity_id] = self._state["current"][
                    entity_id
                ]
        self._state["acknowledged_at"] = datetime.now(timezone.utc).isoformat()
        self._write()
        return self._summary()

    def _summary(self, *, today: date | None = None) -> DisplayChangeSummary:
        counts = {"added": 0, "changed": 0, "removed": 0}
        baseline = self._state["acknowledged"]
        current = self._state["current"]
        for entity_id, current_items in current.items():
            if entity_id not in baseline:
                continue
            previous_items = baseline[entity_id]
            for key, item in current_items.items():
                previous = previous_items.get(key)
                if previous is None:
                    counts["added"] += 1
                elif item["fingerprint"] != previous["fingerprint"]:
                    counts["changed"] += 1
            for key, previous in previous_items.items():
                if key not in current_items and not _is_expired(
                    previous, today or date.min
                ):
                    counts["removed"] += 1
        return DisplayChangeSummary(
            **counts,
            acknowledged_at=self._state["acknowledged_at"],
            available=self.available,
        )

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=".plan-review.", suffix=".tmp"
        )
        temporary_path = Path(temporary_name)
        try:
            os.chmod(temporary_path, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(self._state, file, separators=(",", ":"), sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self._path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _snapshot(model: DisplayModel) -> dict[str, dict[str, dict[str, str | bool | None]]]:
    result: dict[str, dict[str, dict[str, str | bool | None]]] = {}
    for day in model.days:
        for event in day.events:
            if event.source == "lectio":
                _add_item(
                    result,
                    "schedule",
                    event.id,
                    _event_fingerprint(event),
                    _event_expiry(event),
                    expires_inclusive=(
                        (
                            not isinstance(event.start, datetime)
                            and not isinstance(event.end, datetime)
                        )
                        or (
                            isinstance(event.end, datetime)
                            and event.end.time() == time.min
                        )
                    ),
                )
    for item in model.sidebar:
        if item.source in {"assignments", "homework", "cancellations"}:
            _add_item(
                result,
                item.source,
                item.id,
                _sidebar_fingerprint(item),
                _sidebar_expiry(item),
                expires_inclusive=item.source == "homework",
            )
    return result


def _add_item(
    result: dict[str, dict[str, dict[str, str | bool | None]]],
    entity_id: str,
    identity: str,
    fingerprint: str,
    expires_on: str | None,
    *,
    expires_inclusive: bool = False,
) -> None:
    item_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    result.setdefault(entity_id, {})[item_key] = {
        "fingerprint": fingerprint,
        "expires_on": expires_on,
        "expires_inclusive": expires_inclusive,
    }


def _event_fingerprint(event: DisplayEvent) -> str:
    return _fingerprint(
        {
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "title": event.title,
            "teacher": event.teacher,
            "room": event.room,
        }
    )


def _sidebar_fingerprint(item: SidebarItem) -> str:
    return _fingerprint(
        {
            "kind": item.kind,
            "title": item.title,
            "subtitle": item.subtitle,
            "when": item.when.isoformat() if item.when is not None else None,
        }
    )


def _fingerprint(value: dict[str, Any]) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _event_expiry(event: DisplayEvent) -> str:
    if isinstance(event.end, datetime):
        return event.end.date().isoformat()
    return event.end.isoformat()


def _sidebar_expiry(item: SidebarItem) -> str | None:
    if item.source not in {"homework", "cancellations"} or item.when is None:
        return None
    return item.when.date().isoformat() if isinstance(item.when, datetime) else item.when.isoformat()


def _is_expired(item: dict[str, str | bool | None], today: date) -> bool:
    value = item.get("expires_on")
    if not isinstance(value, str):
        return False
    try:
        expires_on = date.fromisoformat(value)
    except ValueError:
        return False
    if expires_on < today:
        return True
    # All-day or midnight-ending events use an exclusive end date. Homework
    # due dates also stop being displayed on their due date once that lesson
    # has passed, so those items age out at the start of that date.
    return expires_on == today and item.get("expires_inclusive") is True


def _empty_state() -> dict[str, Any]:
    return {"version": 1, "acknowledged": {}, "current": {}, "acknowledged_at": None}


def _load_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_state()
    except OSError as error:
        raise ValueError("Plan review state is unreadable") from error
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise ValueError("Plan review state is invalid") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("acknowledged"), dict)
        or not isinstance(value.get("current"), dict)
        or (value.get("acknowledged_at") is not None and not isinstance(value["acknowledged_at"], str))
    ):
        raise ValueError("Plan review state has an invalid format")
    for group in (value["acknowledged"], value["current"]):
        if any(
            not isinstance(source, str)
            or not isinstance(items, dict)
            or any(
                not isinstance(key, str)
                or not isinstance(item, dict)
                or not isinstance(item.get("fingerprint"), str)
                or (
                    item.get("expires_on") is not None
                    and not isinstance(item.get("expires_on"), str)
                )
                or not isinstance(item.get("expires_inclusive", False), bool)
                for key, item in items.items()
            )
            for source, items in group.items()
        ):
            raise ValueError("Plan review state has an invalid item")
    return value
