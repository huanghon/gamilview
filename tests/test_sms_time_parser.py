from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from sms.time_parser import parse_kr_received_time


class SmsTimeParserTests(unittest.TestCase):
    def test_pm_afternoon(self) -> None:
        now = datetime(2026, 7, 14, 20, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        result = parse_kr_received_time("07. 14. 오후 3:37", now=now)
        self.assertEqual(result.isoformat(), "2026-07-14T15:37:00+09:00")

    def test_am_morning(self) -> None:
        now = datetime(2026, 7, 14, 20, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        result = parse_kr_received_time("07. 14. 오전 9:05", now=now)
        self.assertEqual(result.isoformat(), "2026-07-14T09:05:00+09:00")

    def test_noon_and_midnight(self) -> None:
        now = datetime(2026, 7, 14, 20, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        noon = parse_kr_received_time("07. 14. 오후 12:05", now=now)
        midnight = parse_kr_received_time("07. 14. 오전 12:05", now=now)
        self.assertEqual(noon.hour, 12)
        self.assertEqual(midnight.hour, 0)

    def test_cross_year_rollback(self) -> None:
        # Viewing Dec data in early January should roll year back.
        now = datetime(2027, 1, 2, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        result = parse_kr_received_time("12. 30. 오후 3:00", now=now)
        self.assertEqual(result.year, 2026)
        self.assertEqual(result.month, 12)
        self.assertEqual(result.day, 30)

    def test_invalid_format(self) -> None:
        with self.assertRaises(ValueError):
            parse_kr_received_time("not-a-time")


if __name__ == "__main__":
    unittest.main()
