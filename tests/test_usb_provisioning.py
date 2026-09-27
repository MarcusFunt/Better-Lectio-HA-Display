import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.provision import provision_device
from tools.provision.protocol import encode_configuration


def test_make_provision_device_target_uses_module_and_forwards_arguments():
    makefile = Path(__file__).resolve().parents[1] / "Makefile"

    assert "provision-device:\n\t$(PYTHON) -m tools.provision.provision_device $(ARGS)" in makefile.read_text()


def _configuration(**updates):
    values = {
        "server_url": "http://192.168.1.20:8001",
        "device_id": "lectio-screen-1",
        "device_secret": "generated-device-secret",
        "wifi_ssid": "Home WiFi",
        "wifi_password": "private-password",
    }
    values.update(updates)
    return values


def test_usb_configuration_is_versioned_and_bounded():
    line = encode_configuration(**_configuration())

    payload = json.loads(line)
    assert payload["version"] == 1
    assert payload["command"] == "provision"
    assert payload["device_id"] == "lectio-screen-1"
    assert payload["device_secret"] == "generated-device-secret"
    assert line.endswith(b"\n")
    assert len(line) <= 2048


@pytest.mark.parametrize(
    "server_url",
    [
        "https://display.home:8001",
        "http://user:password@display.home:8001",
        "http://display.home:8001/admin",
        "http://display.home:8001?token=secret",
        "http://display.home:8001#fragment",
        "http://display.home:",
        r"http://display.home\other",
    ],
)
def test_usb_configuration_rejects_nonlocal_or_ambiguous_server_urls(server_url):
    with pytest.raises(ValueError):
        encode_configuration(**_configuration(server_url=server_url))


@pytest.mark.parametrize(
    "field,value",
    [
        ("wifi_ssid", "s" * 33),
        ("wifi_password", "p" * 64),
        ("device_id", "bad id"),
        ("device_secret", ""),
        ("server_url", "http://display.home:8001\n"),
    ],
)
def test_usb_configuration_rejects_invalid_or_oversized_fields(field, value):
    with pytest.raises(ValueError):
        encode_configuration(**_configuration(**{field: value}))


def test_usb_configuration_allows_an_open_wifi_network():
    payload = json.loads(
        encode_configuration(**_configuration(wifi_password=""))
    )

    assert payload["wifi_password"] == ""


class _FakeSerialPort:
    def __init__(self, *, fail_write=False):
        self.fail_write = fail_write
        self.payload = b""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def write(self, payload):
        if self.fail_write:
            raise OSError("USB write failed")
        self.payload += payload
        return len(payload)

    def flush(self):
        return None


def _mock_serial(monkeypatch, port):
    monkeypatch.setitem(
        sys.modules,
        "serial",
        SimpleNamespace(Serial=lambda *args, **kwargs: port),
    )
    monkeypatch.setattr(provision_device, "_wait_for_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        provision_device,
        "_create_registry_device",
        lambda name, device_id: {
            "device_id": device_id,
            "device_secret": "generated-device-secret",
        },
    )
    monkeypatch.setattr(
        provision_device,
        "_wait_for_device_response",
        lambda *args, **kwargs: {"version": 1, "status": "ok"},
    )
    monkeypatch.setattr(
        provision_device, "_wait_for_authenticated_status", lambda *args: True
    )
    monkeypatch.setattr(
        provision_device.getpass, "getpass", lambda prompt: "private-password"
    )


def test_host_provisioning_keeps_credentials_out_of_terminal_output(
    monkeypatch, capsys
):
    port = _FakeSerialPort()
    _mock_serial(monkeypatch, port)

    result = provision_device.main(
        [
            "--port",
            "COM4",
            "--server-url",
            "http://192.168.1.20:8001",
            "--ssid",
            "Home WiFi",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert b"generated-device-secret" in port.payload
    assert b"private-password" in port.payload
    assert "generated-device-secret" not in output
    assert "private-password" not in output


def test_host_provisioning_revokes_registry_device_after_usb_write_failure(
    monkeypatch, capsys
):
    port = _FakeSerialPort(fail_write=True)
    _mock_serial(monkeypatch, port)
    revoked = []
    monkeypatch.setattr(
        provision_device,
        "_revoke_registry_device",
        lambda device_id: revoked.append(device_id) or True,
    )

    result = provision_device.main(
        [
            "--port",
            "COM4",
            "--server-url",
            "http://192.168.1.20:8001",
            "--ssid",
            "Home WiFi",
        ]
    )

    output = capsys.readouterr().err
    assert result == 1
    assert len(revoked) == 1
    assert "revoked" in output
    assert "generated-device-secret" not in output
    assert "private-password" not in output
