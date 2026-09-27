"""Versioned, bounded payloads for the USB provisioning protocol."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

MAX_CONFIGURATION_LINE_BYTES = 2048
_DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def encode_configuration(
    *,
    server_url: str,
    device_id: str,
    device_secret: str,
    wifi_ssid: str,
    wifi_password: str,
) -> bytes:
    """Validate fields and encode one newline-terminated JSON configuration."""
    _validate_text("server_url", server_url, max_bytes=200, allow_empty=False)
    _validate_text("device_id", device_id, max_bytes=128, allow_empty=False)
    _validate_text("device_secret", device_secret, max_bytes=128, allow_empty=False)
    _validate_text("wifi_ssid", wifi_ssid, max_bytes=32, allow_empty=False)
    _validate_text("wifi_password", wifi_password, max_bytes=63, allow_empty=True)
    if not _DEVICE_ID.fullmatch(device_id):
        raise ValueError("device_id has an invalid format")
    _validate_server_url(server_url)

    payload = {
        "version": 1,
        "command": "provision",
        "server_url": server_url,
        "device_id": device_id,
        "device_secret": device_secret,
        "wifi_ssid": wifi_ssid,
        "wifi_password": wifi_password,
    }
    encoded = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_CONFIGURATION_LINE_BYTES:
        raise ValueError("USB configuration exceeds the protocol size limit")
    return encoded


def _validate_text(
    name: str, value: str, *, max_bytes: int, allow_empty: bool
) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{name} must be a non-empty string")
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} exceeds the allowed format or size")


def _validate_server_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("server_url must be a valid local HTTP base URL") from error
    if (
        parsed.scheme.casefold() != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
        or "\\" in value
        or (port is not None and not 1 <= port <= 65535)
        or any(character.isspace() for character in parsed.hostname)
    ):
        raise ValueError("server_url must be a local HTTP base URL without credentials or paths")
