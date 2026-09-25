import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trmnl_schedule.events import Event
from trmnl_schedule.refresh import run_refresh

TZ = ZoneInfo("Europe/Copenhagen")


class RefreshTests(unittest.TestCase):
    def config(self, root):
        return {
            "IMAGE_PATH": root / "current.bmp", "CACHE_PATH": root / "events.json",
            "LECTIO_USERNAME": "student", "LECTIO_PASSWORD": "secret", "LECTIO_SCHOOL_ID": "42",
        }

    def test_cache_reuses_same_week_but_refetches_when_next_week_is_needed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            calls = []

            def fetch(**kwargs):
                calls.append((kwargs["iso_year"], kwargs["iso_week"]))
                return []

            run_refresh(config, datetime(2026, 9, 26, 23, 45, tzinfo=TZ), lectio_fetcher=fetch)
            run_refresh(config, datetime(2026, 9, 27, 0, 0, tzinfo=TZ), lectio_fetcher=fetch)
            self.assertEqual(calls, [(2026, 39), (2026, 39), (2026, 40)])

    def test_recent_lectio_events_are_reused_and_secret_is_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            now = datetime(2026, 9, 24, 7, 30, tzinfo=TZ)
            event = Event("Physics", now.replace(hour=8), now.replace(hour=9), "Lectio")
            calls = []

            def fetch(**kwargs):
                calls.append(kwargs["iso_week"])
                return [event]

            run_refresh(config, now, lectio_fetcher=fetch)

            def fail(**kwargs):
                raise AssertionError("fresh cache should have been used")

            run_refresh(config, now + timedelta(minutes=15), lectio_fetcher=fail)
            self.assertEqual(calls, [39])
            self.assertNotIn("secret", (root / "events.json").read_text())

    def test_malformed_cache_is_refetched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.config(root)
            (root / "events.json").write_text(json.dumps({"lectio": {
                "fingerprint": "bad", "fetched_at": "bad", "events": [{"broken": True}],
            }}))
            calls = []

            def fetch(**kwargs):
                calls.append(kwargs["iso_week"])
                return []

            run_refresh(config, datetime(2026, 9, 24, 7, tzinfo=TZ), lectio_fetcher=fetch)
            self.assertEqual(calls, [39])


if __name__ == "__main__":
    unittest.main()
