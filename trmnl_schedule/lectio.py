import re
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .events import Event

WEEKDAYS = {
    "mandag": 0, "tirsdag": 1, "onsdag": 2, "torsdag": 3,
    "fredag": 4, "lørdag": 5, "lordag": 5, "søndag": 6, "sondag": 6,
}
TIME_RANGE = re.compile(r"(?<!\d)(\d{1,2}:\d{2})\s*(?:-|til)\s*(\d{1,2}:\d{2})(?!\d)", re.IGNORECASE)


def _lesson_date(label, index, iso_year, iso_week):
    name = label.split("(", 1)[0].strip().casefold()
    offset = WEEKDAYS.get(name, index)
    return date.fromisocalendar(iso_year, iso_week, offset + 1)


def parse_schedule_html(html, iso_year, iso_week, timezone_name="Europe/Copenhagen"):
    soup = BeautifulSoup(html, "html.parser")
    header = soup.find("tr", class_="s2dayHeader")
    if header is None:
        raise ValueError("Lectio schedule page has no weekday header")
    cells = header.find_all("td")
    columns = soup.find_all("div", class_="s2skemabrikcontainer")
    if len(columns) == len(cells):
        aligned = cells
    elif len(columns) + 1 == len(cells):
        aligned = cells[1:]
    elif len(columns) > len(cells):
        columns, aligned = columns[-len(cells):], cells
    else:
        aligned = cells[-len(columns):] if columns else []
    timezone = ZoneInfo(timezone_name)
    events = []
    day_index = 0
    for cell, column in zip(aligned, columns):
        label = cell.get_text(" ", strip=True)
        if not label:
            continue
        event_day = _lesson_date(label, day_index, iso_year, iso_week)
        day_index += 1
        for card in column.find_all("a", class_="s2skemabrik"):
            classes = set(card.get("class", []))
            tooltip = card.get("data-tooltip", "")
            if "s2cancelled" in classes or "aflyst" in tooltip.casefold():
                continue
            lines = [line.strip() for line in tooltip.replace("\r", "").splitlines() if line.strip()]
            match = TIME_RANGE.search(tooltip)
            if not lines or match is None:
                continue
            values = {}
            for line in lines:
                if ":" in line:
                    key, value = line.split(":", 1)
                    values[key.strip().casefold()] = value.strip()
            title = values.get("navn") or next(
                (line for line in lines if not TIME_RANGE.search(line) and ":" not in line),
                "Untitled lesson",
            )
            start_time, end_time = time.fromisoformat(match.group(1)), time.fromisoformat(match.group(2))
            start = datetime.combine(event_day, start_time, tzinfo=timezone)
            end = datetime.combine(event_day, end_time, tzinfo=timezone)
            if end <= start:
                end += timedelta(days=1)
            details = " · ".join(part for part in (values.get("hold"), values.get("lærer")) if part)
            uid_match = re.search(r"absid=(\d+)", card.get("href", ""))
            events.append(Event(
                title=title, starts_at=start, ends_at=end, source="Lectio",
                location=values.get("lokale", ""), details=details,
                uid=uid_match.group(1) if uid_match else "",
            ))
    return sorted(events, key=lambda event: event.starts_at)


def fetch_week(username, password, school_id, iso_year, iso_week,
               timezone_name="Europe/Copenhagen", client_factory=None):
    if not all((username, password, school_id)):
        raise ValueError("Lectio username, password, and school ID are all required")
    if not str(school_id).isdigit():
        raise ValueError("Lectio school ID must be numeric")
    if client_factory is None:
        try:
            import lectio
        except ImportError as error:
            raise RuntimeError("Install python-lectio to fetch Lectio schedules") from error
        client_factory = lectio.sdk
    client = client_factory(brugernavn=username, adgangskode=password, skoleId=school_id)
    query = urlencode({"type": "elev", "elevid": client.elevId, "week": f"{iso_week:02d}{iso_year}"})
    url = f"https://www.lectio.dk/lectio/{school_id}/SkemaNy.aspx?{query}"
    response = client.session.get(url, timeout=(5, 25))
    response.raise_for_status()
    if "login.aspx" in response.url.casefold():
        raise RuntimeError("Lectio redirected to login; check credentials and school ID")
    return parse_schedule_html(response.text, iso_year, iso_week, timezone_name)
