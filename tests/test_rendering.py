import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from trmnl_schedule.events import Event
from trmnl_schedule.rendering import render_schedule


class RenderingTests(unittest.TestCase):
    def test_renderer_writes_monochrome_800_by_480_bmp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = render_schedule([], Path(directory) / "screen.bmp",
                                   datetime(2026, 9, 24, 8, tzinfo=ZoneInfo("Europe/Copenhagen")))
            with Image.open(path) as image:
                self.assertEqual(image.size, (800, 480))
                self.assertEqual(image.mode, "1")
                self.assertEqual(image.format, "BMP")

    def test_multiday_event_is_visible_during_each_overlapping_day(self):
        timezone = ZoneInfo("Europe/Copenhagen")
        event = Event("Conference", datetime(2026, 9, 23, tzinfo=timezone),
                      datetime(2026, 9, 26, tzinfo=timezone), "Calendar", all_day=True)
        drawn = []
        original = ImageDraw.ImageDraw.text

        def capture(draw, xy, text, *args, **kwargs):
            drawn.append(text)
            return original(draw, xy, text, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch.object(ImageDraw.ImageDraw, "text", capture):
            render_schedule([event], Path(directory) / "screen.bmp",
                            datetime(2026, 9, 24, 8, tzinfo=timezone))
        self.assertEqual(drawn.count("Conference"), 2)


if __name__ == "__main__":
    unittest.main()
