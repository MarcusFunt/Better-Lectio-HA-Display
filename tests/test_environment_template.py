from pathlib import Path


def test_environment_template_has_no_lectio_password_or_raw_ics_settings():
    template = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text()

    assert "LECTIO_PASSWORD" not in template
    assert "LECTIO_USERNAME" not in template
    assert "CALENDAR_ICS_URL" not in template


def test_environment_template_keeps_home_assistant_secrets_on_the_login_page():
    lines = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text().splitlines()
    values = {
        key: value
        for line in lines
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
        for key, value in [line.split("=", maxsplit=1)]
    }

    assert "HOME_ASSISTANT_URL" not in values
    assert "HOME_ASSISTANT_TOKEN" not in values
    assert "LECTIO_HA_API_TOKEN" not in values
    assert values["LECTIO_HA_API_BIND_ADDRESS"] == "127.0.0.1"
    assert values["LECTIO_HA_API_HOST_PORT"] == "8002"


def test_environment_template_leaves_entity_roles_to_the_login_page():
    template = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text()
    values = {
        key: value
        for line in template.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
        for key, value in [line.split("=", maxsplit=1)]
    }

    assert not any(key.startswith("HA_") for key in values)
