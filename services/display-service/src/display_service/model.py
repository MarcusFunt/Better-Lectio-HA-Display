"""Typed, renderer-independent display data structures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

DisplayTime = date | datetime
SidebarKind = Literal["cancellation", "assignment", "homework"]
EventSource = Literal["lectio", "private"]


@dataclass(frozen=True, slots=True)
class DisplayEvent:
    """One calendar event normalized for the display timeline."""

    id: str
    start: DisplayTime
    end: DisplayTime
    title: str
    source: EventSource
    all_day: bool
    teacher: str | None = None
    room: str | None = None


@dataclass(frozen=True, slots=True)
class DisplayDay:
    """A local calendar day and its chronologically ordered events."""

    date: date
    events: tuple[DisplayEvent, ...]


@dataclass(frozen=True, slots=True)
class SidebarItem:
    """One prioritized cancellation, assignment, or homework item."""

    id: str
    kind: SidebarKind
    priority: int
    title: str
    subtitle: str | None
    when: DisplayTime | None
    source: str


@dataclass(frozen=True, slots=True)
class DisplayModel:
    """Complete deterministic input for a future renderer."""

    generated_at: datetime
    days: tuple[DisplayDay, ...]
    sidebar: tuple[SidebarItem, ...]
