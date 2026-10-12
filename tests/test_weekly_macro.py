"""宏观周快照与个股日更的边界测试。"""

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pandas as pd

from run_all import _weekly_macro
from touzi.regime import MacroContext
from touzi.signals.macro import RuleResult
from touzi.util import Cache


class WeeklyMacroTests(unittest.TestCase):
    def test_reuses_same_week_and_recomputes_next_week(self):
        with TemporaryDirectory() as temp:
            cache = Cache(temp)
            calls = []

            def build():
                calls.append(1)
                return MacroContext(
                    as_of=pd.Timestamp("2026-10-12"),
                    results={"pmi": RuleResult("PMI", 62.5, "平稳", explain="已核验")},
                    weights={"pmi": 1.0}, macro_score=62.5,
                    position=0.6, regime="偏积极", staleness={},
                )

            tz = ZoneInfo("Asia/Shanghai")
            monday = datetime(2026, 10, 12, 17, 23, tzinfo=tz)
            tuesday = datetime(2026, 10, 13, 17, 23, tzinfo=tz)
            next_monday = datetime(2026, 10, 19, 17, 23, tzinfo=tz)
            first = _weekly_macro(cache, build, monday)
            second = _weekly_macro(cache, build, tuesday)
            self.assertEqual(len(calls), 1)
            self.assertEqual(second.macro_score, first.macro_score)
            self.assertEqual(second.results["pmi"].explain, "已核验")
            _weekly_macro(cache, build, next_monday)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

