from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict


class BrowserUnavailable(RuntimeError):
    """The isolated authentication browser could not be reached."""


class BrowserSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: Literal["idle", "starting", "waiting_for_user", "candidate", "expired", "error"]
    session: dict[str, Any] | None = None


class AuthBrowserClient:
    def __init__(self, base_url: str, login_url: str, timeout_seconds: int):
        self._base_url = base_url.rstrip("/")
        self._login_url = login_url
        self._timeout_seconds = timeout_seconds

    async def start(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._base_url}/session/start",
                    json={
                        "login_url": self._login_url,
                        "timeout_seconds": self._timeout_seconds,
                    },
                )
                response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
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
        await self._post("/session/complete")

    async def stop(self) -> None:
        try:
            await self._post("/session/stop")
        except BrowserUnavailable:
            # Shutdown/logout must also work if the browser sidecar has already stopped.
            return

    async def _post(self, path: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(f"{self._base_url}{path}")
                response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            raise BrowserUnavailable("The Lectio authentication browser is unavailable") from exc
