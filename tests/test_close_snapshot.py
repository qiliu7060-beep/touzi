"""固定收盘资产与完整买入条件，独立于盘中报价时效。"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import subprocess
import unittest

from tests.test_buy_decision import stock, by_key
from touzi.close_snapshot import (close_target, export_snapshot, public_entry,
                                  validate_close_time, write_snapshot, json_bytes)
from touzi.config import load_config
from touzi.market_calendar import TZ
from touzi.plans import decide
from touzi.data.financial import analyze_financials
from tests.financial_fixtures import statement_tables

QUOTE = "2026-10-23T16:15:00+08:00"
BUILT = "2026-10-23T17:23:00+08:00"


class CloseSnapshotTests(unittest.TestCase):
    def test_actual_pandas_dividend_rows_and_array_context_keep_shape(self):
        import pandas as pd
        import numpy as np
        context = {"div_summary": {"000001": pd.Series({"code": "000001", "dps": np.float64(1.2), "yield": np.nan})},
                   "array": np.array([1., 2., np.nan]), "missing": pd.NA, "date_missing": pd.NaT}
        decoded = json.loads(json_bytes(context))
        self.assertEqual(decoded["div_summary"]["000001"], {"code": "000001", "dps": 1.2, "yield": None})
        self.assertEqual(decoded["array"], [1., 2., None])
        self.assertIsNone(decoded["missing"])
        self.assertIsNone(decoded["date_missing"])

    def sample(self, code="600519"):
        score = stock()
        score.code, score.industry = code, "制造业"
        score.parts["quality"].detail["financial_analysis"] = analyze_financials(
            statement_tables(capex=20., liabilities=60., fetched="2026-10-23T17:00:00+08:00"),
            industry="制造业", as_of=datetime.fromisoformat(BUILT))
        return score

    def test_two_financial_gates_are_strict_and_old_gates_remain(self):
        for key, equal, low in (("debt_equity", .8, .799999), ("capex_to_profit", .25, .249999)):
            sample = self.sample()
            metric = sample.parts["quality"].detail["financial_analysis"]["metrics"][key]
            metric["value"] = equal
            self.assertFalse(by_key(sample)[key].hit)
            self.assertNotEqual(decide(sample, load_config(), macro_score=50).action, "建仓")
            metric["value"] = low
            self.assertTrue(by_key(sample)[key].hit)
        sample = self.sample()
        sample.dividend_yield = 1.5
        self.assertFalse(by_key(sample)["dividend_yield"].hit)
        self.assertNotEqual(decide(sample, load_config(), macro_score=50).action, "建仓")

    def test_unknown_stale_invalid_not_pass_but_verified_exemption_skips(self):
        sample = self.sample()
        metric = sample.parts["quality"].detail["financial_analysis"]["metrics"]["debt_equity"]
        for state in ("missing", "stale", "invalid", "not_applicable"):
            metric["status"] = state
            self.assertFalse(by_key(sample)["debt_equity"].hit)
        sample.industry = "银行"
        check = by_key(sample)["debt_equity"]
        self.assertTrue(check.hit)
        self.assertIn("不表示比例达标", check.note)
        metric["status"], metric["value"] = "ok", "bad"
        self.assertFalse(by_key(sample)["debt_equity"].known)

    def test_latest_close_remains_next_trading_morning_and_long_holiday(self):
        self.assertEqual(str(close_target(datetime(2026, 10, 26, 10, tzinfo=TZ))), "2026-10-23")
        self.assertEqual(str(close_target(datetime(2026, 10, 26, 15, tzinfo=TZ))), "2026-10-26")
        self.assertEqual(str(close_target(datetime(2026, 10, 5, 12, tzinfo=TZ))), "2026-09-30")
        self.assertIsNone(close_target(datetime(2027, 1, 1, tzinfo=TZ)))

    def test_intraday_and_false_trading_day_rejected(self):
        for day, quote in (("2026-10-23", "2026-10-23T14:59:00+08:00"),
                           ("2026-10-24", "2026-10-24T16:15:00+08:00")):
            with self.assertRaises(ValueError):
                validate_close_time(day, quote, BUILT)

    def test_all_701_entries_and_hashes_not_top40_or_old600_limit(self):
        cfg = load_config()
        entries = {f"{i:06d}": public_entry(self.sample(f"{i:06d}"), cfg, 50, QUOTE) for i in range(1, 702)}
        counts = dict(market_total=800, eligible_total=705, attempted=705, scored=701, failed=4, excluded=95)
        manifest, files = export_snapshot(entries, cfg, "2026-10-23", QUOTE, counts, BUILT)
        self.assertEqual(len(manifest["index"]), 701)
        self.assertEqual(len(manifest["shards"]), 8)
        for shard in manifest["shards"]:
            self.assertEqual(hashlib.sha256(files[shard["path"]]).hexdigest(), shard["sha256"])
            self.assertLessEqual(len(shard["codes"]), 100)
        first = entries["000001"]
        self.assertEqual(first["action"], "建仓")
        self.assertEqual(len(first["buy_checks"]), 10)
        self.assertFalse(any(k in first for k in ("holding", "owner", "shares", "cost")))
        with tempfile.TemporaryDirectory() as folder:
            write_snapshot(Path(folder), files)
            self.assertEqual(json.loads((Path(folder) / "close-scores.json").read_text(encoding="utf-8"))["universe"], counts)

    def test_counts_and_null_scores_reject_false_full_coverage(self):
        entry = public_entry(self.sample(), load_config(), 50, QUOTE)
        counts = dict(market_total=1, eligible_total=1, attempted=1, scored=1, failed=0, excluded=0)
        for corrupt in (dict(counts, market_total=10), dict(counts, attempted=0)):
            with self.assertRaises(ValueError):
                export_snapshot({entry["code"]: entry}, load_config(), "2026-10-23", QUOTE, corrupt, BUILT)
        entry["total"] = None
        with self.assertRaises(ValueError):
            export_snapshot({entry["code"]: entry}, load_config(), "2026-10-23", QUOTE, counts, BUILT)

    def test_real_python_export_passes_worker_consumer_before_publication(self):
        cfg = load_config()
        entries = {f"{i:06d}": public_entry(self.sample(f"{i:06d}"), cfg, 50, QUOTE) for i in range(1, 704)}
        counts = dict(market_total=750, eligible_total=705, attempted=705, scored=703, failed=2, excluded=45)
        manifest, files = export_snapshot(entries, cfg, "2026-10-23", QUOTE, counts, BUILT)
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            write_snapshot(Path(folder), files)
            command = ["node", str(root / "scripts/check_close_snapshot.mjs"), folder]
            result = subprocess.run(command, cwd=root, capture_output=True, encoding="utf-8", check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('"scored":703', result.stdout)
            # 保持SHA匹配仍必须拒绝稀疏/不可信的实际财报对象。
            corrupt = deepcopy(entries)
            corrupt["000001"]["financial_analysis"] = {"metrics": {}}
            _, bad_files = export_snapshot(corrupt, cfg, "2026-10-23", QUOTE, counts, BUILT)
            write_snapshot(Path(folder), bad_files)
            result = subprocess.run(command, cwd=root, capture_output=True, encoding="utf-8", check=False)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
