"""休市跳过仅控制重算/部署，不豁免回归测试或未知年校验。"""
from datetime import datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_eligibility import publish_eligibility


class PublishEligibilityTests(unittest.TestCase):
    def test_known_trading_and_closed_days(self):
        for day, expected in [('2026-10-25', False), ('2026-10-26', True), ('2026-10-07', False), ('2026-10-08', True), ('2026-10-10', False)]:
            with self.subTest(day=day):
                self.assertEqual(publish_eligibility(datetime.fromisoformat(day+'T20:00:00').replace(tzinfo=ZoneInfo('Asia/Shanghai'))), expected)

    def test_unknown_year_fails_closed(self):
        with self.assertRaises(ValueError):
            publish_eligibility(datetime(2027,1,1,tzinfo=ZoneInfo('Asia/Shanghai')))

    def test_utc_uses_shanghai_date(self):
        self.assertFalse(publish_eligibility(datetime.fromisoformat('2026-10-25T17:00:00+00:00')))
        self.assertTrue(publish_eligibility(datetime.fromisoformat('2026-10-26T01:30:00+00:00')))

    def test_monday_preopen_skips_old_friday_but_open_resumes(self):
        for time, expected in [('00:55:00',False),('09:29:59',False),('09:30:00',True)]:
            with self.subTest(time=time):
                self.assertEqual(publish_eligibility(datetime.fromisoformat('2026-10-26T'+time+'+08:00')),expected)

    def test_normal_next_day_preopen_still_allows_valid_close(self):
        self.assertTrue(publish_eligibility(datetime.fromisoformat('2026-10-27T01:00:00+08:00')))
        self.assertTrue(publish_eligibility(datetime.fromisoformat('2026-10-27T09:29:59+08:00')))

    def test_long_holiday_preopen_uses_exchange_calendar(self):
        self.assertFalse(publish_eligibility(datetime.fromisoformat('2026-10-08T09:29:59+08:00')))
        self.assertTrue(publish_eligibility(datetime.fromisoformat('2026-10-08T09:30:00+08:00')))

    def test_preopen_unknown_previous_year_fails_closed(self):
        with self.assertRaises(ValueError):
            publish_eligibility(datetime.fromisoformat('2026-01-05T01:00:00+08:00'))


if __name__ == '__main__':
    unittest.main()
