from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_builds_gateway_and_display_as_separate_services():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

    assert set(compose["services"]) == {
        "lectio-gateway",
        "lectio-auth-browser",
        "lectio-auth-lifecycle",
        "lectio-ha-api",
        "display-service",
        "display-diagnostics",
    }
    for name, path in {
        "lectio-gateway": "services/lectio-gateway",
        "lectio-auth-browser": "services/lectio-auth-browser",
        "lectio-auth-lifecycle": "services/lectio-auth-lifecycle",
        "lectio-ha-api": "services/lectio-gateway",
        "display-service": "services/display-service",
        "display-diagnostics": "services/display-service",
    }.items():
        service = compose["services"][name]
        assert service["build"]["context"] == f"./{path}"
        assert (ROOT / path / "Dockerfile").is_file()
        assert service["healthcheck"]["test"]

    assert set(compose["volumes"]) == {
        "lectio-gateway-data",
        "display-service-data",
        "ha-display-config",
        "lectio-ha-api-auth",
    }
    assert compose["services"]["lectio-gateway"]["ports"] == [
        "127.0.0.1:8000:8000"
    ]
    ha_api = compose["services"]["lectio-ha-api"]
    assert ha_api["command"] == [
        "uvicorn",
        "lectio_gateway.ha_api:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8001",
    ]
    assert ha_api["ports"] == [
        "${LECTIO_HA_API_BIND_ADDRESS:-127.0.0.1}:${LECTIO_HA_API_HOST_PORT:-8002}:8001"
    ]
    assert ha_api["environment"]["LECTIO_HA_API_TOKEN"] == "${LECTIO_HA_API_TOKEN:-}"
    assert ha_api["environment"]["LECTIO_HA_API_AUTH_DIR"] == "/var/lib/better-lectio-api-auth"
    assert ha_api["volumes"] == [
        "lectio-ha-api-auth:/var/lib/better-lectio-api-auth:ro"
    ]
    assert ha_api["expose"] == ["8001"]
    assert ha_api["depends_on"] == {"lectio-gateway": {"condition": "service_healthy"}}
    assert compose["services"]["lectio-auth-browser"]["ports"] == [
        "127.0.0.1:6080:6080"
    ]
    assert compose["services"]["lectio-auth-browser"]["profiles"] == ["auth-browser"]
    assert compose["services"]["lectio-ha-api"]["ports"] == [
        "${LECTIO_HA_API_BIND_ADDRESS:-127.0.0.1}:${LECTIO_HA_API_HOST_PORT:-8002}:8001"
    ]
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
    assert compose["services"]["lectio-gateway"]["volumes"] == [
        "lectio-gateway-data:/var/lib/better-lectio",
        "ha-display-config:/var/lib/better-lectio-ha-setup",
        "lectio-ha-api-auth:/var/lib/better-lectio-api-auth",
    ]
    assert compose["services"]["display-service"]["volumes"] == [
        "display-service-data:/var/lib/better-lectio-display",
        "ha-display-config:/var/lib/better-lectio-ha-setup:ro",
    ]
    gateway_environment = compose["services"]["lectio-gateway"]["environment"]
    assert gateway_environment["LECTIO_HA_API_BIND_ADDRESS"] == "${LECTIO_HA_API_BIND_ADDRESS:-127.0.0.1}"
    assert gateway_environment["LECTIO_HA_API_HOST_PORT"] == "${LECTIO_HA_API_HOST_PORT:-8002}"
    assert compose["services"]["lectio-ha-api"]["environment"]["LECTIO_HA_API_AUTH_DIR"] == "/var/lib/better-lectio-api-auth"
    assert compose["services"]["display-service"]["environment"]["HOME_ASSISTANT_CONFIG_DIR"] == "/var/lib/better-lectio-ha-setup"
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
            if name == "lectio-ha-api":
                assert binding.startswith("${LECTIO_HA_API_BIND_ADDRESS:-127.0.0.1}:")
            else:
                assert binding.startswith("127.0.0.1:")


def test_display_service_exposes_home_assistant_entity_role_overrides():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    environment = compose["services"]["display-service"]["environment"]

    assert {
        key: environment[key]
        for key in (
            "HA_LECTIO_CALENDAR",
            "HA_PRIVATE_CALENDARS",
            "HA_ASSIGNMENTS_TODO",
            "HA_HOMEWORK_TODO",
            "HA_CANCELLATIONS_SENSOR",
        )
    } == {
        "HA_LECTIO_CALENDAR": "${HA_LECTIO_CALENDAR:-calendar.lectio}",
        "HA_PRIVATE_CALENDARS": "${HA_PRIVATE_CALENDARS-calendar.private}",
        "HA_ASSIGNMENTS_TODO": "${HA_ASSIGNMENTS_TODO:-todo.lectio_assignments}",
        "HA_HOMEWORK_TODO": "${HA_HOMEWORK_TODO:-todo.lectio_homework}",
        "HA_CANCELLATIONS_SENSOR": "${HA_CANCELLATIONS_SENSOR:-sensor.lectio_cancellations}",
    }
