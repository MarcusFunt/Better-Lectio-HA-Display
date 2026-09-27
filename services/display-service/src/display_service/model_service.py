"""Assemble a display model from independent Lectio gateway sources."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .gateway_client import SOURCES, GatewayApiError, LectioGatewayClient
from .model import DisplayModel
from .model_builder import build_display_model, display_window


@dataclass(frozen=True, slots=True)
class SourceStatus:
    state: str
    error: str | None = None
    is_stale: bool = False
    last_successful_sync: str | None = None


@dataclass(frozen=True, slots=True)
class DisplayModelResult:
    model: DisplayModel
    sources: dict[str, SourceStatus]


class DisplayModelService:
    """Preserve each source's last good items across gateway failures."""

    def __init__(self, client: LectioGatewayClient, *, max_sidebar_items: int = 8) -> None:
        if max_sidebar_items < 0:
            raise ValueError("max_sidebar_items cannot be negative")
        self._client = client
        self._max_sidebar_items = max_sidebar_items
        self._last_good: dict[str, list[dict[str, Any]]] = {}
        self._last_good_sync: dict[str, SourceStatus] = {}

    async def async_build(self, *, now: datetime | None = None) -> DisplayModelResult:
        start, end, _ = display_window(now)
        results = await asyncio.gather(
            self._client.async_get_status(),
            *(self._client.async_get_source(source, start, end) for source in SOURCES),
            return_exceptions=True,
        )
        # Status is diagnostic. Source envelopes carry their own freshness and
        # remain usable even if the separate status request fails.
        if isinstance(results[0], BaseException) and not isinstance(results[0], Exception):
            raise results[0]
        statuses: dict[str, SourceStatus] = {}
        values: dict[str, list[dict[str, Any]]] = {}
        for source, result in zip(SOURCES, results[1:], strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                cached = self._last_good_sync.get(source)
                has_cache = source in self._last_good
                statuses[source] = SourceStatus(
                    state="stale" if has_cache else "error",
                    error=_safe_error_code(result),
                    is_stale=has_cache,
                    last_successful_sync=cached.last_successful_sync if cached else None,
                )
                values[source] = self._last_good.get(source, [])
                continue
            sync = result["sync"]
            status = SourceStatus(
                state=sync["state"],
                error=sync.get("error") if isinstance(sync.get("error"), str) else None,
                is_stale=sync["is_stale"],
                last_successful_sync=sync.get("last_successful_sync"),
            )
            items = result["items"]
            if status.state == "valid" and not status.is_stale:
                self._last_good[source] = items
            self._last_good_sync[source] = status
            statuses[source] = status
            values[source] = self._last_good.get(source, items)

        model = build_display_model(
            now=now,
            lectio_events=values["schedule"],
            assignments=values["assignments"],
            homework=values["homework"],
            cancellations=values["cancellations"],
            max_sidebar_items=self._max_sidebar_items,
        )
        return DisplayModelResult(model=model, sources=statuses)


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, GatewayApiError):
        return error.code
    return "request_failed"
