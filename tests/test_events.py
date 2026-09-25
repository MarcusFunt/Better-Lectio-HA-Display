import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trmnl_schedule.events import parse_ics
from trmnl_schedule.lectio import fetch_week, parse_schedule_html

TZ = ZoneInfo("Europe/Copenhagen")


class EventTests(unittest.TestCase):
    def test_all_day_recurrence_date_until_and_duration(self):
        ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:days
DTSTART;VALUE=DATE:20260923
DURATION:P3D
RRULE:FREQ=DAILY;UNTIL=20260925
SUMMARY:Trip
END:VEVENT
END:VCALENDAR
"""
        events = parse_ics(ics, window_start=datetime(2026, 9, 24, tzinfo=TZ),
                           window_end=datetime(2026, 9, 29, tzinfo=TZ))
        self.assertEqual([e.starts_at.date() for e in events], [date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)])
        self.assertEqual(events[0].ends_at.date(), date(2026, 9, 26))

    def test_multiple_exdates_and_moved_cancelled_instances(self):
        ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series
DTSTART:20260924T100000Z
DTEND:20260924T103000Z
RRULE:FREQ=DAILY;COUNT=4
EXDATE:20260926T100000Z
EXDATE:20260927T100000Z
SUMMARY:Class
END:VEVENT
BEGIN:VEVENT
UID:series
RECURRENCE-ID:20260925T100000Z
DTSTART:20260925T130000Z
DTEND:20260925T133000Z
SUMMARY:Moved class
END:VEVENT
BEGIN:VEVENT
UID:series
RECURRENCE-ID:20260926T100000Z
DTSTART:20260926T100000Z
DTEND:20260926T103000Z
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""
        events = parse_ics(ics, window_start=datetime(2026, 9, 24, tzinfo=TZ),
                           window_end=datetime(2026, 9, 29, tzinfo=TZ))
        self.assertEqual([e.title for e in events], ["Class", "Moved class"])
        self.assertEqual(events[1].starts_at, datetime(2026, 9, 25, 15, tzinfo=TZ))

    def test_utc_and_floating_recurrences_keep_source_time_through_dst(self):
        utc_series = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:utc
DTSTART:20261024T220000Z
DTEND:20261024T223000Z
RRULE:FREQ=DAILY;COUNT=4
SUMMARY:UTC
END:VEVENT
END:VCALENDAR
"""
        utc_events = parse_ics(utc_series, window_start=datetime(2026, 10, 24, tzinfo=TZ),
                               window_end=datetime(2026, 10, 29, tzinfo=TZ))
        self.assertEqual(utc_events[1].starts_at, datetime(2026, 10, 25, 23, tzinfo=TZ))
        floating = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:float
DTSTART:20260924T090000
DTEND:20260924T093000
RRULE:FREQ=DAILY;UNTIL=20260927T090000
SUMMARY:Local
END:VEVENT
END:VCALENDAR
"""
        local_events = parse_ics(floating, window_start=datetime(2026, 9, 24, tzinfo=TZ),
                                 window_end=datetime(2026, 9, 28, tzinfo=TZ))
        self.assertEqual(len(local_events), 4)
        self.assertEqual(local_events[-1].starts_at.hour, 9)

    def test_lectio_schedule_is_grouped_by_weekday(self):
        html = '''<table><tr class="s2dayHeader"><td></td><td>Mandag (21/9)</td></tr></table>
<div class="s2skemabrikcontainer"></div><div class="s2skemabrikcontainer">
<a class="s2skemabrik s2normal" href="?absid=123" data-tooltip="Matematik\nTidspunkt: 08:15 - 09:15\nLokale: A101"></a>
</div>'''
        events = parse_schedule_html(html, 2026, 39)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].starts_at, datetime(2026, 9, 21, 8, 15, tzinfo=TZ))

    def test_lectio_uses_sdk_credentials_and_authenticated_week_url(self):
        requests = []

        class Response:
            url = "https://www.lectio.dk/lectio/42/SkemaNy.aspx?week=392026"
            text = '<tr class="s2dayHeader"><td></td><td>Mandag (21/9)</td></tr><div class="s2skemabrikcontainer"></div>'

            def raise_for_status(self):
                return None

        class Session:
            def get(self, url, timeout):
                requests.append((url, timeout))
                return Response()

        class Client:
            elevId = "123"
            session = Session()

        created = []

        def factory(**kwargs):
            created.append(kwargs)
            return Client()

        events = fetch_week("student", "pw", "42", 2026, 39, client_factory=factory)
        self.assertEqual(events, [])
        self.assertEqual(created, [{"brugernavn": "student", "adgangskode": "pw", "skoleId": "42"}])
        self.assertIn("week=392026", requests[0][0])
        self.assertEqual(requests[0][1], (5, 25))


if __name__ == "__main__":
    unittest.main()
