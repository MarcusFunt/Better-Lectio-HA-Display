"""Minimal asynchronous client for the Home Assistant REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp

from .model_builder import DISPLAY_TIMEZONE

_DEFAULT_TIMEOUT_SECONDS = 10
_UNAVAILABLE_STATES = {"unknown", "unavailable"}


class HomeAssistantApiError(Exception):
    """A sanitized Home Assistant API failure."""

    def __init__(self, code: str, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class HomeAssistantClient:
    """Read calendars, to-do lists, and one structured sensor from HA."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        try:
            parsed = urlsplit(base_url.strip())
            parsed.port
        except ValueError as err:
            raise ValueError("Invalid Home Assistant URL") from err
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "?" in base_url
            or "#" in base_url
        ):
            raise ValueError("Invalid Home Assistant URL")
        if not token.strip():
            raise ValueError("Home Assistant token is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self._base_url = base_url.strip().rstrip("/")
        self._token = token.strip()
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> HomeAssistantClient:
        if self._session is not None:
            raise RuntimeError("Home Assistant client is already open")
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def async_get_calendar_events(
        self, entity_id: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Fetch expanded calendar events for an aware exclusive range."""
        _validate_entity_id(entity_id, "calendar")
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or start.utcoffset() is None
            or end.utcoffset() is None
            or start >= end
        ):
            raise ValueError("Calendar range must be aware and increasing")
        payload = await self._post_service(
            "calendar",
            "get_events",
            {
                "entity_id": entity_id,
                "start_date_time": start.astimezone(DISPLAY_TIMEZONE).isoformat(),
                "end_date_time": end.astimezone(DISPLAY_TIMEZONE).isoformat(),
            },
        )
        events = _service_items(payload, entity_id, "events")
        return _dict_items(events)

    async def async_get_todo_items(self, entity_id: str) -> list[dict[str, Any]]:
        """Fetch only outstanding items from a Home Assistant to-do list."""
        _validate_entity_id(entity_id, "todo")
        payload = await self._post_service(
            "todo", "get_items", {"entity_id": entity_id, "status": "needs_action"}
        )
        return _dict_items(_service_items(payload, entity_id, "items"))

    async def async_get_cancellations(self, entity_id: str) -> list[dict[str, Any]]:
        """Read the cancellation sensor's allowlisted structured attribute."""
        _validate_entity_id(entity_id, "sensor")
        payload = await self._request_json(
            "GET", f"/api/states/{quote(entity_id, safe='.') }"
        )
        if not isinstance(payload.get("attributes"), dict):
            raise HomeAssistantApiError("invalid_response")
        state = payload.get("state")
        if isinstance(state, str) and state.casefold() in _UNAVAILABLE_STATES:
            raise HomeAssistantApiError("entity_unavailable")
        cancellations = payload["attributes"].get("cancellations", [])
        return _dict_items(cancellations)

    async def async_get_entity_sync(self, entity_id: str) -> dict[str, Any]:
        """Read the safe sync attribute exposed by a Lectio-backed HA entity."""
        entity_domain = entity_id.partition(".")[0]
        if entity_domain not in {"calendar", "todo", "sensor"}:
            raise ValueError("Unsupported Home Assistant entity domain")
        _validate_entity_id(entity_id, entity_domain)
        payload = await self._request_json(
            "GET", f"/api/states/{quote(entity_id, safe='.') }"
        )
        attributes = payload.get("attributes")
        if not isinstance(attributes, dict):
            raise HomeAssistantApiError("invalid_response")
        state = payload.get("state")
        if isinstance(state, str) and state.casefold() in _UNAVAILABLE_STATES:
            raise HomeAssistantApiError("entity_unavailable")
        sync = attributes.get("sync")
        if not isinstance(sync, dict):
            raise HomeAssistantApiError("invalid_response")
        return sync

    async def _post_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            f"/api/services/{domain}/{service}?return_response=true",
            data=data,
        )

    async def _request_json(
        self, method: str, path: str, *, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        session = self._session
        if session is None:
            raise RuntimeError("Use HomeAssistantClient as an async context manager")
        try:
            async with session.request(
                method,
                f"{self._base_url}{path}",
                json=data,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
                allow_redirects=False,
            ) as response:
                if response.status < 200 or response.status >= 300:
                    raise HomeAssistantApiError(_status_code(response.status), response.status)
                payload = await response.json(content_type=None)
        except HomeAssistantApiError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise HomeAssistantApiError("connection_failed") from err
        if not isinstance(payload, dict):
            raise HomeAssistantApiError("invalid_response")
        return payload


def _validate_entity_id(entity_id: str, domain: str) -> None:
    entity_domain, separator, object_id = entity_id.partition(".")
    if (
        not separator
        or entity_domain != domain
        or not object_id
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in object_id)
    ):
        raise ValueError(f"Invalid {domain} entity ID")


def _service_items(
    payload: dict[str, Any], entity_id: str, key: str
) -> list[Any]:
    response = payload.get("service_response")
    if not isinstance(response, dict):
        raise HomeAssistantApiError("invalid_response")
    entity_response = response.get(entity_id)
    if not isinstance(entity_response, dict):
        raise HomeAssistantApiError("invalid_response")
    items = entity_response.get(key)
    if not isinstance(items, list):
        raise HomeAssistantApiError("invalid_response")
    return items


def _dict_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise HomeAssistantApiError("invalid_response")
    return items


def _status_code(status: int) -> str:
    return {
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
    }.get(status, "ha_error")
