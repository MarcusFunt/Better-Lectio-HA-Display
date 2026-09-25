from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_builds_gateway_and_display_as_separate_internal_services():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

    assert set(compose["services"]) == {"lectio-gateway", "display-service"}
    for name, path in {
        "lectio-gateway": "services/lectio-gateway",
        "display-service": "services/display-service",
    }.items():
        service = compose["services"][name]
        assert service["build"]["context"] == f"./{path}"
        assert (ROOT / path / "Dockerfile").is_file()
        assert "ports" not in service
        assert service["healthcheck"]["test"]

    assert set(compose["volumes"]) == {"lectio-gateway-data", "display-service-data"}


def test_compose_keeps_admin_and_device_ports_unpublished_by_default():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

    assert all("ports" not in service for service in compose["services"].values())
