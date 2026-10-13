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

    def test_close_publish_requires_today_close_and_coverage(self):
        now = datetime(2026, 10, 13, 17, 23, tzinfo=ZoneInfo("Asia/Shanghai"))
        good = {"quote_at": "2026-10-13 15:00", "quote_coverage": 0.97}
        self.assertEqual(validate_quote_freshness(good, "15:00", now), [])
        self.assertTrue(validate_quote_freshness({**good, "quote_at": "2026-10-12 15:00"},
                                                 "15:00", now))
        self.assertTrue(validate_quote_freshness({**good, "quote_at": "2026-10-13 14:59"},
                                                 "15:00", now))
        self.assertTrue(validate_quote_freshness({**good, "quote_coverage": 0.89},
                                                 "15:00", now))
        self.assertTrue(validate_quote_freshness({"quote_at": "", "quote_coverage": 1.0},
                                                 "15:00", now))


if __name__ == "__main__":
    unittest.main()
