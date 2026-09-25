from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_builds_gateway_and_display_as_separate_services():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

    assert set(compose["services"]) == {
        "lectio-gateway",
        "lectio-auth-browser",
        "lectio-auth-lifecycle",
        "display-service",
        "display-diagnostics",
    }
    for name, path in {
        "lectio-gateway": "services/lectio-gateway",
        "lectio-auth-browser": "services/lectio-auth-browser",
        "lectio-auth-lifecycle": "services/lectio-auth-lifecycle",
        "display-service": "services/display-service",
        "display-diagnostics": "services/display-service",
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
    assert compose["services"]["lectio-auth-browser"]["profiles"] == ["auth-browser"]
    assert compose["services"]["lectio-auth-browser"]["networks"] == [
        "lectio-auth-runtime"
    ]
    assert compose["services"]["lectio-auth-lifecycle"]["networks"] == [
        "lectio-auth-control"
    ]
    assert compose["services"]["lectio-gateway"]["environment"][
        "LECTIO_AUTH_BROWSER_URL"
    ] == "http://${COMPOSE_PROJECT_NAME:-better-lectio-ha-display}-lectio-auth-browser:8765"
    assert compose["services"]["lectio-gateway"]["environment"][
        "DISPLAY_DIAGNOSTICS_URL"
    ] == "http://display-diagnostics:8001"
    assert compose["services"]["lectio-auth-lifecycle"]["environment"][
        "LECTIO_AUTH_BROWSER_CONTAINER"
    ] == "${COMPOSE_PROJECT_NAME:-better-lectio-ha-display}-lectio-auth-browser"
    assert compose["services"]["lectio-auth-lifecycle"]["volumes"] == [
        "/var/run/docker.sock:/var/run/docker.sock"
    ]
    assert "/var/run/docker.sock:/var/run/docker.sock" not in compose["services"][
        "lectio-gateway"
    ].get("volumes", [])
    assert "lectio-auth-runtime" in compose["services"]["lectio-gateway"]["networks"]
    assert set(compose["services"]["lectio-gateway"]["depends_on"]) == {
        "lectio-auth-lifecycle",
        "display-diagnostics",
    }
    assert compose["services"]["display-service"]["ports"] == [
        "${DISPLAY_BIND_ADDRESS:-127.0.0.1}:${DISPLAY_HOST_PORT:-8001}:8000"
    ]
    assert "lectio-auth-control" in compose["services"]["lectio-gateway"]["networks"]
    diagnostics = compose["services"]["display-diagnostics"]
    assert diagnostics["networks"] == ["default"]
    assert diagnostics["expose"] == ["8001"]
    assert "ports" not in diagnostics
    assert diagnostics["volumes"] == [
        "display-service-data:/var/lib/better-lectio-display:ro"
    ]


def test_auth_endpoints_stay_on_loopback_and_display_port_is_configurable():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

    assert compose["services"]["display-service"]["ports"] == [
        "${DISPLAY_BIND_ADDRESS:-127.0.0.1}:${DISPLAY_HOST_PORT:-8001}:8000"
    ]
    for name, service in compose["services"].items():
        if name == "display-service":
            continue
        for binding in service.get("ports", []):
            assert binding.startswith("127.0.0.1:")
