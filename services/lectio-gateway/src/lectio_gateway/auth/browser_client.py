import asyncio
import logging
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

_LOGGER = logging.getLogger(__name__)


class BrowserUnavailable(RuntimeError):
    """The isolated authentication browser could not be reached."""


class BrowserSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: Literal["idle", "starting", "waiting_for_user", "candidate", "expired", "error"]
    session: dict[str, Any] | None = None


class AuthBrowserClient:
    def __init__(
        self,
        base_url: str,
        login_url: str,
        timeout_seconds: int,
        lifecycle_url: str = "http://lectio-auth-lifecycle:8766",
    ):
        self._base_url = base_url.rstrip("/")
        self._lifecycle_url = lifecycle_url.rstrip("/")
        self._login_url = login_url
        self._timeout_seconds = timeout_seconds

    async def start(self) -> None:
        await self._post(self._lifecycle_url, "/browser/start")
        try:
            await self._wait_for_browser()
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._base_url}/session/start",
                    json={
                        "login_url": self._login_url,
                        "timeout_seconds": self._timeout_seconds,
                    },
                )
                response.raise_for_status()
        except BrowserUnavailable:
            await self._stop_lifecycle()
            raise
        except (httpx.HTTPError, ValueError) as exc:
            await self._stop_lifecycle()
            raise BrowserUnavailable("The Lectio authentication browser is unavailable") from exc

    async def snapshot(self) -> BrowserSnapshot:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self._base_url}/session/status")
                response.raise_for_status()
                return BrowserSnapshot.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise BrowserUnavailable("The Lectio authentication browser is unavailable") from exc

    async def complete(self) -> None:
        await self._post(self._base_url, "/session/complete")

    async def stop(self) -> None:
        try:
            await self._post(self._base_url, "/session/stop")
        except BrowserUnavailable:
            # Shutdown/logout must also work if the browser sidecar has already stopped.
            pass
        await self._stop_lifecycle()

    async def _wait_for_browser(self) -> None:
        deadline = time.monotonic() + 30
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"{self._base_url}/health")
                    response.raise_for_status()
                    return
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                await asyncio.sleep(0.25)
        raise BrowserUnavailable("The Lectio authentication browser did not become ready") from last_error

    async def _stop_lifecycle(self) -> None:
        last_error: BrowserUnavailable | None = None
        for attempt in range(1, 3):
            try:
                await self._post(self._lifecycle_url, "/browser/stop")
                return
            except BrowserUnavailable as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.25)

        # Authentication cleanup must not mask gateway shutdown or cancellation.
        # The supervisor also removes an orphaned browser when it restarts.
        _LOGGER.error(
            "Could not stop the temporary auth-browser after two attempts; "
            "the lifecycle supervisor will retry cleanup on restart: %s",
            last_error,
        )

    async def _post(self, base_url: str, path: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(f"{base_url}{path}")
                response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            raise BrowserUnavailable("The Lectio authentication browser is unavailable") from exc
