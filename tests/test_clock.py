import unittest
from datetime import date, datetime, timezone

from src import clock

T = clock.parse_now
MINUS = "−"


class ClockTest(unittest.TestCase):
    def test_parse_now_is_store_local(self):
        t = T("2026-10-31T10:00")
        self.assertEqual(t.utcoffset().total_seconds(), -4 * 3600)
        self.assertEqual((t.hour, t.minute), (10, 0))

    def test_now_converts_aware_override_to_store_time(self):
        self.assertEqual(clock.now(datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)), T("2026-11-02T10:00"))

    def test_opening_hours(self):
        self.assertTrue(clock.is_open(T("2026-11-02T09:00")))   # Mon, opens 09:00
        self.assertFalse(clock.is_open(T("2026-11-02T18:00")))  # closes 18:00
        self.assertTrue(clock.is_open(T("2026-10-31T12:59")))   # Sat
        self.assertFalse(clock.is_open(T("2026-10-31T13:00")))
        self.assertFalse(clock.is_open(T("2026-11-01T11:00")))  # Sun

    def test_closing_time(self):
        self.assertEqual(clock.closing_time(T("2026-10-31T10:00")), T("2026-10-31T13:00"))
        self.assertIsNone(clock.closing_time(T("2026-11-01T10:00")))

    def test_next_opening(self):
        self.assertEqual(clock.next_opening(T("2026-10-31T13:30")), T("2026-11-02T09:00"))
        self.assertEqual(clock.next_opening(T("2026-11-02T07:00")), T("2026-11-02T09:00"))
        self.assertEqual(clock.next_opening(T("2026-11-02T19:00")), T("2026-11-03T09:00"))

    def test_reopening_text(self):
        self.assertEqual(clock.reopening_text(T("2026-10-31T14:00")), "seg 02/11 às 09:00")

    def test_time_line_open(self):
        self.assertEqual(
            clock.time_line(T("2026-11-02T09:15")),
            f"[agora: seg 02/11/2026 09:15, horário de Campo Grande (UTC{MINUS}4) — loja aberta | fecha às 18:00]",
        )

    def test_time_line_closed(self):
        self.assertEqual(
            clock.time_line(T("2026-11-01T20:00")),
            f"[agora: dom 01/11/2026 20:00, horário de Campo Grande (UTC{MINUS}4) — loja fechada | reabre seg 02/11 às 09:00]",
        )

    def test_fmt_date(self):
        self.assertEqual(clock.fmt_date(date(2025, 12, 22)), "22/12/2025")
