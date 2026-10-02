"""Render the typed display model as the panel's fixed 1-bit BMP format."""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .model import DisplayEvent, DisplayModel, DisplayTime, SidebarItem

WIDTH = 800
HEIGHT = 480
_INK = 0
_PAPER = 1
_LEFT = 20
_MAIN_RIGHT = 622
_SIDEBAR_LEFT = 638
_BOTTOM = 459
_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_DISPLAY_TIMEZONE = ZoneInfo("Europe/Copenhagen")


@dataclass(frozen=True, slots=True)
class RenderedDisplay:
    """Immutable, content-addressed bitmap artifact for one display model."""

    bmp: bytes
    content_hash: str
    filename: str


def render_display(
    model: DisplayModel,
    *,
    change_count: int = 0,
    acknowledged_at: str | None = None,
    show_status: bool = False,
    data_stale: bool = False,
) -> RenderedDisplay:
    """Render a display model to a deterministic, content-addressed BMP."""
    image = Image.new("1", (WIDTH, HEIGHT), _PAPER)
    draw = ImageDraw.Draw(image)
    title_font = _font(24)
    small_font = _font(12)

    today = model.days[0].date if model.days else model.generated_at.date()
    _text(draw, (_LEFT, 9), "LECTIO  /  THREE DAY SCHEDULE", small_font)
    header = f"{today:%A %d %B}".upper()
    _text(draw, (_LEFT, 29), _fit(draw, header, title_font, 555), title_font)
    if show_status:
        acknowledged = _format_acknowledged_at(acknowledged_at)
        change_label = f"{change_count} CHANGE{'S' if change_count != 1 else ''}"
        _text(draw, (641, 13), change_label, _font(17))
        _text(draw, (641, 39), f"REVIEWED {acknowledged}", small_font)
    draw.line((_LEFT, 70, WIDTH - _LEFT, 70), fill=_INK, width=2)
    draw.line((_MAIN_RIGHT, 83, _MAIN_RIGHT, _BOTTOM), fill=_INK, width=1)

    _draw_schedule(draw, model, small_font)
    _draw_sidebar(
        draw,
        model.sidebar,
        small_font,
        data_stale=data_stale,
    )

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


def render_display_model(
    model: DisplayModel,
    *,
    change_count: int = 0,
    acknowledged_at: str | None = None,
    show_status: bool = False,
    data_stale: bool = False,
) -> bytes:
    """Compatibility wrapper for the existing device API integration."""
    return render_display(
        model,
        change_count=change_count,
        acknowledged_at=acknowledged_at,
        show_status=show_status,
        data_stale=data_stale,
    ).bmp


def _format_acknowledged_at(value: str | None) -> str:
    if value is None:
        return "--:--"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return "--:--"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return "--:--"
    return parsed.astimezone(_DISPLAY_TIMEZONE).strftime("%H:%M")


def mark_display_stale(bmp: bytes) -> bytes:
    """Overlay a safe stale marker while preserving the last schedule pixels."""
    with Image.open(BytesIO(bmp)) as existing:
        image = existing.convert("1")
    draw = ImageDraw.Draw(image)
    x = _SIDEBAR_LEFT
    draw.rectangle((x, 84, WIDTH - _LEFT, 110), fill=_PAPER)
    _text(draw, (x, 86), "DATA STALE", _font(17))
    output = BytesIO()
    image.save(output, format="BMP")
    image.close()
    return output.getvalue()


def _draw_schedule(draw, model, small_font) -> None:
    days = model.days[:3]
    if not days:
        _text(draw, (_LEFT, 96), "No schedule data", _font(18))
        return

    time_right = 94
    columns = (94, 270, 446, _MAIN_RIGHT)
    grid_top = 120
    row_edges = tuple(round(grid_top + index * (_BOTTOM - grid_top) / 7) for index in range(8))

    _text(draw, (_LEFT + 2, 91), "TIME", small_font)
    for index, day in enumerate(days):
        _text(
            draw,
            (columns[index] + 6, 87),
            _fit(draw, f"{day.date:%a %d %b}".upper(), _font(15), 164),
            _font(15),
        )
    draw.line((_LEFT, grid_top, _MAIN_RIGHT, grid_top), fill=_INK, width=1)
    for x in (time_right, columns[1], columns[2]):
        draw.line((x, 84, x, _BOTTOM), fill=_INK, width=1)

    timed = sorted(
        (
            (
                event.start.astimezone(_DISPLAY_TIMEZONE).hour * 60
                + event.start.astimezone(_DISPLAY_TIMEZONE).minute,
                event.end.astimezone(_DISPLAY_TIMEZONE).hour * 60
                + event.end.astimezone(_DISPLAY_TIMEZONE).minute,
            )
            for day in days
            for event in day.events
            if not event.all_day
            and isinstance(event.start, datetime)
            and isinstance(event.end, datetime)
        )
    )
    slots: list[tuple[int | None, int | None]] = []
    if any(event.all_day for day in days for event in day.events):
        slots.append((None, None))
    for start, end in timed:
        if slots and slots[-1][0] is not None and start - slots[-1][0] <= 25:
            previous_start, previous_end = slots[-1]
            slots[-1] = (previous_start, max(previous_end, end))
        else:
            slots.append((start, end))
    visible_limit = 7 if len(slots) <= 7 else 6
    visible_slots = slots[:visible_limit]

    cells: dict[tuple[int, int], list[DisplayEvent]] = {}
    hidden = [0] * len(days)
    for day_index, day in enumerate(days):
        for event in day.events:
            if event.all_day:
                slot_index = 0
            elif isinstance(event.start, datetime):
                local_start = event.start.astimezone(_DISPLAY_TIMEZONE)
                minute = local_start.hour * 60 + local_start.minute
                slot_index = min(
                    (index for index, slot in enumerate(slots) if slot[0] is not None),
                    key=lambda index: abs(minute - slots[index][0]),
                )
            else:
                hidden[day_index] += 1
                continue
            if slot_index >= visible_limit:
                hidden[day_index] += 1
            else:
                cells.setdefault((slot_index, day_index), []).append(event)

    for row in range(7):
        y0, y1 = row_edges[row], row_edges[row + 1]
        if row:
            draw.line((_LEFT, y0, _MAIN_RIGHT, y0), fill=_INK, width=1)
        row_label = "MORE" if len(slots) > 7 and row == 6 else f"{row + 1}. modul"
        _text(draw, (_LEFT + 2, y0 + 4), row_label, _font(11))
        if row < len(visible_slots):
            start, end = visible_slots[row]
            label = "ALL DAY" if start is None else f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"
            _text(draw, (_LEFT + 2, y0 + 22), label, _font(10))
        for day_index in range(len(days)):
            events = cells.get((row, day_index), ())
            if not events:
                continue
            x = columns[day_index] + 3
            cell_right = columns[day_index + 1] - 4
            draw.rectangle((x, y0 + 3, x + 3, y1 - 4), fill=_INK)
            text_x = x + 8
            available = cell_right - text_x
            event = events[0]
            _text(draw, (text_x, y0 + 4), _fit(draw, event.title, _font(14), available), _font(14))
            detail = " · ".join(
                value
                for value in (_abbreviate_detail(event.teacher), _abbreviate_detail(event.room))
                if value
            )
            if len(events) > 1:
                detail = f"{detail} +{len(events) - 1}" if detail else f"+{len(events) - 1} more"
            if detail:
                _text(draw, (text_x, y0 + 23), _fit(draw, detail, _font(11), available), _font(11))

    for day_index, count in enumerate(hidden):
        if count:
            x = columns[day_index] + 10
            y = row_edges[-2] + 15
            _text(draw, (x, y), f"+{count} later", _font(14))


def _draw_sidebar(
    draw,
    items: tuple[SidebarItem, ...],
    small_font,
    *,
    data_stale: bool = False,
) -> None:
    x = _SIDEBAR_LEFT
    width = WIDTH - x - _LEFT
    _text(draw, (x, 86), "DATA STALE" if data_stale else "UP NEXT", _font(17))
    draw.line((x, 112, WIDTH - _LEFT, 112), fill=_INK, width=1)
    y = 123
    groups = (
        ("CANCELLATIONS", "cancellation"),
        ("ASSIGNMENTS", "assignment"),
        ("HOMEWORK", "homework"),
    )
    item_count = len(items)
    visible_count = 0
    footer_y = _BOTTOM - 17
    overflow = False
    if not items:
        _text(draw, (x + 2, y + 2), "Nothing due", _font(15))
        return
    for label, kind in groups:
        group = [item for item in items if item.kind == kind]
        if not group:
            continue

        first_secondary = group[0].subtitle or _display_date(group[0].when)
        first_height = 22 + (16 if first_secondary else 0) + 7
        reserve_footer = 17 if item_count - visible_count > 1 else 0
        if y + 19 + first_height + reserve_footer > _BOTTOM:
            overflow = True
            break
        _text(draw, (x, y), label, small_font)
        y += 19
        for item in group:
            secondary = item.subtitle or _display_date(item.when)
            item_height = 22 + (16 if secondary else 0) + 7
            remaining = item_count - visible_count - 1
            reserve_footer = 17 if remaining else 0
            if y + item_height + reserve_footer > _BOTTOM:
                overflow = True
                break
            _text(draw, (x + 2, y), _fit(draw, item.title, _font(15), width - 4), _font(15))
            y += 22
            if secondary:
                _text(draw, (x + 2, y), _fit(draw, secondary, small_font, width - 4), small_font)
                y += 16
            y += 7
            visible_count += 1
        if overflow:
            break

    if overflow:
        omitted = item_count - visible_count
        _text(draw, (x + 2, footer_y), f"+{omitted} more", small_font)


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
