from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_builds_gateway_and_display_as_separate_internal_services():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

    assert set(compose["services"]) == {
        "lectio-gateway",
        "lectio-auth-browser",
        "display-service",
    }
    for name, path in {
        "lectio-gateway": "services/lectio-gateway",
        "lectio-auth-browser": "services/lectio-auth-browser",
        "display-service": "services/display-service",
    }.items():
        service = compose["services"][name]
        assert service["build"]["context"] == f"./{path}"
        assert (ROOT / path / "Dockerfile").is_file()
        assert service["healthcheck"]["test"]

    assert set(compose["volumes"]) == {"lectio-gateway-data", "display-service-data"}
    assert compose["services"]["lectio-gateway"]["ports"] == [
        "127.0.0.1:8000:8000"
    ]
    assert compose["services"]["lectio-auth-browser"]["ports"] == [
        "127.0.0.1:6080:6080"
    ]
    assert "ports" not in compose["services"]["display-service"]
    assert compose["services"]["lectio-auth-browser"]["networks"] == [
        "lectio-auth-control"
    ]
    assert "lectio-auth-control" in compose["services"]["lectio-gateway"]["networks"]


def test_compose_only_publishes_auth_endpoints_on_loopback():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

    for service in compose["services"].values():
        for binding in service.get("ports", []):
            assert binding.startswith("127.0.0.1:")
