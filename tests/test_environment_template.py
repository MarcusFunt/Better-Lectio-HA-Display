from pathlib import Path


def test_environment_template_has_only_runtime_bind_and_login_settings():
    template = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text()
    values = {
        key: value
        for line in template.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
        for key, value in [line.split("=", maxsplit=1)]
    }
    assert set(values) == {
        "TIMEZONE", "DISPLAY_BIND_ADDRESS", "DISPLAY_HOST_PORT",
        "LECTIO_AUTH_BROWSER_VIEW_URL", "LECTIO_AUTH_TIMEOUT_SECONDS",
    }
    assert values["DISPLAY_BIND_ADDRESS"] == "127.0.0.1"
    assert "LECTIO_PASSWORD" not in template
    assert "LECTIO_USERNAME" not in template
    assert "CALENDAR_ICS_URL" not in template
