import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .images import preserve_image_version

WIDTH = 800
HEIGHT = 480
INK = 0
PAPER = 1


def _font(size, bold=False):
    choices = (
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf")
        if bold else
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf")
    )
    for name in choices:
        if Path(name).exists():
            return ImageFont.truetype(name, size=size)
    return ImageFont.load_default()


def _fit_text(draw, text, font, width):
    text = " ".join(str(text).split())
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "..."


def _time_text(event):
    return "ALL DAY" if event.all_day else f"{event.starts_at:%H:%M}–{event.ends_at:%H:%M}"


def _time_column_width(draw, value, font):
    return max(132, draw.textlength(value, font=font) + 14)


def _draw_column(draw, x, width, title, events, today, tomorrow, regular, bold, timezone):
    draw.text((x, 99), title, font=bold, fill=INK)
    draw.line((x, 128, x + width, 128), fill=INK, width=2)
    y, visible = 139, 0
    bottom = HEIGHT - 26
    for label, day in (("TODAY", today), ("TOMORROW", tomorrow)):
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone)
        day_end = day_start + timedelta(days=1)
        day_events = [e for e in events if e.starts_at < day_end and e.ends_at > day_start]
        if y + 25 > bottom:
            return
        draw.text((x, y), f"{label} · {day:%a %d %b}".upper(), font=regular, fill=INK)
        y += 25
        if not day_events:
            if y + 21 > bottom:
                return
            draw.text((x + 8, y), "No events", font=regular, fill=INK)
            y += 25
            continue
        for event in day_events:
            if visible >= 6 or y + 43 > bottom:
                if y + 20 <= bottom:
                    draw.text((x + 8, y), "More events on the next refresh", font=regular, fill=INK)
                return
            time_text = _time_text(event)
            draw.text((x + 8, y), time_text, font=regular, fill=INK)
            time_width = _time_column_width(draw, time_text, regular)
            title_text = _fit_text(draw, event.title, regular, width - time_width - 8)
            draw.text((x + time_width, y), title_text, font=regular, fill=INK)
            if event.location or event.details:
                detail = " · ".join(part for part in (event.location, event.details) if part)
                draw.text((x + time_width, y + 19), _fit_text(draw, detail, regular, width - time_width - 8), font=regular, fill=INK)
            draw.line((x + 8, y + 43, x + width - 8, y + 43), fill=INK, width=1)
            y += 48
            visible += 1
        y += 5


def render_schedule(events, image_path, now=None, timezone_name="Europe/Copenhagen"):
    timezone = ZoneInfo(timezone_name)
    now = now or datetime.now(timezone)
    now = now.replace(tzinfo=timezone) if now.tzinfo is None else now.astimezone(timezone)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    events = sorted(events, key=lambda event: event.starts_at)
    image = Image.new("1", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)
    regular, bold = _font(18), _font(24, bold=True)
    header, eyebrow = _font(30, bold=True), _font(14)
    draw.text((28, 18), f"{today:%A}, {today:%d %B %Y}".upper(), font=header, fill=INK)
    draw.text((30, 59), "LECTIO SCHEDULE  +  CALENDAR", font=eyebrow, fill=INK)
    draw.text((WIDTH - 183, 59), f"UPDATED {now:%H:%M}", font=eyebrow, fill=INK)
    draw.line((24, 84, WIDTH - 24, 84), fill=INK, width=2)
    draw.line((400, 96, 400, 454), fill=INK, width=1)
    lectio = [event for event in events if event.source.casefold() == "lectio"]
    calendar = [event for event in events if event.source.casefold() != "lectio"]
    _draw_column(draw, 28, 350, "LECTIO", lectio, today, tomorrow, regular, bold, timezone)
    _draw_column(draw, 424, 348, "CALENDAR", calendar, today, tomorrow, regular, bold, timezone)

    target = Path(image_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False) as tmp:
            temporary_path = Path(tmp.name)
        image.save(temporary_path, format="BMP")
        preserve_image_version(temporary_path.read_bytes(), target)
        os.replace(temporary_path, target)
    finally:
        image.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target
