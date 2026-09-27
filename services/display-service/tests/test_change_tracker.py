import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from display_service.change_tracker import DisplayChangeTracker
from display_service.model_builder import build_display_model
from display_service.model_service import SourceStatus

TZ = ZoneInfo("Europe/Copenhagen")


def _model(*, now, events=()):
    return build_display_model(now=now, lectio_events=events)


def _fresh():
    return {"schedule": SourceStatus(state="valid")}


def test_change_tracker_detects_and_persists_changes_until_acknowledged(tmp_path):
    path = tmp_path / "plan-review.json"
    tracker = DisplayChangeTracker(path)
    now = datetime(2026, 9, 25, 7, 0, tzinfo=TZ)
    first = _model(
        now=now,
        events=[
            {
                "id": "lesson-1",
                "subject": "Math",
                "start": "2026-09-25T09:00:00+02:00",
                "end": "2026-09-25T09:45:00+02:00",
            }
        ],
    )

    assert tracker.update(first, _fresh()).total == 0

    changed = _model(
        now=now,
        events=[
            {
                "id": "lesson-1",
                "subject": "Math moved",
                "start": "2026-09-25T10:00:00+02:00",
                "end": "2026-09-25T10:45:00+02:00",
            }
        ],
    )
    summary = tracker.update(changed, _fresh())

    assert (summary.added, summary.changed, summary.removed, summary.total) == (
        0,
        1,
        0,
        1,
    )
    assert DisplayChangeTracker(path).summary == summary
    assert tracker.acknowledge().total == 0
    assert DisplayChangeTracker(path).summary.total == 0


def test_persisted_change_state_does_not_store_event_text_or_source_ids(tmp_path):
    path = tmp_path / "plan-review.json"
    tracker = DisplayChangeTracker(path)
    model = _model(
        now=datetime(2026, 9, 25, 7, 0, tzinfo=TZ),
        events=[
            {
                "id": "secret-source-record-identifier",
                "subject": "Secret lesson text",
                "start": "2026-09-26T09:00:00+02:00",
                "end": "2026-09-26T09:45:00+02:00",
            }
        ],
    )

    tracker.update(model, _fresh())
    stored = path.read_text(encoding="utf-8")

    assert "Secret lesson text" not in stored
    assert "secret-source-record-identifier" not in stored


def test_unreadable_review_state_disables_tracking_without_resetting_baseline(
    tmp_path,
):
    path = tmp_path / "plan-review.json"
    path.write_text("{broken", encoding="utf-8")
    tracker = DisplayChangeTracker(path)

    assert tracker.available is False
    assert tracker.summary.available is False
    assert tracker.acknowledge() is None
    with pytest.raises(RuntimeError, match="Plan review state is unavailable"):
        tracker.update(
            _model(now=datetime(2026, 9, 25, 7, 0, tzinfo=TZ)),
            _fresh(),
        )
    assert path.read_text(encoding="utf-8") == "{broken"


def test_stale_source_does_not_create_false_removals(tmp_path):
    tracker = DisplayChangeTracker(tmp_path / "plan-review.json")
    now = datetime(2026, 9, 25, 7, 0, tzinfo=TZ)
    event = {
        "id": "lesson-1",
        "subject": "Math",
        "start": "2026-09-26T09:00:00+02:00",
        "end": "2026-09-26T09:45:00+02:00",
    }
    tracker.update(_model(now=now, events=[event]), _fresh())

    empty = _model(now=now)
    stale = {"schedule": SourceStatus(state="stale", is_stale=True)}
    assert tracker.update(empty, stale).total == 0

    assert tracker.update(empty, _fresh()).removed == 1


def test_items_that_age_out_of_the_three_day_window_are_not_reported_removed(
    tmp_path,
):
    tracker = DisplayChangeTracker(tmp_path / "plan-review.json")
    before = datetime(2026, 9, 25, 7, 0, tzinfo=TZ)
    yesterday = {
        "id": "lesson-1",
        "subject": "Yesterday's math",
        "start": "2026-09-25T09:00:00+02:00",
        "end": "2026-09-25T09:45:00+02:00",
    }
    tracker.update(_model(now=before, events=[yesterday]), _fresh())

    after_window = datetime(2026, 9, 28, 7, 0, tzinfo=TZ)
    summary = tracker.update(_model(now=after_window), _fresh())

    assert summary.total == 0


def test_event_removed_while_still_in_today_is_reported_but_midnight_expiry_is_not(
    tmp_path,
):
    tracker = DisplayChangeTracker(tmp_path / "plan-review.json")
    before = datetime(2026, 9, 25, 7, 0, tzinfo=TZ)
    still_relevant = {
        "id": "lesson-today",
        "subject": "Math",
        "start": "2026-09-25T09:00:00+02:00",
        "end": "2026-09-25T09:45:00+02:00",
    }
    tracker.update(_model(now=before, events=[still_relevant]), _fresh())
    after_change = datetime(2026, 9, 25, 8, 0, tzinfo=TZ)
    assert tracker.update(_model(now=after_change), _fresh()).removed == 1

    midnight_tracker = DisplayChangeTracker(tmp_path / "midnight-review.json")
    ending_at_midnight = {
        "id": "lesson-midnight",
        "subject": "Late study",
        "start": "2026-09-25T23:00:00+02:00",
        "end": "2026-09-26T00:00:00+02:00",
    }
    midnight_tracker.update(_model(now=before, events=[ending_at_midnight]), _fresh())
    next_day = datetime(2026, 9, 26, 7, 0, tzinfo=TZ)

    assert midnight_tracker.update(_model(now=next_day), _fresh()).total == 0


def test_homework_aging_out_after_its_due_time_is_not_reported_removed(tmp_path):
    tracker = DisplayChangeTracker(tmp_path / "plan-review.json")
    before = datetime(2026, 9, 25, 7, 0, tzinfo=TZ)
    initial = build_display_model(
        now=before,
        homework=[
            {
                "id": "homework-1",
                "subject": "Math",
                "description": "Practice",
                "target_lesson_start": "2026-09-25T09:00:00+02:00",
            }
        ],
    )
    tracker.update(
        initial,
        {"homework": SourceStatus(state="valid")},
    )
    after_due = datetime(2026, 9, 25, 10, 0, tzinfo=TZ)
    empty = build_display_model(now=after_due)

    assert (
        tracker.update(
            empty,
            {"homework": SourceStatus(state="valid")},
            ).total
        == 0
    )


def test_upgrade_prunes_retired_ha_source_keys_without_spurious_changes(tmp_path):
    path = tmp_path / "plan-review.json"
    item = {"fingerprint": "a" * 64, "expires_on": None, "expires_inclusive": False}
    path.write_text(json.dumps({
        "version": 1,
        "acknowledged": {"calendar.lectio": {}, "calendar.private": {}},
        "current": {"calendar.lectio": {"old": item}, "calendar.private": {"private": item}},
        "acknowledged_at": None,
    }), encoding="utf-8")
    tracker = DisplayChangeTracker(path)
    assert tracker.summary.total == 0
    tracker.update(_model(now=datetime(2026, 9, 25, 7, tzinfo=TZ)), _fresh())
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert set(persisted["current"]) == {"schedule"}
    assert set(persisted["acknowledged"]) == {"schedule"}
