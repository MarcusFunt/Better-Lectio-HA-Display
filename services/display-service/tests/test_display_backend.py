import asyncio
from datetime import date, datetime, timezone

from display_service.main import DisplayBackend
from display_service.model import DisplayDay, DisplayEvent, DisplayModel
from display_service.model_service import DisplayModelResult, SourceStatus
from display_service.renderer import render_display_model


class SequencedModels:
    def __init__(self, results):
        self.results = iter(results)

    async def async_build(self):
        return next(self.results)


def model(title=None):
    generated_at = datetime(2026, 9, 25, 6, tzinfo=timezone.utc)
    events = () if title is None else (
        DisplayEvent(
            id="lectio:lesson-1",
            start=datetime(2026, 9, 25, 8, tzinfo=timezone.utc),
            end=datetime(2026, 9, 25, 8, 45, tzinfo=timezone.utc),
            title=title,
            source="lectio",
            all_day=False,
        ),
    )
    return DisplayModel(
        generated_at=generated_at,
        days=(DisplayDay(date(2026, 9, 25), events), DisplayDay(date(2026, 9, 26), ()), DisplayDay(date(2026, 9, 27), ())),
        sidebar=(),
    )


def result(display_model, *, schedule="valid", assignments="valid", homework="valid", cancellations="valid"):
    return DisplayModelResult(
        model=display_model,
        sources={source: SourceStatus(state=state, is_stale=state == "stale", error="connection_failed" if state in {"stale", "error"} else None)
                 for source, state in (("schedule", schedule), ("assignments", assignments), ("homework", homework), ("cancellations", cancellations))},
    )


def test_gateway_outage_retains_last_rendered_bitmap_and_retries(tmp_path):
    backend = DisplayBackend(tmp_path)
    previous_model = model("Last known-good lesson")
    previous = backend.images.publish(render_display_model(previous_model), previous_model.generated_at)
    backend._models = SequencedModels((
        result(model(), schedule="error"),
        result(model(), schedule="error", assignments="error", homework="error", cancellations="error"),
        result(model(), schedule="stale"),
        result(model("Recovered lesson")),
    ))

    async def run():
        await backend.refresh_if_due()
        assert backend.images.current.content_hash == previous.content_hash
        backend._next_refresh_at = 0
        await backend.refresh_if_due()
        assert backend.images.current.content_hash == previous.content_hash
        backend._next_refresh_at = 0
        await backend.refresh_if_due()
        assert backend.images.current.content_hash == previous.content_hash
        backend._next_refresh_at = 0
        await backend.refresh_if_due()
        assert backend.images.current.content_hash != previous.content_hash

    asyncio.run(run())


def test_partial_gateway_failure_uses_stale_source_and_tracks_fresh_changes(tmp_path):
    backend = DisplayBackend(tmp_path)
    backend._models = SequencedModels((
        result(model("Math")),
        result(model("Physics"), assignments="stale"),
    ))

    async def run():
        await backend.refresh_if_due()
        first = backend.images.current
        backend._next_refresh_at = 0
        await backend.refresh_if_due()
        assert backend.images.current.content_hash != first.content_hash
        assert backend.changes.summary.changed == 1

    asyncio.run(run())


def test_partial_schedule_across_iso_week_boundary_keeps_complete_bitmap(tmp_path):
    backend = DisplayBackend(tmp_path)
    monday_lesson = DisplayEvent(
        id="lectio:monday",
        start=datetime(2026, 9, 28, 8, tzinfo=timezone.utc),
        end=datetime(2026, 9, 28, 8, 45, tzinfo=timezone.utc),
        title="Monday mathematics",
        source="lectio",
        all_day=False,
    )
    full = DisplayModel(
        generated_at=datetime(2026, 9, 27, 6, tzinfo=timezone.utc),
        days=(DisplayDay(date(2026, 9, 27), ()), DisplayDay(date(2026, 9, 28), (monday_lesson,)), DisplayDay(date(2026, 9, 29), ())),
        sidebar=(),
    )
    partial = DisplayModel(
        generated_at=full.generated_at,
        days=tuple(DisplayDay(day.date, ()) for day in full.days),
        sidebar=(),
    )
    previous = backend.images.publish(render_display_model(full), full.generated_at)
    backend._models = SequencedModels((result(partial, schedule="stale"),))

    asyncio.run(backend.refresh_if_due())

    assert backend.images.current.content_hash == previous.content_hash
