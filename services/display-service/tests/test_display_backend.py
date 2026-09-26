import asyncio
import json
from datetime import date, datetime, timezone

from display_service.main import DisplayBackend
from display_service.model import DisplayDay, DisplayEvent, DisplayModel
from display_service.model_service import DisplayModelResult, SourceStatus
from display_service.renderer import render_display_model


def test_restart_with_unavailable_calendar_sources_retains_image_until_recovery(tmp_path):
    backend = DisplayBackend(tmp_path)
    previous_model = _model("Last known-good lesson")
    previous = backend.images.publish(
        render_display_model(previous_model), previous_model.generated_at
    )

    # A new backend and model service simulate a process restart, so their
    # per-source in-memory caches are empty while the rendered image persists.
    restarted = DisplayBackend(tmp_path)
    models = _SequencedModels(
        (
            _result(
                _model(),
                lectio="error",
                private="valid",
                assignments="valid",
            ),
            _result(
                _model(),
                lectio="error",
                private="error",
                assignments="valid",
            ),
            _result(
                _model("Recovered lesson"),
                lectio="valid",
                private="valid",
                assignments="valid",
            ),
        )
    )
    restarted._models = models

    async def run():
        await restarted.refresh_if_due()
        assert restarted.images.current == previous

        restarted._next_refresh_at = 0
        await restarted.refresh_if_due()
        assert restarted.images.current == previous

        restarted._next_refresh_at = 0
        await restarted.refresh_if_due()
        assert restarted.images.current is not None
        assert restarted.images.current.content_hash != previous.content_hash

    asyncio.run(run())


def test_display_backend_reports_home_assistant_connection_state(tmp_path):
    backend = DisplayBackend(tmp_path)
    assert _setup_status(tmp_path)["state"] == "not_configured"

    backend._models = _SequencedModels(
        (
            _result(
                _model("Connected lesson"),
                lectio="valid",
                private="valid",
                assignments="valid",
                homework="valid",
                cancellations="valid",
            ),
        )
    )

    async def run():
        await backend.refresh_if_due()

    asyncio.run(run())

    status = _setup_status(tmp_path)
    assert status["state"] == "connected"
    assert "last_checked_at" in status
    assert "token" not in status


def test_display_backend_reports_rejected_home_assistant_token(tmp_path):
    backend = DisplayBackend(tmp_path)
    backend._models = _SequencedModels(
        (
            _result(
                _model(),
                lectio="error",
                private="error",
                assignments="error",
                error="unauthorized",
            ),
        )
    )

    async def run():
        await backend.refresh_if_due()

    asyncio.run(run())

    assert _setup_status(tmp_path)["state"] == "unauthorized"


def test_runtime_reloads_managed_configuration_and_keeps_last_image_on_corruption(
    monkeypatch, tmp_path
):
    from display_service import main
    from display_service.main import HomeAssistantRuntime

    clients = []

    class FakeHomeAssistantClient:
        def __init__(self, url, token):
            self.url = url
            self.token = token
            self.closed = False
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

    monkeypatch.setattr(main, "HomeAssistantClient", FakeHomeAssistantClient)
    config_dir = tmp_path / "shared-config"
    config_dir.mkdir()
    config_path = config_dir / "home-assistant-display.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "ha_url": "http://first.local:8123",
                "ha_token": "first-secret",
                "entities": {"lectio_calendar": "calendar.school"},
            }
        ),
        encoding="utf-8",
    )
    backend = DisplayBackend(tmp_path / "display")
    previous_model = _model("Last known-good lesson")
    previous = backend.images.publish(
        render_display_model(previous_model), previous_model.generated_at
    )
    runtime = HomeAssistantRuntime(backend, config_dir, {})

    async def run():
        await runtime.reload_configuration()
        assert backend._models is not None
        assert backend._entity_config.lectio_calendar_entity_id == "calendar.school"

        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "ha_url": "http://second.local:8123",
                    "ha_token": "second-secret",
                    "entities": {"lectio_calendar": "calendar.new_school"},
                }
            ),
            encoding="utf-8",
        )
        await runtime.reload_configuration()
        assert clients[0].closed
        assert clients[-1].url == "http://second.local:8123"
        assert clients[-1].token == "second-secret"
        assert backend._entity_config.lectio_calendar_entity_id == "calendar.new_school"

        config_path.write_text("{broken", encoding="utf-8")
        await runtime.reload_configuration()
        assert clients[-1].closed
        assert backend._models is None
        assert _setup_status(tmp_path / "display")["state"] == "invalid_configuration"
        assert backend.images.current == previous

        await runtime.close()

    asyncio.run(run())


def _setup_status(data_dir):
    return json.loads(
        (data_dir / "home-assistant-setup.json").read_text(encoding="utf-8")
    )


class _SequencedModels:
    def __init__(self, results):
        self._results = iter(results)

    async def async_build(self):
        return next(self._results)


def _result(
    model,
    *,
    lectio,
    private,
    assignments,
    homework="error",
    cancellations="error",
    error=None,
):
    return DisplayModelResult(
        model=model,
        sources={
            "calendar.lectio": SourceStatus(state=lectio, error=error),
            "calendar.private": SourceStatus(state=private, error=error),
            "todo.lectio_assignments": SourceStatus(state=assignments, error=error),
            "todo.lectio_homework": SourceStatus(state=homework, error=error),
            "sensor.lectio_cancellations": SourceStatus(
                state=cancellations, error=error
            ),
        },
    )


def _model(title=None):
    generated_at = datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc)
    events = ()
    if title:
        events = (
            DisplayEvent(
                id="lesson",
                start=datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc),
                end=datetime(2026, 9, 25, 8, 45, tzinfo=timezone.utc),
                title=title,
                source="lectio",
                all_day=False,
            ),
        )
    return DisplayModel(
        generated_at=generated_at,
        days=(
            DisplayDay(date=date(2026, 9, 25), events=events),
            DisplayDay(date=date(2026, 9, 26), events=()),
            DisplayDay(date=date(2026, 9, 27), events=()),
        ),
        sidebar=(),
    )
