"""Validated Home Assistant entity IDs grouped by semantic display role."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_ENTITY_ID = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+\Z")


@dataclass(frozen=True, slots=True)
class HomeAssistantEntityConfig:
    """Home Assistant identifiers used by the display model service."""

    lectio_calendar_entity_id: str = "calendar.lectio"
    private_calendar_entity_ids: tuple[str, ...] = ("calendar.private",)
    assignments_entity_id: str = "todo.lectio_assignments"
    homework_entity_id: str = "todo.lectio_homework"
    cancellations_entity_id: str = "sensor.lectio_cancellations"

    def __post_init__(self) -> None:
        if not isinstance(self.private_calendar_entity_ids, tuple):
            raise ValueError("Private calendar entity IDs must be a tuple")
        _validate_entity_id(
            self.lectio_calendar_entity_id, role="Lectio calendar", domain="calendar"
        )
        for entity_id in self.private_calendar_entity_ids:
            _validate_entity_id(entity_id, role="Private calendar", domain="calendar")
        _validate_entity_id(
            self.assignments_entity_id, role="Assignments", domain="todo"
        )
        _validate_entity_id(self.homework_entity_id, role="Homework", domain="todo")
        _validate_entity_id(
            self.cancellations_entity_id, role="Cancellations", domain="sensor"
        )
        entity_ids = (
            self.lectio_calendar_entity_id,
            *self.private_calendar_entity_ids,
            self.assignments_entity_id,
            self.homework_entity_id,
            self.cancellations_entity_id,
        )
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("A duplicate entity ID is configured across Home Assistant roles")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> HomeAssistantEntityConfig:
        """Build and validate configuration from an explicit environment mapping."""
        defaults = cls()
        private_value = environ.get(
            "HA_PRIVATE_CALENDARS", ",".join(defaults.private_calendar_entity_ids)
        )
        if not isinstance(private_value, str):
            raise ValueError("HA_PRIVATE_CALENDARS must be a string")
        private_calendars = tuple(
            entity_id
            for part in private_value.split(",")
            if (entity_id := part.strip())
        )
        return cls(
            lectio_calendar_entity_id=_environment_value(
                environ, "HA_LECTIO_CALENDAR", defaults.lectio_calendar_entity_id
            ),
            private_calendar_entity_ids=private_calendars,
            assignments_entity_id=_environment_value(
                environ, "HA_ASSIGNMENTS_TODO", defaults.assignments_entity_id
            ),
            homework_entity_id=_environment_value(
                environ, "HA_HOMEWORK_TODO", defaults.homework_entity_id
            ),
            cancellations_entity_id=_environment_value(
                environ, "HA_CANCELLATIONS_SENSOR", defaults.cancellations_entity_id
            ),
        )


def _environment_value(
    environ: Mapping[str, str], key: str, default: str
) -> str:
    value = environ.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value.strip()


def _validate_entity_id(value: str, *, role: str, domain: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{role} entity ID must not be empty")
    if not _ENTITY_ID.fullmatch(value):
        raise ValueError(f"{role} must have a valid entity ID")
    actual_domain = value.split(".", maxsplit=1)[0]
    if actual_domain != domain:
        raise ValueError(f"{role} entity ID must use the {domain} domain")
