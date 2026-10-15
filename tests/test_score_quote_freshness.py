"""定时评分只发布行情源证实的交易日数据。"""

from datetime import datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from publish_site import validate_quote_freshness
from touzi.screener import _merge_live_quotes


class ScoreQuoteFreshnessTests(unittest.TestCase):
    def test_only_quotes_from_latest_common_trading_day_are_scored(self):
        candidates = pd.DataFrame({
            "code": ["000001", "000002", "000003"],
            "price": [9.0, 9.0, 9.0],
            "pe_ttm": [8.0, 8.0, 8.0],
        })
        quotes = pd.DataFrame({
            "code": ["000001", "000002", "000003"],
            "price": [10.0, 11.0, 12.0],
            "pe_ttm": [10.0, 11.0, 12.0],
            "quote_ts": ["2026-10-13 15:00:01", "2026-10-13 15:00:02",
                         "2026-10-12 15:00:01"],
        })
        updated, quote_at, coverage = _merge_live_quotes(candidates, quotes)
        self.assertEqual(updated["code"].tolist(), ["000001", "000002"])
        self.assertEqual(updated["price"].tolist(), [10.0, 11.0])
        self.assertEqual(updated["pe_ttm"].tolist(), [10.0, 11.0])
        self.assertEqual(quote_at.strftime("%Y-%m-%d %H:%M"), "2026-10-13 15:00")
        self.assertAlmostEqual(coverage, 2 / 3)

    def test_same_day_close_requires_minimum_time_and_coverage(self):
        now = datetime(2026, 10, 13, 17, 23, tzinfo=ZoneInfo("Asia/Shanghai"))
        good = {"quote_at": "2026-10-13 15:00", "quote_coverage": 0.97}
        self.assertEqual(validate_quote_freshness(good, "15:00", now), [])
        self.assertTrue(validate_quote_freshness({**good, "quote_at": "2026-10-13 14:59"},
                                                 "15:00", now))
        self.assertTrue(validate_quote_freshness({**good, "quote_coverage": 0.89},
                                                 "15:00", now))

    def test_delayed_close_publish_allows_previous_day_before_market_open(self):
        now = datetime(2026, 10, 14, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        flags = {"quote_at": "2026-10-13 16:14", "quote_coverage": 1.0}
        self.assertEqual(validate_quote_freshness(flags, "15:00", now), [])
        self.assertEqual(validate_quote_freshness(flags, "09:30", now), [])

    def test_previous_day_exception_rejects_intraday_or_expired_quotes(self):
        before_open = datetime(2026, 10, 14, 9, 29,
                               tzinfo=ZoneInfo("Asia/Shanghai"))
        at_open = datetime(2026, 10, 14, 9, 30,
                           tzinfo=ZoneInfo("Asia/Shanghai"))
        good = {"quote_at": "2026-10-13 15:00", "quote_coverage": 1.0}
        self.assertEqual(validate_quote_freshness(good, "15:00", before_open), [])
        self.assertTrue(validate_quote_freshness(
            {**good, "quote_at": "2026-10-13 14:59"}, "09:30", before_open))
        self.assertTrue(validate_quote_freshness(good, "15:00", at_open))
        self.assertTrue(validate_quote_freshness(
            {**good, "quote_at": "2026-10-12 15:00"}, "15:00", before_open))

    def test_future_or_invalid_quote_metadata_is_rejected(self):
        now = datetime(2026, 10, 13, 17, 23, tzinfo=ZoneInfo("Asia/Shanghai"))
        good = {"quote_at": "2026-10-13 15:00", "quote_coverage": 1.0}
        self.assertEqual(validate_quote_freshness(
            {**good, "quote_at": "2026-10-13 17:28"}, "15:00", now), [])
        self.assertTrue(validate_quote_freshness(
            {**good, "quote_at": "2026-10-13 17:29"}, "15:00", now))
        self.assertTrue(validate_quote_freshness({"quote_at": "", "quote_coverage": 1.0},
                                                 "15:00", now))
        self.assertTrue(validate_quote_freshness(
            {"quote_at": "not-a-time", "quote_coverage": 1.0}, "15:00", now))


if __name__ == "__main__":
    unittest.main()

