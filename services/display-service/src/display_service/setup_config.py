"""Read managed Home Assistant settings with a legacy environment fallback."""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .entity_config import HomeAssistantEntityConfig
from .ha_client import HomeAssistantClient

_CONFIG_FILENAME = "home-assistant-display.json"


@dataclass(frozen=True, slots=True)
class HomeAssistantDisplaySettings:
    """Validated runtime settings; keep the long-lived token out of reprs."""

    ha_url: str
    ha_token: str = field(repr=False)
    entity_config: HomeAssistantEntityConfig = HomeAssistantEntityConfig()


class HomeAssistantDisplaySettingsStore:
    """Load versioned managed configuration or pre-migration environment values."""

    def __init__(self, config_dir: Path):
        self.config_dir = Path(config_dir)
        self.config_path = self.config_dir / _CONFIG_FILENAME

    def load(
        self, environ: Mapping[str, str]
    ) -> HomeAssistantDisplaySettings | None:
        try:
            config_mode = self.config_path.lstat().st_mode
        except FileNotFoundError:
            return self._load_environment(environ)
        except OSError as exc:
            raise ValueError("Managed Home Assistant configuration is unreadable") from exc
        if not stat.S_ISREG(config_mode):
            raise ValueError("Managed Home Assistant configuration is invalid")
        return self._load_managed()

    def _load_managed(self) -> HomeAssistantDisplaySettings | None:
        try:
            if not self.config_path.is_file():
                raise ValueError("Managed Home Assistant configuration is invalid")
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Managed Home Assistant configuration is unreadable") from exc
        if payload == {"version": 1, "disconnected": True}:
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or set(payload) != {"version", "ha_url", "ha_token", "entities"}
        ):
            raise ValueError("Managed Home Assistant configuration has an invalid format")
        entities = payload.get("entities")
        if not isinstance(entities, dict):
            raise ValueError("Managed Home Assistant entity configuration is invalid")
        try:
            entity_config = _entity_config_from_roles(entities)
            return _validated_settings(payload.get("ha_url"), payload.get("ha_token"), entity_config)
        except (TypeError, ValueError) as exc:
            raise ValueError("Managed Home Assistant configuration is invalid") from exc

    @staticmethod
    def _load_environment(
        environ: Mapping[str, str]
    ) -> HomeAssistantDisplaySettings | None:
        url = environ.get("HOME_ASSISTANT_URL", "").strip()
        token = environ.get("HOME_ASSISTANT_TOKEN", "").strip()
        entity_config = HomeAssistantEntityConfig.from_env(environ)
        if not url and not token:
            return None
        if not url or not token:
            raise ValueError("Home Assistant URL and token must both be configured")
        return _validated_settings(url, token, entity_config)


def _entity_config_from_roles(roles: Mapping[str, object]) -> HomeAssistantEntityConfig:
    defaults = HomeAssistantEntityConfig()
    allowed = {
        "lectio_calendar",
        "private_calendars",
        "assignments_todo",
        "homework_todo",
        "cancellations_sensor",
    }
    if set(roles) - allowed:
        raise ValueError("Unknown Home Assistant entity role")
    private_calendars = roles.get(
        "private_calendars", list(defaults.private_calendar_entity_ids)
    )
    if not isinstance(private_calendars, (list, tuple)) or any(
        not isinstance(entity_id, str) for entity_id in private_calendars
    ):
        raise ValueError("Private calendar entity IDs must be a list of strings")
    return HomeAssistantEntityConfig(
        lectio_calendar_entity_id=_role_value(
            roles, "lectio_calendar", defaults.lectio_calendar_entity_id
        ),
        private_calendar_entity_ids=tuple(private_calendars),
        assignments_entity_id=_role_value(
            roles, "assignments_todo", defaults.assignments_entity_id
        ),
        homework_entity_id=_role_value(
            roles, "homework_todo", defaults.homework_entity_id
        ),
        cancellations_entity_id=_role_value(
            roles, "cancellations_sensor", defaults.cancellations_entity_id
        ),
    )


def _role_value(roles: Mapping[str, object], key: str, default: str) -> str:
    value = roles.get(key, default)
    if not isinstance(value, str):
        raise ValueError("Home Assistant entity IDs must be strings")
    return value


def _validated_settings(
    url: object,
    token: object,
    entity_config: HomeAssistantEntityConfig,
) -> HomeAssistantDisplaySettings:
    if not isinstance(url, str) or not isinstance(token, str):
        raise ValueError("Home Assistant URL and token must be strings")
    HomeAssistantClient(url, token)
    return HomeAssistantDisplaySettings(
        ha_url=url.strip().rstrip("/"),
        ha_token=token.strip(),
        entity_config=entity_config,
    )
