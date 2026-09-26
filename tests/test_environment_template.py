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


def test_environment_template_documents_home_assistant_entity_roles():
    template = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text()
    values = {
        key: value
        for line in template.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
        for key, value in [line.split("=", maxsplit=1)]
    }

    assert values["HA_LECTIO_CALENDAR"] == "calendar.lectio"
    assert values["HA_PRIVATE_CALENDARS"] == "calendar.private"
    assert values["HA_ASSIGNMENTS_TODO"] == "todo.lectio_assignments"
    assert values["HA_HOMEWORK_TODO"] == "todo.lectio_homework"
    assert values["HA_CANCELLATIONS_SENSOR"] == "sensor.lectio_cancellations"
