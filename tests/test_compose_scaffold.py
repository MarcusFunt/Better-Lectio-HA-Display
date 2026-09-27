from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_connects_display_to_private_gateway_without_startup_cycle():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]

    assert set(services) == {
        "lectio-gateway", "lectio-auth-browser", "lectio-auth-lifecycle",
        "display-service", "display-diagnostics",
    }
    assert services["display-service"]["environment"]["LECTIO_GATEWAY_URL"] == "http://lectio-gateway:8000"
    assert "depends_on" not in services["display-service"]
    assert set(services["lectio-gateway"]["depends_on"]) == {"lectio-auth-lifecycle", "display-diagnostics"}
    assert services["display-diagnostics"]["depends_on"] == {"display-service": {"condition": "service_healthy"}}
    assert set(compose["volumes"]) == {"lectio-gateway-data", "display-service-data"}
    assert services["lectio-gateway"]["volumes"] == ["lectio-gateway-data:/var/lib/better-lectio"]
    assert services["display-service"]["volumes"] == ["display-service-data:/var/lib/better-lectio-display"]
    assert services["display-diagnostics"]["volumes"] == ["display-service-data:/var/lib/better-lectio-display:ro"]
    assert services["display-diagnostics"]["networks"] == ["default"]
    assert "ports" not in services["display-diagnostics"]


def test_compose_retains_device_and_auth_bindings_without_ha_configuration():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    assert services["display-service"]["ports"] == [
        "${DISPLAY_BIND_ADDRESS:-127.0.0.1}:${DISPLAY_HOST_PORT:-8001}:8000"
    ]
    assert services["lectio-gateway"]["ports"] == ["127.0.0.1:8000:8000"]
    assert services["lectio-auth-browser"]["ports"] == ["127.0.0.1:6080:6080"]
    assert services["lectio-auth-browser"]["profiles"] == ["auth-browser"]
    assert services["lectio-auth-lifecycle"]["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock"]
    assert not any("HOME_ASSISTANT" in key or key.startswith("HA_") or "HA_API" in key
                   for service in services.values() for key in service.get("environment", {}))
    for name, path in {
        "lectio-gateway": "services/lectio-gateway",
        "lectio-auth-browser": "services/lectio-auth-browser",
        "lectio-auth-lifecycle": "services/lectio-auth-lifecycle",
        "display-service": "services/display-service",
        "display-diagnostics": "services/display-service",
    }.items():
        assert services[name]["build"]["context"] == f"./{path}"
        assert (ROOT / path / "Dockerfile").is_file()
        assert services[name]["healthcheck"]["test"]


def test_ci_runs_service_checks_without_hacs_validation():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    assert set(workflow["jobs"]) == {"firmware", "checks"}
    check_steps = workflow["jobs"]["checks"]["steps"]
    commands = "\n".join(step.get("run", "") for step in check_steps)
    assert "make test" in commands
    assert "make lint" in commands
    assert "make compose-config" in commands
    assert "make compose-build" in commands
