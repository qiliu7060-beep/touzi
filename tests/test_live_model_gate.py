"""实际发布产物的门禁：坏模型不能覆盖最近合格版本。"""
import copy
from datetime import datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_live_model import fixtures
from touzi.config import load_config
from touzi.live_model import export_model, validate_export_model


class LiveModelGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = fixtures()[0]["model"]
        cls.now = datetime(2026, 10, 23, 10, 1, tzinfo=ZoneInfo("Asia/Shanghai"))

    def candidate(self):
        return copy.deepcopy(self.baseline)

    def test_real_export_with_explicit_missing_financial_score_is_valid(self):
        # 不得用“所有项都有数值”替代正常的可用权重归一。
        model = self.candidate()
        self.assertIsNone(model["entries"]["000001"]["fixed_parts"]["quality"])
        validate_export_model(model, ["000001"], self.now)

    def test_exact_capture_coverage_and_nonempty_model(self):
        for expected in [[], ["000001", "000002"], ["000001", "000001"], ["000002"]]:
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                validate_export_model(self.candidate(), expected, self.now)
        model = self.candidate()
        model["entries"] = {}
        with self.assertRaises(ValueError):
            validate_export_model(model, [], self.now)

    def test_live_export_cannot_silently_skip_uncaptured_stocks(self):
        cfg = load_config()
        cfg["general"]["live"] = True
        res = {"stock_results": [SimpleNamespace(code="000001", live_input=None)],
               "flags": {"as_of": "2026-10-23", "quote_at": "2026-10-23 10:00", "quote_coverage": 1.}}
        with self.assertRaises(ValueError):
            export_model(res, cfg, self.now)

    def test_bad_schema_dates_coverage_and_configuration_are_rejected(self):
        mutations = [
            ("schema", lambda m: m.update(schema_version=2)),
            ("provenance", lambda m: m.update(provenance="unknown")),
            ("future", lambda m: m.update(built_at=(self.now + timedelta(minutes=2)).isoformat())),
            ("expired", lambda m: m.update(expires_at=self.now.isoformat())),
            ("long-expiry", lambda m: m.update(expires_at=(self.now + timedelta(days=3)).isoformat())),
            ("old-quote", lambda m: m.update(quote_at="2026-10-21 15:00")),
            ("future-quote", lambda m: m.update(quote_at="2026-10-23 10:03")),
            ("wrong-day", lambda m: m.update(as_of="2026-10-22")),
            ("coverage", lambda m: m.update(quote_coverage=.89)),
            ("zero-weight", lambda m: m["settings"].update(stock_weights=dict.fromkeys(["valuation", "quality", "dividend", "policy"], 0))),
            ("bad-fee", lambda m: m["fees"].update(commission_rate=-.1)),
            ("bad-stop", lambda m: m["plan"].update(sell_stop_loss=-2)),
            ("wrong-code", lambda m: m["entries"]["000001"].update(code="000002")),
            ("missing-rule", lambda m: m["entries"]["000001"]["sell_checks"].pop()),
            ("missing-pool", lambda m: m["entries"]["000001"]["pool_refs"].update(pe_history="missing")),
            ("null-config", lambda m: m.update(settings=None)),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                model = self.candidate()
                mutate(model)
                with self.assertRaises(ValueError):
                    validate_export_model(model, ["000001"], self.now)

    def test_resource_limits_apply_to_actual_payload(self):
        model = self.candidate()
        key = next(iter(model["pools"]))
        model["pools"][key] = [1.] * 10001
        with self.assertRaises(ValueError):
            validate_export_model(model, ["000001"], self.now)
        model = self.candidate()
        model["entries"]["000001"]["provenance"]["oversize"] = "x" * (12 * 1024 * 1024)
        with self.assertRaises(ValueError):
            validate_export_model(model, ["000001"], self.now)

    def test_expired_actual_model_is_not_republished(self):
        with self.assertRaises(ValueError):
            validate_export_model(self.candidate(), ["000001"], self.now + timedelta(days=2))


if __name__ == "__main__":
    unittest.main()
