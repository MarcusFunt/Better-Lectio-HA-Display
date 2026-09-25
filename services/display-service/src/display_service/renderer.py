"""Render the typed display model as the panel's fixed 1-bit BMP format."""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from .model import DisplayEvent, DisplayModel, DisplayTime, SidebarItem

WIDTH = 800
HEIGHT = 480
_INK = 0
_PAPER = 1
_LEFT = 20
_MAIN_RIGHT = 646
_SIDEBAR_LEFT = 660
_BOTTOM = 459
_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


@dataclass(frozen=True, slots=True)
class RenderedDisplay:
    """Immutable, content-addressed bitmap artifact for one display model."""

    bmp: bytes
    content_hash: str
    filename: str


def render_display(model: DisplayModel) -> RenderedDisplay:
    """Render a display model to a deterministic, content-addressed BMP."""
    image = Image.new("1", (WIDTH, HEIGHT), _PAPER)
    draw = ImageDraw.Draw(image)
    title_font = _font(24)
    day_font = _font(17)
    body_font = _font(14)
    small_font = _font(12)

    today = model.days[0].date if model.days else model.generated_at.date()
    _text(draw, (_LEFT, 15), f"SCHOOL DISPLAY  |  {today:%A %d %B %Y}", title_font)
    draw.line((_LEFT, 48, WIDTH - _LEFT, 48), fill=_INK, width=2)
    draw.line((_MAIN_RIGHT, 59, _MAIN_RIGHT, _BOTTOM), fill=_INK, width=1)

    _draw_schedule(draw, model, day_font, body_font, small_font)
    _draw_sidebar(draw, model.sidebar, day_font, body_font, small_font)

    output = BytesIO()
    image.save(output, format="BMP")
    image.close()
    bmp = output.getvalue()
    content_hash = sha256(bmp).hexdigest()
    return RenderedDisplay(
        bmp=bmp,
        content_hash=content_hash,
        filename=f"{content_hash}.bmp",
    )


def render_display_model(model: DisplayModel) -> bytes:
    """Compatibility wrapper for the existing device API integration."""
    return render_display(model).bmp


def _draw_schedule(draw, model, day_font, body_font, small_font) -> None:
    days = model.days[:3]
    if not days:
        _text(draw, (_LEFT, 68), "No schedule data", body_font)
        return

    section_top = 60
    section_height = (_BOTTOM - section_top) // len(days)
    for index, day in enumerate(days):
        top = section_top + index * section_height
        _text(draw, (_LEFT, top + 3), f"{day.date:%A %d %b}".upper(), day_font)
        draw.line((_LEFT, top + 24, _MAIN_RIGHT - 12, top + 24), fill=_INK, width=1)
        events = day.events
        if not events:
            _text(draw, (_LEFT + 8, top + 34), "No events", body_font)
            continue

        row_top = top + 29
        row_height = 35
        available_rows = max(1, (section_height - 30) // row_height)
        visible = events[:available_rows]
        for event in visible:
            time_label = _event_time(event, day.date)
            _text(draw, (_LEFT + 6, row_top), time_label, small_font)
            title_x = _LEFT + 112
            title_width = _MAIN_RIGHT - title_x - 14
            _text(draw, (title_x, row_top), _fit(draw, event.title, body_font, title_width), body_font)
            detail = " · ".join(
                value
                for value in (
                    _abbreviate_detail(event.teacher),
                    _abbreviate_detail(event.room),
                )
                if value
            )
            if detail:
                _text(
                    draw,
                    (title_x, row_top + 16),
                    _fit(draw, detail, small_font, title_width),
                    small_font,
                )
            row_top += row_height
        if len(events) > len(visible) and row_top + 14 < top + section_height:
            _text(
                draw,
                (_LEFT + 112, row_top),
                f"+ {len(events) - len(visible)} more",
                small_font,
            )


def _draw_sidebar(draw, items: tuple[SidebarItem, ...], day_font, body_font, small_font) -> None:
    x = _SIDEBAR_LEFT
    width = WIDTH - x - _LEFT
    _text(draw, (x, 61), "UP NEXT", day_font)
    y = 84
    groups = (
        ("CANCELLATIONS", "cancellation"),
        ("ASSIGNMENTS", "assignment"),
        ("HOMEWORK", "homework"),
    )
    item_count = len(items)
    visible_count = 0
    footer_y = _BOTTOM - 13
    overflow = False
    for label, kind in groups:
        group = [item for item in items if item.kind == kind]
        if not group:
            empty_height = 34
            reserve_footer = 13 if item_count > visible_count else 0
            if y + empty_height + reserve_footer <= _BOTTOM:
                _text(draw, (x, y), label, small_font)
                y += 17
                _text(draw, (x + 4, y), "None", small_font)
                y += 17
            continue

        first_secondary = group[0].subtitle or _display_date(group[0].when)
        first_height = 16 + (14 if first_secondary else 0) + 3
        reserve_footer = 13 if item_count - visible_count > 1 else 0
        if y + 17 + first_height + reserve_footer > _BOTTOM:
            overflow = True
            break
        _text(draw, (x, y), label, small_font)
        y += 17
        for item in group:
            secondary = item.subtitle or _display_date(item.when)
            item_height = 16 + (14 if secondary else 0) + 3
            remaining = item_count - visible_count - 1
            reserve_footer = 13 if remaining else 0
            if y + item_height + reserve_footer > _BOTTOM:
                overflow = True
                break
            _text(draw, (x + 4, y), _fit(draw, item.title, body_font, width - 6), body_font)
            y += 16
            if secondary:
                _text(draw, (x + 4, y), _fit(draw, secondary, small_font, width - 6), small_font)
                y += 14
            y += 3
            visible_count += 1
        if overflow:
            break

    if overflow:
        omitted = item_count - visible_count
        _text(draw, (x + 4, footer_y), f"+{omitted} more", small_font)


def _event_time(event: DisplayEvent, day: date) -> str:
    if event.all_day:
        return "ALL DAY"
    if not isinstance(event.start, datetime) or not isinstance(event.end, datetime):
        return "ALL DAY"
    start = event.start
    end = event.end
    start_label = start.strftime("%H:%M") if start.date() == day else "..."
    end_label = end.strftime("%H:%M") if end.date() == day else "..."
    return f"{start_label}-{end_label}"


def _display_date(value: DisplayTime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%d %b")


def _abbreviate_detail(value: str | None) -> str:
    """Compact multiword teacher and room labels while keeping the final word."""
    if not value:
        return ""
    words = value.split()
    if len(words) < 2:
        return " ".join(words)
    shortened = [
        word
        if (word.endswith(".") and len(word) <= 3) or not word.isalpha()
        else f"{word[0]}."
        for word in words[:-1]
    ]
    return " ".join((*shortened, words[-1]))


def _font(size: int) -> ImageFont.FreeTypeFont:
    """Load the same DejaVu Sans face in local and container deployments."""
    font_path = os.getenv("DISPLAY_FONT_PATH", _FONT_PATH)
    return ImageFont.truetype(font_path, size)


def _fit(draw, value: str, font, max_width: int) -> str:
    text = " ".join(value.split())
    if _text_width(draw, text, font) <= max_width:
        return text
    ellipsis = "..."
    while text and _text_width(draw, text + ellipsis, font) > max_width:
        text = text[:-1]
    return text.rstrip() + ellipsis


def _text_width(draw, value: str, font) -> float:
    try:
        return draw.textlength(value, font=font)
    except UnicodeEncodeError:
        return draw.textlength(_ascii_fallback(value), font=font)


def _text(draw, position, value: str, font) -> None:
    try:
        draw.text(position, value, font=font, fill=_INK)
    except UnicodeEncodeError:
        draw.text(position, _ascii_fallback(value), font=font, fill=_INK)


def _ascii_fallback(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", errors="replace").decode("ascii")
