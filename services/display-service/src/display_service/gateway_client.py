"""Private Compose-network client for normalized Lectio gateway data."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .model_builder import DISPLAY_TIMEZONE

SOURCES = ("schedule", "assignments", "homework", "cancellations")
_STATES = {"unknown", "valid", "stale", "expired", "error"}


class GatewayApiError(Exception):
    """A failure code without gateway response content or private Lectio data."""

    def __init__(self, code: str, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class LectioGatewayClient:
    def __init__(self, base_url: str = "http://lectio-gateway:8000", *, timeout_seconds: int = 10) -> None:
        try:
            parsed = urlsplit(base_url.strip())
            parsed.port
        except ValueError as error:
            raise ValueError("Invalid Lectio gateway URL") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or timeout_seconds <= 0
        ):
            raise ValueError("Invalid Lectio gateway URL or timeout")
        self._base_url = base_url.strip().rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> LectioGatewayClient:
        if self._session is not None:
            raise RuntimeError("Lectio gateway client is already open")
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def async_get_status(self) -> dict[str, Any]:
        payload = await self._get("status")
        if (
            not isinstance(payload.get("auth"), dict)
            or not isinstance(payload["auth"].get("state"), str)
            or not isinstance(payload.get("sources"), dict)
        ):
            raise GatewayApiError("invalid_response")
        return payload

    async def async_get_source(
        self, source: str, start: datetime, end: datetime
    ) -> dict[str, Any]:
        if source not in SOURCES:
            raise ValueError("Unknown Lectio source")
        if (
            start.tzinfo is None
            or start.utcoffset() is None
            or end.tzinfo is None
            or end.utcoffset() is None
            or start >= end
        ):
            raise ValueError("Source range must be aware and increasing")
        payload = await self._get(
            source,
            params={
                "start": start.astimezone(DISPLAY_TIMEZONE).isoformat(),
                "end": end.astimezone(DISPLAY_TIMEZONE).isoformat(),
            },
        )
        items = payload.get("items")
        sync = payload.get("sync")
        if not isinstance(items, list) or not isinstance(sync, dict):
            raise GatewayApiError("invalid_response")
        if (
            not isinstance(sync.get("state"), str)
            or sync["state"] not in _STATES
            or not isinstance(sync.get("is_stale"), bool)
            or not _optional_aware_datetime(sync.get("last_successful_sync"))
        ):
            raise GatewayApiError("invalid_response")
        if not all(_valid_item(source, item) for item in items):
            raise GatewayApiError("invalid_response")
        return payload

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        session = self._session
        if session is None:
            raise RuntimeError("Use LectioGatewayClient as an async context manager")
        options: dict[str, Any] = {"headers": {"Accept": "application/json"}, "allow_redirects": False}
        if params is not None:
            options["params"] = params
        try:
            async with session.get(f"{self._base_url}/api/v1/{path}", **options) as response:
                if response.status < 200 or response.status >= 300:
                    code = {401: "authentication_required", 409: "student_id_required"}.get(
                        response.status, "gateway_error"
                    )
                    raise GatewayApiError(code, response.status)
                payload = await response.json(content_type=None)
        except GatewayApiError:
            raise
        except ValueError as error:
            raise GatewayApiError("invalid_response") from error
        except (aiohttp.ClientError, TimeoutError) as error:
            raise GatewayApiError("connection_failed") from error
        if not isinstance(payload, dict):
            raise GatewayApiError("invalid_response")
        return payload


def _optional_aware_datetime(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _valid_item(source: str, item: Any) -> bool:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
        return False
    if source == "schedule":
        return _valid_interval(item)
    if source == "assignments":
        return isinstance(item.get("title"), str) and _optional_aware_datetime(item.get("due"))
    if source == "homework":
        return isinstance(item.get("description"), str) and _optional_aware_datetime(
            item.get("target_lesson_start")
        )
    return _valid_interval(item) and isinstance(item.get("original_lesson"), dict)


def _valid_interval(item: dict[str, Any]) -> bool:
    if not _optional_aware_datetime(item.get("start")) or not _optional_aware_datetime(item.get("end")):
        return False
    try:
        start = datetime.fromisoformat(item["start"])
        end = datetime.fromisoformat(item["end"])
    except (KeyError, TypeError, ValueError):
        return False
    return start < end
