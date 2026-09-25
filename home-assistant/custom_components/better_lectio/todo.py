"""Read-only assignment and homework to-do lists."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import LectioDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Create the read-only assignment and homework lists."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            LectioAssignmentsTodo(entry, coordinator),
            LectioHomeworkTodo(entry, coordinator),
        ]
    )


class _LectioTodoList(TodoListEntity):
    """Common read-only list behavior."""

    _attr_has_entity_name = True
    _attr_supported_features = 0

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: LectioDataUpdateCoordinator,
        source: str,
    ) -> None:
        self._coordinator = coordinator
        self._source = source
        self._todo_items: list[TodoItem] = []
        self._attr_unique_id = f"{entry.entry_id}_{source}"
        self._remove_coordinator_listener = coordinator.async_add_listener(
            self._coordinator_updated
        )
        self._coordinator_updated()

    @property
    def available(self) -> bool:
        """Keep cached to-do data unavailable when the gateway is unreachable."""
        return self._coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose source freshness without leaking raw gateway errors."""
        data = self._coordinator.data
        return {
            "gateway_reachable": self._coordinator.gateway_reachable,
            "sync": data.sources.get(self._source, {}) if data is not None else {},
        }

    @property
    def todo_items(self) -> list[TodoItem]:
        """Return normalized items from the last coordinator update."""
        return self._todo_items

    def _coordinator_updated(self) -> None:
        data = self._coordinator.data
        raw_items = data.items[self._source] if data is not None else []
        converter = assignment_to_todo if self._source == "assignments" else homework_to_todo
        self._todo_items = [converter(item) for item in raw_items]
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from coordinator updates when unloaded."""
        self._remove_coordinator_listener()


class LectioAssignmentsTodo(_LectioTodoList):
    """Read-only assignment list."""

    _attr_name = "Lectio assignments"

    def __init__(self, entry: ConfigEntry, coordinator: LectioDataUpdateCoordinator):
        super().__init__(entry, coordinator, "assignments")


class LectioHomeworkTodo(_LectioTodoList):
    """Read-only homework list."""

    _attr_name = "Lectio homework"

    def __init__(self, entry: ConfigEntry, coordinator: LectioDataUpdateCoordinator):
        super().__init__(entry, coordinator, "homework")


def assignment_to_todo(item: dict[str, Any]) -> TodoItem:
    """Normalize an assignment as a Home Assistant to-do item."""
    status = str(item.get("status", "")).strip().casefold()
    completed = status in {"completed", "done", "submitted", "afleveret", "afsluttet"}
    return TodoItem(
        uid=_stable_uid(item),
        summary=_text(item.get("title")) or "Lectio assignment",
        status=(
            TodoItemStatus.COMPLETED if completed else TodoItemStatus.NEEDS_ACTION
        ),
        due=_parse_datetime(item.get("due")),
        description=_join_description(
            item.get("description"), item.get("subject"), item.get("source_url")
        ),
    )


def homework_to_todo(item: dict[str, Any]) -> TodoItem:
    """Normalize homework and use its lesson time as the due time."""
    subject = _text(item.get("subject"))
    description = _text(item.get("description"))
    return TodoItem(
        uid=_stable_uid(item),
        summary=subject or description or "Lectio homework",
        status=TodoItemStatus.NEEDS_ACTION,
        due=_parse_datetime(item.get("target_lesson_start")),
        description=_join_description(description, item.get("source_url")),
    )


def _stable_uid(item: dict[str, Any]) -> str | None:
    return _text(item.get("id")) or _text(item.get("source_id"))


def _parse_datetime(value: Any) -> date | datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        return None
    if isinstance(parsed, datetime):
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    if isinstance(parsed, date):
        return parsed
    return None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _join_description(*values: Any) -> str | None:
    parts = [text for value in values if (text := _text(value))]
    return "\n".join(parts) or None
