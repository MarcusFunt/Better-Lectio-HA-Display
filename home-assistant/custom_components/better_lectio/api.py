"""Async client for the local Better Lectio Gateway API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import REQUEST_TIMEOUT_SECONDS, SOURCES


class GatewayApiError(Exception):
    """A sanitized gateway API error."""

    def __init__(self, status: int | None = None) -> None:
        self.status = status
        if status == 401:
            self.code = "authentication_required"
        elif status == 409:
            self.code = "student_id_required"
        elif status is None:
            self.code = "connection_failed"
        else:
            self.code = "gateway_error"
        super().__init__(self.code)


class GatewayApi:
    """Client that reuses Home Assistant's shared HTTP session."""

    def __init__(self, hass: HomeAssistant, base_url: str) -> None:
        self._session = async_get_clientsession(hass)
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

    async def get_status(self) -> dict[str, Any]:
        """Read gateway auth and per-source sync status."""
        payload = await self._get("/api/v1/status")
        if not isinstance(payload.get("auth"), dict) or not isinstance(
            payload.get("sources"), dict
        ):
            raise GatewayApiError()
        return payload

    async def get_source(
        self, source: str, start: datetime, end: datetime
    ) -> dict[str, Any]:
        """Fetch one normalized source over an aware date range."""
        if source not in SOURCES:
            raise ValueError(f"Unknown Lectio data source: {source}")
        payload = await self._get(
            f"/api/v1/{source}",
            params={"start": start.isoformat(), "end": end.isoformat()},
        )
        if not isinstance(payload.get("items"), list) or not isinstance(
            payload.get("sync"), dict
        ):
            raise GatewayApiError()
        if not all(isinstance(item, dict) for item in payload["items"]):
            raise GatewayApiError()
        return payload

    async def _get(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Issue a GET without retaining gateway response bodies in exceptions."""
        try:
            async with self._session.get(
                f"{self._base_url}{path}", params=params, timeout=self._timeout
            ) as response:
                if response.status != 200:
                    raise GatewayApiError(response.status)
                payload = await response.json()
        except GatewayApiError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise GatewayApiError() from err
        if not isinstance(payload, dict):
            raise GatewayApiError()
        return payload
