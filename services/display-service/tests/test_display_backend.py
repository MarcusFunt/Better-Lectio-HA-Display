import asyncio
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


class _SequencedModels:
    def __init__(self, results):
        self._results = iter(results)

    async def async_build(self):
        return next(self._results)


def _result(model, *, lectio, private, assignments):
    return DisplayModelResult(
        model=model,
        sources={
            "calendar.lectio": SourceStatus(state=lectio),
            "calendar.private": SourceStatus(state=private),
            "todo.lectio_assignments": SourceStatus(state=assignments),
            "todo.lectio_homework": SourceStatus(state="error"),
            "sensor.lectio_cancellations": SourceStatus(state="error"),
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
