"""Offline regression cases: facts available at signal time and actual cash flows."""
import unittest
import json
import tempfile
from pathlib import Path
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

    def test_unpadded_dates_select_chronologically_latest_version(self):
        fin = pd.concat([archive()["fin"]] * 2, ignore_index=True)
        fin["available_at"] = ["2022-9-1", "2022-10-1"]
        fin["report_date"] = ["2021-12-31", pd.Timestamp("2021-12-31")]
        fin["roe"] = [15, 99]
        visible = disclosed(fin, pd.Timestamp("2022-11-01"), "report_date")
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible.roe.iloc[0], 99)

    def test_conflicting_same_time_versions_fail_but_exact_duplicates_are_safe(self):
        fin = archive()["fin"]
        duplicate = pd.concat([fin, fin], ignore_index=True)
        self.assertEqual(len(disclosed(duplicate, pd.Timestamp("2022-06-30"), "report_date")), 1)
        duplicate.loc[1, "roe"] = 99
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            disclosed(duplicate, pd.Timestamp("2022-06-30"), "report_date")

    def test_future_version_conflicts_do_not_change_current_information(self):
        fin = archive()["fin"]
        future = fin.assign(available_at="2099-01-01")
        versions = pd.concat([fin, future, future.assign(roe=99)])
        visible = disclosed(versions, pd.Timestamp("2022-06-30"), "report_date")
        self.assertEqual(visible.roe.iloc[0], 15)

    def test_empty_quality_metrics_never_rank_a_dated_financial_row(self):
        raw = archive()
        for col in ("roe", "profit_growth", "ocf_to_profit", "debt_ratio", "current_ratio"):
            raw["fin"][col] = np.nan
        score = self.score(raw)
        self.assertTrue(np.isnan(score.parts["quality"].score))
        self.assertTrue(np.isnan(score.total))
        self.assertIn("quality", score.data_flags["历史信息缺失"])

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

    def test_side_block_never_fills(self):
        ph = prices(self.dates, [10]*4)
        ph["buy_blocked"] = True
        result = self.run_ledger({"a": ph})
        self.assertTrue(result["trades"].empty)
        self.assertEqual(result["daily"].nav.iloc[-1], 1)

    def test_unknown_volume_or_missing_quote_refuses_performance(self):
        for col, value in (("volume", np.nan), ("volume", -1), ("close", np.nan)):
            with self.subTest(col=col, value=value):
                ph = prices(self.dates, [10]*4)
                ph.loc[self.dates[2], col] = value
                with self.assertRaises(HistoricalDataError) as ctx:
                    self.run_ledger({"a": ph})
                self.assertEqual(ctx.exception.audit.iloc[0]["code"], "a")
                self.assertEqual(ctx.exception.audit.iloc[0]["date"], self.dates[2])

    def test_truncated_and_internal_price_gaps_are_not_suspensions(self):
        for dates in (self.dates[:2], self.dates.delete(2)):
            with self.assertRaisesRegex(HistoricalDataError, "unexplained_return_price_gap"):
                self.run_ledger({"a": prices(dates, [10]*len(dates))})

    def test_sourced_suspension_retains_cash_and_marks_without_filling(self):
        ph = prices(self.dates, [10, np.nan, 12, 13])
        ph["security_status"] = "trading"
        ph["status_source"] = ""
        ph.loc[self.dates[1], ["security_status", "status_source", "volume"]] = ["suspended", "archive:event-1", np.nan]
        result = self.run_ledger({"a": ph})
        self.assertEqual(result["daily"].cash.iloc[1], 1)
        self.assertEqual(result["trades"].date.iloc[0], self.dates[2])
        self.assertIn("suspended_mark", result["events"].event.tolist())

    def test_sourced_suspended_holding_uses_prior_mark(self):
        ph = prices(self.dates, [10, 10, np.nan, 12])
        ph["security_status"] = "trading"
        ph["status_source"] = ""
        ph.loc[self.dates[2], ["security_status", "status_source", "volume"]] = ["suspended", "archive:event-2", np.nan]
        result = self.run_ledger({"a": ph})
        self.assertEqual(result["daily"].nav.iloc[2], 1)
        self.assertAlmostEqual(result["daily"].nav.iloc[-1], 1.2)

    def test_unsupported_delisting_and_unsourced_suspension_refuse_performance(self):
        for status in ("delisted", "suspended"):
            ph = prices(self.dates, [10]*4)
            ph["security_status"] = "trading"
            ph.loc[self.dates[2], "security_status"] = status
            with self.assertRaises(HistoricalDataError):
                self.run_ledger({"a": ph})

    def test_prices_after_exit_are_not_required(self):
        px = {"a": prices(self.dates[:3], [10]*3)}
        result = self.run_ledger(px, {self.dates[0]: ["a"], self.dates[1]: []})
        self.assertAlmostEqual(result["daily"].nav.iloc[-1], 1)

    def test_future_selection_does_not_require_prelisting_quotes(self):
        px = {"a": prices(self.dates[1:], [10]*3)}
        result = self.run_ledger(px, {self.dates[0]: [], self.dates[1]: ["a"]})
        self.assertEqual(result["trades"].date.iloc[0], self.dates[2])

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

    def test_price_truncation_stops_and_retains_score_and_price_audits(self):
        raw = archive()
        raw["return_price_hist"] = raw["return_price_hist"].loc[:"2022-07-01"]
        with self.assertRaises(HistoricalDataError) as ctx:
            self.run_b(raw)
        audit = ctx.exception.audit
        self.assertTrue(audit["signal_date"].notna().any())
        self.assertIn("unexplained_return_price_gap", audit["missing"].tolist())

    def test_last_quarter_is_valuation_boundary_and_future_returns_are_excluded(self):
        raw = archive()
        raw["return_price_hist"].loc["2023-07-03":, "close"] = 1000
        result = self.run_b(raw)
        self.assertEqual(result["daily"].index[-1], pd.Timestamp("2023-06-30"))
        self.assertEqual(result["periods"].iloc[-1]["下期"], "2023-06-30")
        self.assertEqual(result["periods"].iloc[-1]["调仓日"], "2023-03-31")
        self.assertEqual(result["metrics"]["组合"]["累计收益%"], 0)
        self.assertFalse((result["audit"].signal_date == pd.Timestamp("2023-06-30")).any())


class PipelineIntegration(unittest.TestCase):
    def test_expected_refusal_is_saved_without_reusing_old_portfolio(self):
        from scripts.run_all import _run_portfolio_checked, _load_backtest

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pd.DataFrame({"旧收益": [96.04]}).to_csv(root / "backtest_portfolio.csv", index=False)
            (root / "backtest_metrics.json").write_text(json.dumps({"portfolio": {"累计收益%": 96.04}}))
            audit = pd.DataFrame([{"code": "000001", "missing": "fin"}])
            with patch("builtins.print"):
                result = _run_portfolio_checked(
                    unittest.mock.Mock(side_effect=HistoricalDataError("无原始档案", audit)), root)
                _, loaded = _load_backtest(load_config(), root)
            self.assertIsNone(result)
            self.assertIsNone(loaded)
            status = json.loads((root / "backtest_b_status.json").read_text())
            self.assertEqual(status["status"], "not_computed")
            self.assertEqual(pd.read_csv(root / "backtest_b_audit.csv")["missing"].iloc[0], "fin")

    def test_unexpected_programming_error_is_not_suppressed(self):
        from scripts.run_all import _run_portfolio_checked

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "bug"):
                _run_portfolio_checked(unittest.mock.Mock(side_effect=RuntimeError("bug")), Path(folder))

    def test_latest_refusal_blocks_even_previously_computed_cache(self):
        from scripts.run_all import _load_backtest

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pd.DataFrame({"组合收益%": [0]}).to_csv(root / "backtest_portfolio.csv", index=False)
            meta = {"status": "computed", "history_contract": "pit-v2"}
            (root / "backtest_metrics.json").write_text(json.dumps({"portfolio": meta}))
            status_path = root / "backtest_b_status.json"
            status_path.write_text(json.dumps(meta))
            self.assertIsNotNone(_load_backtest(load_config(), root)[1])
            status_path.write_text(json.dumps({**meta, "status": "not_computed"}))
            with patch("builtins.print"):
                self.assertIsNone(_load_backtest(load_config(), root)[1])

    def test_refused_b_still_runs_c_and_generates_reports(self):
        from scripts.run_all import build_parser, run_once
        from touzi.util import Cache

        cfg = load_config()
        args = build_parser().parse_args(["--skip-macro"])
        empty = pd.DataFrame()
        macro_bt = {"summary": {}, "table": empty, "detail": empty, "tables": {}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch("touzi.screener.Screener") as scr, \
                 patch("touzi.tracker.ensure_holdings_template"), \
                 patch("touzi.tracker.Tracker") as tracker, \
                 patch("touzi.portfolio.Portfolio") as portfolio, \
                 patch("touzi.backtest.validate_macro_rules", return_value=macro_bt), \
                 patch("touzi.backtest.backtest_portfolio", side_effect=HistoricalDataError("档案不足")), \
                 patch("touzi.backtest.backtest_stocks", return_value={}) as c, \
                 patch("touzi.regime.data_freshness", return_value=None), \
                 patch("touzi.signals.policy.load_industry_policy", return_value=empty), \
                 patch("touzi.signals.policy.load_policy_events", return_value=empty), \
                 patch("touzi.report.build_dashboard", return_value="dashboard.html") as dashboard, \
                 patch("touzi.report.build_markdown", return_value="report.md") as markdown, \
                 patch("builtins.print"):
                scr.return_value.run.return_value = empty
                scr.return_value.results = []
                tracker.return_value.build.return_value = pd.DataFrame(columns=["建议动作"])
                portfolio.return_value.build.return_value = empty
                result = run_once(cfg, args, Cache(root / "cache"), None, root)
            c.assert_called_once()
            dashboard.assert_called_once()
            markdown.assert_called_once()
            self.assertIsNone(result["port_bt"])
            saved = json.loads((root / "backtest_metrics.json").read_text())
            self.assertEqual(saved["portfolio"]["status"], "not_computed")
            self.assertNotIn("组合", saved["portfolio"])


if __name__ == "__main__":
    unittest.main()

