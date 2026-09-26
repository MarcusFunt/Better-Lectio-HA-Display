"""Private, versioned configuration used by the HA setup wizard."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator

_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_ENTITY_DOMAINS = {
    "lectio_calendar": "calendar",
    "assignments_todo": "todo",
    "homework_todo": "todo",
    "cancellations_sensor": "sensor",
}


def _validate_http_url(value: str, *, reject_wildcard: bool = True) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing .port validates malformed and out-of-range ports.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid Home Assistant URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        raise ValueError("Enter an HTTP(S) URL without credentials, query, or fragment")
    if reject_wildcard:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and address.is_unspecified:
            raise ValueError("Use an address reachable from Home Assistant")
    return value.rstrip("/")


class HomeAssistantEntityRoles(BaseModel):
    """Semantic Home Assistant entities read by the display service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lectio_calendar: str = "calendar.lectio"
    private_calendars: tuple[str, ...] = ("calendar.private",)
    assignments_todo: str = "todo.lectio_assignments"
    homework_todo: str = "todo.lectio_homework"
    cancellations_sensor: str = "sensor.lectio_cancellations"

    @field_validator(
        "lectio_calendar", "private_calendars", "assignments_todo",
        "homework_todo", "cancellations_sensor", mode="before"
    )
    @classmethod
    def validate_entity_id_shape(cls, value):
        values = value if isinstance(value, (tuple, list)) else (value,)
        if any(not isinstance(item, str) or not _ENTITY_ID.fullmatch(item) for item in values):
            raise ValueError("Use valid Home Assistant entity IDs")
        return value

    @model_validator(mode="after")
    def validate_domains_and_duplicates(self) -> "HomeAssistantEntityRoles":
        all_entity_ids: list[str] = []
        for field, domain in _ENTITY_DOMAINS.items():
            entity_id = getattr(self, field)
            if entity_id.split(".", 1)[0] != domain:
                raise ValueError(f"{field} must use the {domain} domain")
            all_entity_ids.append(entity_id)
        for entity_id in self.private_calendars:
            if entity_id.split(".", 1)[0] != "calendar":
                raise ValueError("private_calendars must contain calendar entities")
            all_entity_ids.append(entity_id)
        if len(all_entity_ids) != len(set(all_entity_ids)):
            raise ValueError("Entity roles must use unique entity IDs")
        return self


class HomeAssistantDisplaySettings(BaseModel):
    """Display connection details; the HA credential is hidden from repr."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ha_url: str
    ha_token: SecretStr
    entity_roles: HomeAssistantEntityRoles = HomeAssistantEntityRoles()

    @field_validator("ha_url")
    @classmethod
    def validate_ha_url(cls, value: str) -> str:
        return _validate_http_url(value)


class HomeAssistantSetupStore:
    """Persist setup secrets separately from Lectio sessions and display data."""

    def __init__(self, config_dir: Path, gateway_data_dir: Path):
        self.config_dir = Path(config_dir)
        self.gateway_data_dir = Path(gateway_data_dir)
        self.display_config_path = self.config_dir / "home-assistant-display.json"
        self.gateway_api_url_path = self.gateway_data_dir / "ha-api-url.json"

    @staticmethod
    def _secure_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            path.chmod(0o700)

    @classmethod
    def _atomic_write(cls, path: Path, payload: bytes, *, mode: int) -> None:
        cls._secure_directory(path.parent)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            if os.name == "posix":
                os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, path)
            if os.name == "posix":
                path.chmod(mode)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def save_display_settings(self, settings: HomeAssistantDisplaySettings) -> None:
        payload = {
            "version": 1,
            "ha_url": settings.ha_url,
            "ha_token": settings.ha_token.get_secret_value(),
            "entities": settings.entity_roles.model_dump(mode="json"),
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self._atomic_write(self.display_config_path, encoded, mode=0o600)

    def load_display_settings(self) -> HomeAssistantDisplaySettings | None:
        try:
            payload = json.loads(self.display_config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Managed Home Assistant display configuration is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Managed Home Assistant display configuration has an unsupported version")
        if payload == {"version": 1, "disconnected": True}:
            return None
        try:
            payload = dict(payload)
            payload["entity_roles"] = payload.pop("entities")
            return HomeAssistantDisplaySettings.model_validate(
                {key: value for key, value in payload.items() if key != "version"}
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Managed Home Assistant display configuration is invalid") from exc

    def update_display_settings(
        self,
        *,
        ha_url: str,
        ha_token: str,
        entity_roles: HomeAssistantEntityRoles,
    ) -> HomeAssistantDisplaySettings:
        existing = self.load_display_settings()
        token = ha_token.strip()
        if not token:
            if existing is None:
                raise ValueError("A Home Assistant token is required for initial setup")
            token = existing.ha_token.get_secret_value()
        updated = HomeAssistantDisplaySettings(
            ha_url=ha_url,
            ha_token=token,
            entity_roles=entity_roles,
        )
        self.save_display_settings(updated)
        return updated

    def clear_display_settings(self) -> None:
        payload = json.dumps(
            {"version": 1, "disconnected": True},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._atomic_write(self.display_config_path, payload, mode=0o600)

    def save_gateway_api_url(self, url: str) -> None:
        normalized = _validate_http_url(url)
        payload = json.dumps(
            {"version": 1, "url": normalized}, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        self._atomic_write(self.gateway_api_url_path, payload, mode=0o600)

    def load_gateway_api_url(self) -> str | None:
        try:
            payload = json.loads(self.gateway_api_url_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Managed Home Assistant API URL is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Managed Home Assistant API URL has an unsupported version")
        try:
            return _validate_http_url(payload["url"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Managed Home Assistant API URL is invalid") from exc
