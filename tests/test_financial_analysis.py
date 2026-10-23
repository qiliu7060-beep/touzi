"""财务金额、五年窗口、评分及财务预警的确定性验证。"""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from financial_fixtures import statement_tables
from touzi.config import load_config
from touzi.data.financial import analyze_financials, normalize_reports
from touzi.signals.quality import score_quality
from touzi.plans import evaluate_sell

NOW = "2026-10-23T10:00:00+08:00"


def analysis(**kw):
    return analyze_financials(statement_tables(**kw), "食品饮料", as_of=NOW)


def quality(a, industry="食品饮料"):
    return score_quality({"roe": 20., "ocf_to_profit": 1., "profit_growth_std": .1,
                          "industry": industry, "financial_analysis": a}, load_config())


def checks(a):
    return {c.key: c for c in evaluate_sell(SimpleNamespace(parts={"quality": quality(a)}, total=85.), load_config())}


class FinancialAnalysisTests(unittest.TestCase):
    def test_exact_boundaries_and_continuous_scores(self):
        a = analysis()
        self.assertFalse(a["metrics"]["debt_equity"]["excellent"])
        self.assertFalse(a["metrics"]["capex_to_profit"]["excellent"])
        q = quality(a)
        self.assertEqual(q.score, 100.)
        self.assertEqual(q.detail["financial_analysis"]["quality"]["coverage"], 1.)
        for key in ("sell_debt_equity", "sell_capex_to_profit"):
            self.assertTrue(checks(a)[key].hit)
            self.assertEqual(checks(a)[key].level, "预警")
        b = analysis(liabilities=79.99999, capex=24.99999)
        self.assertTrue(b["metrics"]["debt_equity"]["excellent"])
        self.assertTrue(b["metrics"]["capex_to_profit"]["excellent"])
        self.assertFalse(checks(b)["sell_debt_equity"].hit)
        self.assertFalse(checks(b)["sell_capex_to_profit"].hit)

    def test_approved_weights_and_curve(self):
        q = quality(analysis(liabilities=140., capex=62.5))
        self.assertAlmostEqual(q.score, 82.5)
        self.assertEqual(q.detail["sub_scores"]["debt"], 50.)
        self.assertEqual(q.detail["sub_scores"]["capex"], 50.)
        self.assertAlmostEqual(sum(q.detail["financial_analysis"]["quality"]["contributions"].values()), q.score)

    def test_missing_year_does_not_slide_window(self):
        tables = statement_tables()
        tables["lrb"].loc[4, "report_date"] = "2020-12-31"
        a = analyze_financials(tables, "食品饮料", as_of=NOW)
        self.assertEqual(a["window"]["years"], list(range(2021, 2026)))
        self.assertEqual(a["metrics"]["capex_to_profit"]["status"], "missing")
        self.assertFalse(checks(a)["sell_capex_to_profit"].known)

    def test_quarter_cumulative_not_summed(self):
        tables = statement_tables()
        row = {**tables["lrb"].iloc[-1].to_dict(), "report_date": "2025-09-30", "net_profit": 9999.}
        import pandas as pd
        tables["lrb"] = pd.concat([tables["lrb"], pd.DataFrame([row])])
        self.assertEqual(analyze_financials(tables, "食品饮料", as_of=NOW)["metrics"]["capex_to_profit"]["net_profit_sum"], 500.)

    def test_latest_revision_wins(self):
        tables = statement_tables()
        import pandas as pd
        revised = {**tables["lrb"].iloc[-1].to_dict(), "net_profit": 200., "source_updated_at": "2026-04-01T12:00:00+08:00"}
        tables["lrb"] = pd.concat([pd.DataFrame([revised]), tables["lrb"]])
        self.assertEqual(analyze_financials(tables, "食品饮料", as_of=NOW)["metrics"]["capex_to_profit"]["net_profit_sum"], 600.)

    def test_loss_year_included_and_flagged(self):
        tables = statement_tables()
        tables["lrb"].loc[0, "net_profit"] = -100.
        a = analyze_financials(tables, "食品饮料", as_of=NOW)
        self.assertEqual(a["loss_years"], [2021])
        self.assertEqual(a["metrics"]["capex_to_profit"]["net_profit_sum"], 300.)

    def test_nonpositive_profit_is_not_zero_ratio(self):
        a = analysis(profit=-100.)
        self.assertIsNone(a["metrics"]["capex_to_profit"]["value"])
        self.assertIsNone(quality(a).detail["sub_scores"]["capex"])
        self.assertFalse(checks(a)["sell_capex_to_profit"].known)
        self.assertTrue(checks(a)["sell_annual_loss"].hit)
        self.assertTrue(checks(a)["sell_cumulative_loss"].hit)

    def test_nonpositive_equity_zero_score_and_warning(self):
        a = analysis(equity=0.)
        self.assertEqual(quality(a).detail["sub_scores"]["debt"], 0.)
        self.assertIsNone(a["metrics"]["debt_equity"]["value"])
        self.assertTrue(checks(a)["sell_equity_nonpositive"].hit)

    def test_stale_source_is_not_new_warning(self):
        a = analysis(fetched="2026-10-20T09:00:00+08:00", liabilities=200.)
        self.assertEqual(a["status"], "stale")
        self.assertIsNone(quality(a).detail["sub_scores"]["debt"])
        self.assertFalse(checks(a)["sell_debt_equity"].known)
        self.assertEqual(a["fetched_at"], "2026-10-20T09:00:00+08:00")

    def test_future_fetch_is_not_verified(self):
        self.assertEqual(analysis(fetched="2026-10-24T09:00:00+08:00")["status"], "stale")

    def test_industry_exemptions(self):
        for industry in ("银行", "保险", "证券", "金融行业", "房地产"):
            a = analyze_financials(statement_tables(), industry, NOW)
            q = quality(a, industry)
            self.assertIsNone(q.detail["sub_scores"]["debt"])
            self.assertEqual(a["metrics"]["debt_equity"]["status"], "not_applicable")
            self.assertEqual(q.detail["sub_scores"]["capex"] is None, industry != "房地产")

    def test_unknown_industry_retains_values_without_applying_generic_thresholds(self):
        a = analyze_financials(statement_tables(), "", NOW)
        self.assertEqual(a["status"], "partial")
        self.assertEqual(a["metrics"]["debt_equity"]["value"], .8)
        self.assertEqual(a["metrics"]["capex_to_profit"]["value"], .25)
        self.assertIsNone(quality(a).detail["sub_scores"]["debt"])
        self.assertIsNone(quality(a).detail["sub_scores"]["capex"])
        self.assertFalse(checks(a)["sell_debt_equity"].known)
        self.assertFalse(checks(a)["sell_capex_to_profit"].known)

    def test_empty_sources_show_unknown(self):
        a = analyze_financials(None, as_of=NOW)
        self.assertEqual(a["status"], "missing")
        self.assertIsNone(a["fetched_at"])
        self.assertFalse(checks(a)["sell_debt_equity"].known)

    def test_filing_window_cutoff(self):
        self.assertEqual(analyze_financials(None, as_of="2026-04-30")["window"]["end_year"], 2024)
        self.assertEqual(analyze_financials(None, as_of="2026-05-01")["window"]["end_year"], 2025)

    def test_same_scope_and_amount_fields(self):
        raw = {"20241231": {"rCurrency": "CNY", "rType": "合并期末", "data": [
            {"item_field": "RIGHAGGR", "item_value": "242011315120.60"},
            {"item_field": "PARESHARRIGH", "item_value": "999"},
            {"item_field": "TOTLIAB", "item_value": "50000000000"}]}}
        row = normalize_reports("600519", "fzb", raw, NOW).iloc[0]
        self.assertEqual(row["total_equity"], 242011315120.60)
        for field, bad in (("rCurrency", "USD"), ("rType", "母公司期末")):
            changed = copy.deepcopy(raw)
            changed["20241231"][field] = bad
            with self.assertRaises(ValueError):
                normalize_reports("600519", "fzb", changed, NOW)


if __name__ == "__main__":
    unittest.main()
