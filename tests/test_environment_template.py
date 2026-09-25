from pathlib import Path


def test_environment_template_has_no_lectio_password_or_raw_ics_settings():
    template = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text()

    assert "LECTIO_PASSWORD" not in template
    assert "LECTIO_USERNAME" not in template
    assert "CALENDAR_ICS_URL" not in template


def test_environment_template_leaves_home_assistant_token_empty():
    lines = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text().splitlines()
    values = {
        key: value
        for line in lines
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
        for key, value in [line.split("=", maxsplit=1)]
    }

    assert values["HOME_ASSISTANT_TOKEN"] == ""
