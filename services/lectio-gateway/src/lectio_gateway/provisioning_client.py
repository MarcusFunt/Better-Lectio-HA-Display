"""Authenticated gateway client for the display provisioning API."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

_DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DeviceRegistrationConflict(Exception):
    pass


class DisplayProvisioningUnavailable(Exception):
    pass


class DisplayProvisioningClient:
    def __init__(self, base_url: str, token_path: Path) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_path = token_path

    async def create(self, name: str, device_id: str) -> dict[str, str]:
        payload = await self._request(
            "POST",
            "/internal/v1/provisioning/devices",
            json={"name": name, "device_id": device_id},
        )
        if (
            not isinstance(payload, dict)
            or payload.get("device_id") != device_id
            or not isinstance(payload.get("device_secret"), str)
        ):
            raise DisplayProvisioningUnavailable
        return {
            "device_id": device_id,
            "device_secret": payload["device_secret"],
        }

    async def revoke(self, device_id: str) -> dict[str, object]:
        return await self._request(
            "POST", f"/internal/v1/provisioning/devices/{device_id}/revoke"
        )

    async def status(self, device_id: str) -> dict[str, object]:
        return await self._request(
            "GET", f"/internal/v1/provisioning/devices/{device_id}"
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, object]:
        token = self._read_token()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
        except httpx.HTTPError as error:
            raise DisplayProvisioningUnavailable from error
        if response.status_code == 409:
            raise DeviceRegistrationConflict
        if response.status_code == 404:
            raise KeyError("Device not found")
        if response.status_code not in {200, 201}:
            raise DisplayProvisioningUnavailable
        try:
            payload = response.json()
        except ValueError as error:
            raise DisplayProvisioningUnavailable from error
        if not isinstance(payload, dict):
            raise DisplayProvisioningUnavailable
        return payload

    def _read_token(self) -> str:
        try:
            token = self._token_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise DisplayProvisioningUnavailable from error
        if len(token) < 40 or any(character.isspace() for character in token):
            raise DisplayProvisioningUnavailable
        return token


def valid_device_id(value: str) -> bool:
    return bool(_DEVICE_ID.fullmatch(value))
