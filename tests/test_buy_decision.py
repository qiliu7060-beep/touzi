"""买入硬门槛的数据来源与缺失值回归测试。"""

from types import SimpleNamespace
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from touzi.config import load_config
from touzi.plans import decide, evaluate_buy, evaluate_sell


def stock(*, pe_source="个股5年历史", pb_source="个股5年历史",
          dividend_yield=3.0, total=80.0):
    valuation = SimpleNamespace(detail={
        "pe_percentile": 0.3,
        "pb_percentile": 0.3,
        "pe_percentile_source": pe_source,
        "pb_percentile_source": pb_source,
        "graham_product": 30.0,
    })
    quality = SimpleNamespace(score=75.0, detail={"financial_analysis": {"metrics": {
        "debt_equity": {"value": .6, "status": "ok"},
        "capex_to_profit": {"value": .2, "status": "ok"},
    }}})
    return SimpleNamespace(
        code="600519", name="测试股", price=20.0, pe_ttm=15.0, pb=2.0,
        dividend_yield=dividend_yield, total=total,
        parts={"valuation": valuation, "quality": quality},
    )


def by_key(score):
    return {check.key: check for check in evaluate_buy(score, load_config(), macro_score=50.0)}


class BuyDecisionTests(unittest.TestCase):
    def test_self_history_percentiles_can_pass(self):
        checks = by_key(stock())
        self.assertEqual(len(checks), 10)
        self.assertTrue(all(check.hit for check in checks.values()))
        self.assertEqual(decide(stock(), load_config(), macro_score=50.0).action, "建仓")

    def test_cross_section_cannot_pose_as_own_history(self):
        score = stock(pe_source="行业内截面", pb_source="全市场截面")
        checks = by_key(score)
        self.assertFalse(checks["pe_percentile"].hit)
        self.assertFalse(checks["pe_percentile"].known)
        self.assertFalse(checks["pb_percentile"].hit)
        self.assertFalse(checks["pb_percentile"].known)
        self.assertIsNone(checks["pe_percentile"].value)
        self.assertEqual(decide(score, load_config(), macro_score=50.0).action, "观望")

    def test_missing_data_is_not_a_known_near_miss(self):
        score = stock(dividend_yield=float("nan"))
        checks = by_key(score)
        self.assertFalse(checks["dividend_yield"].known)
        decision = decide(score, load_config(), macro_score=50.0)
        self.assertEqual(decision.action, "观望")
        self.assertIn("数据待核验", decision.reasons[0])

    def test_one_known_failure_remains_near_buy(self):
        score = stock(dividend_yield=1.5)
        self.assertEqual(decide(score, load_config(), macro_score=50.0).action, "接近建仓")

    def test_zero_score_is_known_failure_not_missing(self):
        check = by_key(stock(total=0.0))["total"]
        self.assertEqual(check.value, 0.0)
        self.assertTrue(check.known)
        self.assertFalse(check.hit)

    def test_sell_percentile_requires_own_history_too(self):
        score = stock(pe_source="行业内截面")
        score.parts["valuation"].detail["pe_percentile"] = 0.9
        checks = {check.key: check for check in evaluate_sell(score, load_config(), macro_score=50.0)}
        self.assertFalse(checks["sell_pe_percentile"].hit)
        self.assertFalse(checks["sell_pe_percentile"].known)
        score.parts["valuation"].detail["pe_percentile_source"] = "个股5年历史"
        checks = {check.key: check for check in evaluate_sell(score, load_config(), macro_score=50.0)}
        self.assertTrue(checks["sell_pe_percentile"].hit)


if __name__ == "__main__":
    unittest.main()
