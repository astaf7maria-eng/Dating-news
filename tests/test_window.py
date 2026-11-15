"""Boundary checks for suggested and custom editorial windows."""

import unittest
from datetime import date, datetime, timedelta

from dating_digest.domain.window import date_window, in_window, rolling_window, validate_window


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.end = datetime.fromisoformat("2026-11-15T12:00:00+03:00")

    def test_exact_window_and_boundaries(self):
        start, end = rolling_window(self.end)
        self.assertEqual(start.isoformat(), "2026-11-08T12:00:00+03:00")
        self.assertEqual(end - start, timedelta(days=7))
        self.assertTrue(in_window(start, start, end))
        self.assertFalse(in_window(start - timedelta(microseconds=1), start, end))
        self.assertFalse(in_window(end, start, end))

    def test_equivalent_timestamp_in_another_timezone(self):
        instant = datetime.fromisoformat("2026-11-08T09:00:00+00:00")
        start, end = rolling_window(self.end)
        self.assertTrue(in_window(instant, start, end))
        self.assertEqual(rolling_window(self.end), rolling_window(
            datetime.fromisoformat("2026-11-15T09:00:00+00:00")
        ))

    def test_unknown_timezone_is_rejected(self):
        naive = datetime(2026, 11, 15, 12)
        with self.assertRaises(ValueError):
            rolling_window(naive)
        with self.assertRaises(ValueError):
            in_window(naive, *rolling_window(self.end))

    def test_custom_dates_include_last_day_and_can_exceed_week(self):
        start, end = date_window(date(2026, 10, 30), date(2026, 11, 12))
        self.assertEqual(end - start, timedelta(days=14))
        self.assertTrue(in_window(datetime.fromisoformat("2026-11-12T23:59:59+03:00"), start, end))
        self.assertFalse(in_window(datetime.fromisoformat("2026-11-13T00:00:00+03:00"), start, end))

    def test_same_day_is_valid_but_reversed_dates_are_not(self):
        start, end = date_window(date(2026, 11, 1), date(2026, 11, 1))
        self.assertEqual(end - start, timedelta(days=1))
        with self.assertRaises(ValueError):
            date_window(date(2026, 11, 2), date(2026, 11, 1))
        with self.assertRaises(ValueError):
            validate_window(end, start)


if __name__ == "__main__":
    unittest.main()
