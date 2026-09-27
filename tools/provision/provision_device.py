"""Provision one XIAO ESP32-S3 display over USB without printing credentials."""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .protocol import encode_configuration

_ROOT = Path(__file__).resolve().parents[2]
_HELLO = b'{"version":1,"command":"hello"}\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="USB serial port, such as COM4")
    parser.add_argument("--server-url", help="private LAN base URL for display-service")
    parser.add_argument("--ssid", help="Wi-Fi network name")
    parser.add_argument("--name", default="Better Lectio display")
    parser.add_argument("--flash", action="store_true", help="build and flash lectio_s3 first")
    args = parser.parse_args(argv)

    try:
        import serial
    except ImportError:
        print(
            "pyserial is missing; install tools/provision/requirements.txt first.",
            file=sys.stderr,
        )
        return 2

    server_url = args.server_url or input("Private display-service URL: ").strip()
    wifi_ssid = args.ssid or input("Wi-Fi network name: ").strip()
    wifi_password = getpass.getpass("Wi-Fi password (hidden; blank for open Wi-Fi): ")
    device_id = uuid.uuid4().hex
    try:
        # Validate user-supplied data before creating an active registry entry.
        encode_configuration(
            server_url=server_url,
            device_id=device_id,
            device_secret="validation-only",
            wifi_ssid=wifi_ssid,
            wifi_password=wifi_password,
        )
    except ValueError as error:
        print(f"Invalid provisioning input: {error}", file=sys.stderr)
        return 2

    try:
        if args.flash:
            _flash_firmware(args.port)
        with serial.Serial(args.port, baudrate=115200, timeout=0.5, write_timeout=5) as port:
            if not _wait_for_ready(port, timeout_seconds=45):
                print("The device did not become ready over USB.", file=sys.stderr)
                return 1
            credential = _create_registry_device(args.name, device_id)
            registered = True
            payload = encode_configuration(
                server_url=server_url,
                device_id=credential["device_id"],
                device_secret=credential["device_secret"],
                wifi_ssid=wifi_ssid,
                wifi_password=wifi_password,
            )
            try:
                written = port.write(payload)
                port.flush()
                if written != len(payload):
                    raise OSError("partial USB write")
            except Exception:
                revoked = _revoke_registry_device(device_id)
                registered = False
                if revoked:
                    print("USB provisioning failed; the temporary device credential was revoked.", file=sys.stderr)
                else:
                    print(
                        "USB provisioning failed and automatic revocation did not complete. "
                        f"Revoke device ID {device_id} in the display service.",
                        file=sys.stderr,
                    )
                return 1
            finally:
                payload = b""

            response = _wait_for_device_response(port, timeout_seconds=5)
            if response is not None and response.get("status") == "error":
                revoked = _revoke_registry_device(device_id)
                registered = False
                if revoked:
                    print("The device rejected its configuration; the temporary credential was revoked.", file=sys.stderr)
                else:
                    print(
                        "The device rejected its configuration and automatic revocation did not complete. "
                        f"Revoke device ID {device_id} in the display service.",
                        file=sys.stderr,
                    )
                return 1

            print(f"Configuration sent to device {device_id}; waiting for authenticated service contact.")
            if _wait_for_authenticated_status(server_url, device_id, credential["device_secret"]):
                print("Provisioning complete: the device authenticated and reached the display service.")
                return 0
            print(
                "USB accepted the configuration, but service contact was not observed yet. "
                f"Device ID {device_id} remains registered; check its Wi-Fi and LAN route."
            )
            return 1
    except Exception as error:
        if "registered" in locals() and registered:
            if _revoke_registry_device(device_id):
                print("Provisioning stopped; the temporary device credential was revoked.", file=sys.stderr)
            else:
                print(
                    "Provisioning stopped and automatic revocation did not complete. "
                    f"Revoke device ID {device_id} in the display service.",
                    file=sys.stderr,
                )
        else:
            print(f"Provisioning failed ({type(error).__name__}).", file=sys.stderr)
        return 1


def _flash_firmware(port: str) -> None:
    command = [
        "pio",
        "run",
        "-d",
        str(_ROOT / "firmware"),
        "-e",
        "lectio_s3",
        "-t",
        "upload",
        "--upload-port",
        port,
    ]
    subprocess.run(command, cwd=_ROOT, check=True)


def _registry_command(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "display-service",
            "python",
            "-m",
            "display_service.devices",
            *arguments,
        ],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _create_registry_device(name: str, device_id: str) -> dict[str, str]:
    result = _registry_command("create", name, "--device-id", device_id)
    if result.returncode != 0:
        raise RuntimeError("The display service could not register the device")
    try:
        credential = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError("The display service returned an invalid registry result") from error
    if (
        not isinstance(credential, dict)
        or credential.get("device_id") != device_id
        or not isinstance(credential.get("device_secret"), str)
    ):
        raise RuntimeError("The display service returned an invalid registry result")
    return credential


def _revoke_registry_device(device_id: str) -> bool:
    try:
        result = _registry_command("revoke", device_id)
        response = json.loads(result.stdout)
    except (OSError, ValueError):
        return False
    return (
        result.returncode == 0
        and isinstance(response, dict)
        and response.get("device_id") == device_id
        and response.get("revoked") is True
    )


def _wait_for_ready(port, *, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    next_hello = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_hello:
            port.write(_HELLO)
            port.flush()
            next_hello = time.monotonic() + 1.0
        response = _read_response(port)
        if response and response.get("status") == "ready":
            return True
    return False


def _wait_for_device_response(port, *, timeout_seconds: int) -> dict[str, object] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = _read_response(port)
        if response and response.get("status") in {"ok", "error"}:
            return response
    return None


def _read_response(port) -> dict[str, object] | None:
    try:
        line = port.readline()
        if not line or len(line) > 2048:
            return None
        response = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, OSError):
        return None
    if not isinstance(response, dict) or response.get("version") != 1:
        return None
    return response


def _wait_for_authenticated_status(server_url: str, device_id: str, secret: str) -> bool:
    url = f"{server_url.rstrip('/')}/device/v1/status"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {secret}",
                "X-Device-ID": device_id,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read(4096))
            if isinstance(payload, dict) and payload.get("device_id") == device_id:
                return True
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            pass
        time.sleep(2)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
