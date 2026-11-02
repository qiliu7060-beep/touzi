"""Offline regression cases: facts available at signal time and actual cash flows."""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from touzi.config import load_config
from touzi.screener import Screener
from touzi.backtest import backtest_portfolio, _portfolio_metrics
from touzi.portfolio_pit import disclosed, strict_inputs, simulate_closes, HistoricalDataError


def prices(dates, values, volume=None):
    return pd.DataFrame({"close": values, "volume": volume if volume is not None else 100},
                        index=pd.to_datetime(dates))


def archive():
    dates = pd.bdate_range("2022-01-03", "2023-07-04")
    fin = pd.DataFrame({"report_date": pd.to_datetime(["2021-12-31"]),
                        "available_at": ["2022-04-28"], "is_annual": [True],
                        "roe": [15.0], "net_margin": [10.0], "debt_ratio": [30.0],
                        "current_ratio": [2.0], "profit_growth": [12.0],
                        "revenue_growth": [12.0], "ocf_to_profit": [1.2]})
    return {"fin": fin, "fin_vintage_verified": True,
            "valuation_vintage_verified": True, "bond_vintage_verified": True,
            "div_vintage_verified": True, "div_history_complete": True,
            "pe_hist": pd.Series(10.0, index=dates), "pb_hist": pd.Series(1.0, index=dates),
            "div": pd.DataFrame({"available_at": ["2022-05-01"],
                                 "ex_date": pd.to_datetime(["2022-05-10"]), "dps": [1.0]}),
            "price_hist": prices(dates, [10.0] * len(dates)),
            "signal_price_basis": "unadjusted", "return_price_basis_verified": True,
            "return_price_hist": prices(dates, [10.0] * len(dates)),
            "bond_hist": pd.Series([2.5, 99.0], index=pd.to_datetime(["2022-05-01", "2099-01-01"]))}


class HistoricalFacts(unittest.TestCase):
    def setUp(self):
        self.scr = Screener(load_config(), as_of="2023-01-01")
        self.meta = {"code": "000001", "name": "样本", "industry": "未知",
                     "price": 999.0, "pe_ttm": 999.0, "pb": 999.0}

    def score(self, raw, meta=None, t="2022-06-30"):
        # Any access to a current helper is a regression.
        with patch.object(self.scr, "_bond_yield", side_effect=AssertionError("current bond")), \
             patch.object(self.scr, "_div_summary_map", side_effect=AssertionError("current div")), \
             patch.object(self.scr, "_industry_policy", side_effect=AssertionError("current policy")), \
             patch.object(self.scr, "_policy_events", side_effect=AssertionError("current events")):
            return self.scr.score_one(meta or self.meta, raw, as_of=t,
                                      point_in_time=True, strict_history=True)

    def test_report_period_is_not_publication_date(self):
        raw = archive()
        self.assertEqual(len(strict_inputs(raw, pd.Timestamp("2021-12-31"))["fin"]), 0)
        self.assertEqual(len(strict_inputs(raw, pd.Timestamp("2022-04-28"))["fin"]), 0)
        self.assertEqual(len(strict_inputs(raw, pd.Timestamp("2022-04-29"))["fin"]), 1)

    def test_unknown_publication_and_current_restatements_are_unavailable(self):
        raw = archive()
        raw["fin"] = raw["fin"].drop(columns="available_at")
        self.assertFalse(np.isfinite(self.score(raw).total))
        raw = archive()
        raw["fin_vintage_verified"] = False
        self.assertFalse(np.isfinite(self.score(raw).total))

    def test_restatement_visible_only_after_its_own_publication(self):
        raw = archive()
        version = raw["fin"].copy()
        version["available_at"] = "2022-08-01"
        version["roe"] = 99.0
        raw["fin"] = pd.concat([raw["fin"], version])
        self.assertEqual(disclosed(raw["fin"], pd.Timestamp("2022-06-30"), "report_date").roe.iloc[0], 15)
        self.assertEqual(disclosed(raw["fin"], pd.Timestamp("2022-08-02"), "report_date").roe.iloc[0], 99)

    def test_pe_pb_absent_never_use_current_snapshot(self):
        for field in ("pe_hist", "pb_hist"):
            for bad in (None, pd.Series(dtype=float), pd.Series([5.0], index=pd.to_datetime(["2099-01-01"]))):
                raw = archive()
                raw[field] = bad
                score = self.score(raw)
                self.assertTrue(np.isnan(score.pe_ttm if field == "pe_hist" else score.pb))
                self.assertFalse(np.isfinite(score.total))
                self.assertIn(field, score.data_flags["历史信息缺失"])

    def test_future_rows_and_current_metadata_do_not_change_historical_score(self):
        raw = archive()
        before = self.score(raw)
        raw["pe_hist"].loc[pd.Timestamp("2099-01-01")] = 10000
        raw["pb_hist"].loc[pd.Timestamp("2099-01-01")] = 10000
        raw["price_hist"].loc[pd.Timestamp("2099-01-01")] = [10000, 100]
        future_div = pd.DataFrame({"available_at": ["2099-01-01"],
                                   "ex_date": pd.to_datetime(["2022-06-01"]), "dps": [10000]})
        raw["div"] = pd.concat([raw["div"], future_div])
        raw["fin"] = pd.concat([raw["fin"], raw["fin"].assign(available_at="2099-01-01", roe=999)])
        after = self.score(raw, {**self.meta, "price": 123, "pe_ttm": 1, "pb": 0.1})
        self.assertAlmostEqual(before.total, after.total)
        self.assertEqual(after.parts["dividend"].detail["bond_yield"], 2.5)
        self.assertEqual(before.dividend_yield, after.dividend_yield)

    def test_unknown_dividend_is_missing_not_zero(self):
        raw = archive()
        raw["div"] = None
        raw["div_history_complete"] = False
        score = self.score(raw)
        self.assertTrue(np.isnan(score.dividend_yield))
        self.assertTrue(np.isnan(score.parts["dividend"].score))
        self.assertTrue(np.isnan(score.total))

    def test_ex_date_and_publication_both_required(self):
        raw = archive()
        raw["div"]["ex_date"] = pd.Timestamp("2022-07-01")
        self.assertEqual(self.score(raw).dividend_yield, 0.0)
        # An archived complete history proves no paid dividends yet, unlike unknown data.


class Execution(unittest.TestCase):
    def setUp(self):
        self.dates = pd.to_datetime(["2023-03-31", "2023-04-03", "2023-04-04", "2023-04-05"])
        self.bench = pd.Series([100, 200, 220, 240], index=self.dates)

    def run_ledger(self, px=None, targets=None, cost=0, **kw):
        px = px if px is not None else {"a": prices(self.dates, [10, 20, 22, 24])}
        return simulate_closes(px, self.bench, targets or {self.dates[0]: ["a"]}, cost, **kw)

    def test_weekend_signal_fills_monday_and_benchmark_shares_start(self):
        result = self.run_ledger()
        self.assertEqual(result["trades"].iloc[0].date, self.dates[1])
        self.assertEqual(result["trades"].iloc[0].price, 20)
        self.assertAlmostEqual(result["daily"].nav.iloc[-1], 1.2)
        self.assertAlmostEqual(result["daily"].benchmark_nav.iloc[-1], 1.2)
        self.assertEqual(result["daily"].nav.iloc[0], 1)

    def test_suspended_buy_waits_without_renormalizing_other_stock(self):
        px = {"a": prices(self.dates, [10, 10, 10, 10]),
              "b": prices(self.dates, [10, 20, 30, 40], [100, 0, 100, 100])}
        result = self.run_ledger(px, {self.dates[0]: ["a", "b"]})
        self.assertGreater(result["daily"].cash.iloc[1], 0)
        self.assertEqual(result["trades"].query("code == 'b'").iloc[0].date, self.dates[2])
        self.assertAlmostEqual(result["daily"].nav.iloc[-1], 0.5 + 0.5 * 40/30)

    def test_suspended_sell_remains_held_until_resumption(self):
        px = {"a": prices(self.dates, [10, 10, 30, 20], [100, 100, 0, 100])}
        targets = {self.dates[0]: ["a"], self.dates[1]: []}
        result = self.run_ledger(px, targets)
        self.assertAlmostEqual(result["daily"].nav.iloc[2], 3)
        self.assertEqual(result["trades"].iloc[-1].date, self.dates[3])
        self.assertAlmostEqual(result["daily"].nav.iloc[-1], 2)

    def test_costs_on_actual_buy_and_sell_cashflows(self):
        px = {"a": prices(self.dates, [10, 10, 10, 10])}
        result = self.run_ledger(px, {self.dates[0]: ["a"], self.dates[1]: []}, cost=0.01)
        self.assertAlmostEqual(result["daily"].nav.iloc[-1], (1 - .01)/(1 + .01))
        self.assertAlmostEqual(result["trades"].fee.sum(), 1 - result["daily"].nav.iloc[-1])
        self.assertGreaterEqual(result["daily"].cash.min(), -1e-12)

    def test_unchanged_names_still_rebalance_drift_and_charge_cost(self):
        px = {"a": prices(self.dates, [10, 10, 20, 20]),
              "b": prices(self.dates, [10, 10, 10, 10])}
        result = self.run_ledger(px, {self.dates[0]: ["a", "b"], self.dates[2]: ["a", "b"]}, cost=.01)
        self.assertTrue((result["trades"].date == self.dates[3]).any())
        self.assertGreater(result["trades"].query("date == @self.dates[3]").fee.sum(), 0)

    def test_unchanged_weights_and_prices_have_no_fictitious_turnover(self):
        px = {"a": prices(self.dates, [10]*4)}
        result = self.run_ledger(px, {self.dates[0]: ["a"], self.dates[2]: ["a"]}, cost=.01)
        self.assertEqual(len(result["trades"]), 1)

    def test_initial_loss_is_included_in_drawdown(self):
        self.assertEqual(_portfolio_metrics(pd.Series([-.1, 0.0]), 252)["最大回撤%"], -10)

    def test_missing_volume_and_side_block_never_fill(self):
        for column, value in (("volume", np.nan), ("buy_blocked", True)):
            ph = prices(self.dates, [10]*4)
            ph[column] = value
            result = self.run_ledger({"a": ph})
            self.assertTrue(result["trades"].empty)
            self.assertEqual(result["daily"].nav.iloc[-1], 1)

    def test_expiry_and_end_do_not_invent_liquidation(self):
        px = {"a": prices(self.dates, [10]*4, [100, 0, 0, 100])}
        result = self.run_ledger(px, max_wait_days=2)
        self.assertTrue(result["trades"].empty)
        self.assertIn("expired", result["events"].event.tolist())
        result = self.run_ledger()
        self.assertGreater(result["units"]["a"], 0)
        self.assertFalse((result["trades"].quantity < 0).any())

    def test_future_price_changes_cannot_change_earlier_fills(self):
        before = self.run_ledger()
        changed = {"a": prices(self.dates, [10, 20, 999, 999])}
        after = self.run_ledger(changed)
        pd.testing.assert_frame_equal(before["trades"], after["trades"])
        pd.testing.assert_frame_equal(before["daily"].iloc[:2], after["daily"].iloc[:2])


class PortfolioIntegration(unittest.TestCase):
    def run_b(self, raw):
        cfg = load_config()
        cfg.general["start_date"] = "2022-06-01"
        cfg.backtest["min_stocks_per_period"] = 1
        cfg.backtest["cost_rate"] = 0.0
        dates = pd.bdate_range("2022-06-01", "2023-07-04")
        benchmark = prices(dates, [100]*len(dates))
        meta = pd.DataFrame([{"code": "000001", "name": "样本", "industry": "未知"}])
        universes = {t: meta for t in pd.to_datetime(["2022-06-30", "2022-09-30", "2022-12-30", "2023-03-31"])}
        return backtest_portfolio(cfg, top_n=1, historical_universes=universes,
                                  archived_raws={"000001": raw}, benchmark=benchmark)

    def test_no_archive_no_published_performance_and_audit_is_retained(self):
        raw = archive()
        raw["fin"] = raw["fin"].drop(columns="available_at")
        with self.assertRaises(HistoricalDataError) as ctx:
            self.run_b(raw)
        self.assertFalse(ctx.exception.audit.empty)
        self.assertFalse(ctx.exception.audit.eligible.any())
        self.assertIn("未计算收益", str(ctx.exception))

    def test_real_screener_and_weekend_quarter_end_integration(self):
        result = self.run_b(archive())
        self.assertIn("2022-12-30", result["periods"]["调仓日"].tolist())
        self.assertTrue((result["trades"].date > result["trades"].signal_date).all())
        self.assertAlmostEqual(result["metrics"]["超额"]["累计超额%"], 0)
        self.assertTrue(result["audit"].eligible.all())


if __name__ == "__main__":
    unittest.main()
