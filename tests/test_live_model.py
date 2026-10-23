"""无网络同输入合约：Python 评分入口与 Worker 镜像逐字段对照。"""
import copy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from touzi.config import load_config
from touzi.live_model import clean, export_model
from touzi.plans import evaluate_sell
from touzi.screener import Screener


def fixtures():
    cases = []
    for variant in ("history", "industry", "market", "no-reference", "invalid-history", "bonus", "no-bond", "no-dividend", "finance-penalty", "threshold-equality"):
        cfg = load_config()
        cfg["valuation"]["primary_bonus"] = .15 if variant == "bonus" else 0
        s = Screener.__new__(Screener)
        s.cfg, s.live, s.as_of = cfg, True, pd.Timestamp("2026-10-23")
        s._universe = pd.DataFrame({"code": [f"{i:06d}" for i in range(30)],
            "pe_ttm": [5 + i for i in range(30)], "pb": [.4 + i / 10 for i in range(30)],
            "industry": ["银行"] * (3 if variant == "market" else 30) +
                        (["其他"] * 27 if variant == "market" else [])})
        if variant == "no-reference":
            s._universe = s._universe.head(3)
        s._industry_policy = lambda: pd.DataFrame()
        s._policy_events = lambda: pd.DataFrame()
        s._bond_yield = lambda: float("nan") if variant == "no-bond" else 2.0
        s._div_summary = None
        if variant == "finance-penalty":
            s._div_summary = pd.DataFrame()
            s._div_summary_map = lambda: {"000001": {"dividend_times": 2, "finance_times": 3}}
        hist = pd.Series([5 + i % 20 for i in range(40)], index=pd.date_range(end="2026-10-22", periods=40))
        if variant in ("industry", "market", "no-reference"):
            hist = hist.head(10)
        if variant == "invalid-history":
            hist[:] = -1
            hist.iloc[-4:] = [5, 6, 7, 8]
        dividends = pd.DataFrame({"ex_date": pd.to_datetime([f"{year}-06-01" for year in range(2015, 2027)]),
                                  "dps": [.5] * 12})
        if variant == "no-dividend":
            dividends = dividends.iloc[:0]
        raw = {"code": "000001", "pe_hist": hist, "pb_hist": hist / 10, "fin": None, "div": dividends}
        meta = {"code": "000001", "name": "合约样例", "industry": "银行", "price": 10., "pe_ttm": 10., "pb": 1.}
        baseline = s.score_one(meta, raw)
        if variant == "threshold-equality":
            cfg["plan"]["sell_total"] = baseline.total
            cfg["plan"]["sell_reduce_total"] = baseline.total
            cfg["plan"]["sell_graham"] = 10.
            cfg["plan"]["sell_dividend_yield"] = 5.
            cfg["plan"]["sell_pe_abs"] = 10.
        result = {"stock_results": [baseline], "macro": SimpleNamespace(macro_score=45.),
                  "flags": {"as_of": "2026-10-23", "quote_at": "2026-10-23T10:00:00+08:00", "quote_coverage": 1.}}
        model = export_model(result, cfg, datetime(2026, 10, 23, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
        for price, pe, pb in [(10., 10., 1.), (5., -2., .5), (20., 50., 3.), (12.5, 18., 2.5), (11., None, None)]:
            current = {**meta, "price": price, "pe_ttm": float("nan") if pe is None else pe,
                       "pb": float("nan") if pb is None else pb}
            score = s.score_one(current, raw)
            checks = [c.as_dict() for c in evaluate_sell(score, cfg, macro_score=45.)
                      if c.key not in {"sell_stop_loss", "sell_take_profit"}]
            expected = clean({"total": score.total, "grade": score.grade,
                              **{key: value.score for key, value in score.parts.items()},
                              "dy": score.dividend_yield, "valuation_detail": score.parts["valuation"].detail,
                              "checks": checks})
            cases.append({"name": f"{variant}-{price}-{pe}-{pb}", "model": copy.deepcopy(model),
                          "quote": {"code": "000001", "price": price, "pe": pe, "pb": pb,
                                    "quote_ts": "2026-10-23T10:00:00+08:00"}, "expected": expected})
    return cases


class LiveModelTests(unittest.TestCase):
    def test_actual_json_passes_same_worker_validator(self):
        model = fixtures()[0]['model']
        now = int(datetime(2026, 10, 23, 10, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000)
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / 'live-model.json'
            for valid in (True, False):
                payload = copy.deepcopy(model)
                if not valid:
                    payload['schema_version'] = 99
                artifact.write_text(json.dumps(payload, allow_nan=False), encoding='utf-8')
                result = subprocess.run(['node', '--experimental-strip-types',
                    '--import', f'data:text/javascript,Date.now=()=>{now}',
                    'tests/check_live_model.mjs', str(artifact)], cwd=ROOT,
                    encoding='utf-8', capture_output=True)
                if valid:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0, '实际错误产物必须阻止发布')

    def test_public_export_contains_only_captured_live_inputs(self):
        case = fixtures()[0]
        model = case["model"]
        payload = json.dumps(model, allow_nan=False)
        self.assertNotIn("holdings_file", payload)
        self.assertNotIn("sell_stop_loss", json.dumps(model["entries"]))
        self.assertNotIn("买入价", payload)
        self.assertEqual(model["schema_version"], 1)
        self.assertEqual(model["provenance"], "python-score-one-live-inputs")
        self.assertEqual(len(model["entries"]["000001"]["pool_refs"]), 6)

    def test_point_in_time_does_not_capture_live_model(self):
        # 用入口显式的 point_in_time 分支守卫，防止未来改动污染回测。
        with patch('touzi.live_model.capture_input', side_effect=AssertionError('不得捕获回测')):
            cfg = load_config()
            s = Screener.__new__(Screener)
            s.cfg, s.live, s.as_of = cfg, True, pd.Timestamp("2026-10-23")
            s._div_summary = None
            s._industry_policy = lambda: pd.DataFrame()
            s._policy_events = lambda: pd.DataFrame()
            s._bond_yield = lambda: 2.
            score = s.score_one({"code": "000001", "name": "测试", "price": 10., "pe_ttm": 10., "pb": 1.},
                                {}, point_in_time=True)
            self.assertIsNone(score.live_input)

    def test_worker_matches_python(self):
        # Python 数据仓库与 Worker 是独立部署；发布副本也必须独立可测试。
        for name in ('live-scoring.mts', 'live-model-validation.mts'):
            worker = ROOT / 'webapp/lib' / name
            if worker.exists():
                self.assertEqual(worker.read_text(encoding='utf-8'),
                                 (ROOT / 'scripts' / name).read_text(encoding='utf-8'))
        result = subprocess.run(['node', '--experimental-strip-types', 'tests/test_live_scoring.mjs'],
                                cwd=ROOT, input=json.dumps(fixtures(), ensure_ascii=False),
                                encoding='utf-8', capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
