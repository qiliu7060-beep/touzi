"""扣非独立展示接入：所有请求均mock；真实核验摘录与合成异常分开。"""
import copy
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from financial_fixtures import statement_tables
from touzi.config import load_config
from touzi.data.deducted_profit import (MAX_BYTES, annual_deducted_evidence, amount,
                                       fetch_deducted_parent_profit, normalize_deducted_reports, source_url)
from touzi.data.financial import analyze_financials, fetch_financial_statements
from touzi.financial_coverage import financial_coverage
from touzi.plans import decide, evaluate_buy, evaluate_sell
from touzi.signals.quality import score_quality
from touzi.util import Cache
from test_buy_decision import stock

NOW = "2026-11-06T10:00:00+08:00"
SHA = "a" * 64  # 合成响应标识，不是现场响应哈希。
SAMPLES = json.loads((ROOT / "tests/fixtures/deducted_parent_verified.json").read_text())["samples"]


def report(value="100.00", published="20260401"):
    return {"rCurrency": "CNY", "rType": "合并期末", "publish_date": published,
            "data_source": "其他", "is_audit": "未审计", "data": [
                {"item_field": "NPCUT", "item_title": "扣非净利润", "item_source": "ysb", "item_value": value},
                {"item_field": "NPCUT", "item_title": "扣非净利润", "item_source": "ysb", "item_value": value},
                {"item_field": "NETPROFIT", "item_value": "999999"},
                {"item_field": "PARENETP", "item_value": "888888"}]}


def normalized(reports=None, fetched=NOW):
    return normalize_deducted_reports("600519", {"20251231": report()} if reports is None else reports, fetched, SHA)


def wire(reports=None):
    return json.dumps({"result": {"data": {"report_list": reports or {"20251231": report()}}}}).encode()


class DeductedProfitTests(unittest.TestCase):
    def test_verified_real_amounts_match_annual_report_not_parent_profit(self):
        for sample in SAMPLES:
            with self.subTest(code=sample["code"], year=sample["year"]):
                f = normalize_deducted_reports(sample["code"],
                    {f'{sample["year"]}1231': report(sample["value"], sample["published"])}, NOW, SHA)
                row = f.iloc[0]
                self.assertEqual(Decimal(row.amount_exact), Decimal(sample["value"]))
                self.assertNotEqual(Decimal(row.amount_exact), Decimal(sample["parent"]))
                self.assertEqual(row.unit, "yuan")
                self.assertEqual(row.currency, "CNY")
                self.assertIsNone(row.disclosed_at)
                self.assertFalse(row.point_in_time_eligible)

    def test_bank_million_yuan_conversion_is_evidence_not_adapter_multiplier(self):
        sample = next(s for s in SAMPLES if s["code"] == "600036" and s["year"] == 2024)
        self.assertEqual(Decimal(sample["value"]), Decimal("148011") * 1000000)
        # 新浪原始金额已为元，不能再次乘百万。
        self.assertEqual(normalized({"20241231": report(sample["value"])}).iloc[0].deducted_parent_net_profit, 148011000000.)

    def test_restatement_versions_are_not_silently_replaced(self):
        for s in (s for s in SAMPLES if "adjusted" in s):
            r = normalized({"20211231": report(s["value"])}).iloc[0]
            self.assertNotEqual(Decimal(r.amount_exact), Decimal(s["adjusted"]))
            self.assertIn("restatement_unverified", r.version_basis)

    def test_supplier_date_is_not_verified_disclosure(self):
        s = SAMPLES[-1]
        f = normalized({"20211231": report(s["value"], s["published"])})
        _, ev = annual_deducted_evidence(f, "2021-12-31", NOW)
        self.assertEqual(ev["supplier_disclosed_date"], "2022-03-19")
        self.assertNotEqual(ev["supplier_disclosed_date"], s["verified_document_date"])
        self.assertIsNone(ev["disclosed_at"])
        self.assertEqual(ev["disclosure_status"], "unverified")

    def test_zero_negative_exact_precision_and_nonfinite(self):
        for value in ("0", "-45393719289.830000", "86240905977.420000"):
            row = normalized({"20251231": report(value)}).iloc[0]
            self.assertEqual(Decimal(row.amount_exact), Decimal(value))
            self.assertEqual(row.value_status, "ok")
        for value in (None, True, False, "--", "nan", "Infinity", "1e999"):
            self.assertIsNone(amount(value))
            self.assertNotEqual(normalized({"20251231": report(value)}).iloc[0].value_status, "ok")

    def test_wrong_currency_scope_or_identity_retains_missing(self):
        for key, value, reason in (("rCurrency", "USD", "currency_unverified"),
                                   ("rType", "母公司期末", "scope_unverified")):
            r = report(); r[key] = value
            f = normalized({"20251231": r})
            v, ev = annual_deducted_evidence(f, "2025-12-31", NOW)
            self.assertIsNone(v)
            self.assertEqual(ev["reason"], reason)
        r = report()
        r["data"][0]["item_source"] = "lrb"
        self.assertEqual(normalized({"20251231": r}).iloc[0].value_status, "field_identity_changed")

    def test_does_not_fallback_to_net_profit_or_eps(self):
        r = report(); r["data"] = r["data"][2:] + [{"item_field": "EPS", "item_value": "68.65"}]
        row = normalized({"20251231": r}).iloc[0]
        self.assertEqual(row.value_status, "field_missing")
        self.assertIsNone(row.amount_exact)

    def test_duplicate_exact_values_accepted_conflicts_rejected(self):
        r = report("100.00"); r["data"][1]["item_value"] = "100"
        self.assertEqual(normalized({"20251231": r}).iloc[0].value_status, "ok")
        for value in ("101", None):
            r["data"][1]["item_value"] = value
            f = normalized({"20251231": r})
            v, ev = annual_deducted_evidence(f, "2025-12-31", NOW)
            self.assertIsNone(v)
            self.assertEqual(ev["reason"], "duplicate_conflict")

    def test_annual_only_no_quarter_ttm_or_year_sum(self):
        rs = {"20251231": report("40"), "20250930": report("999"), "20250630": report("888"),
              "20251231TTM": report("777"), "notdate": report("666")}
        f = normalized(rs)
        self.assertEqual(len(f), 1)
        self.assertEqual(f.iloc[0].deducted_parent_net_profit, 40.)
        with self.assertRaises(ValueError):
            normalized({"20250930": report()})

    def test_duplicate_report_rows_rejected(self):
        f = pd.concat([normalized(), normalized()], ignore_index=True)
        self.assertEqual(annual_deducted_evidence(f, "2025-12-31", NOW)[1]["reason"], "duplicate_report")

    def test_five_year_window_missing_does_not_slide(self):
        tables = statement_tables(fetched=NOW)
        tables["deducted_parent"] = normalized({f"{y}1231": report(str(y)) for y in (2020, 2021, 2022, 2024, 2025)})
        a = analyze_financials(tables, "制造业", NOW)
        self.assertEqual(a["window"]["years"], list(range(2021, 2026)))
        self.assertIsNone(a["years"][2]["deducted_parent_net_profit"])
        self.assertEqual(a["years"][2]["deducted_parent_evidence"]["reason"], "year_missing")
        self.assertEqual(financial_coverage({"a": {"financial_analysis": a}})["fields"],
                         {"expected": 5, "deducted_parent_missing": 1})

    def test_stale_future_unknown_time_and_future_disclosure(self):
        f = normalized(fetched="2026-11-01T10:00:00+08:00")
        v, ev = annual_deducted_evidence(f, "2025-12-31", NOW)
        self.assertEqual(v, 100.)
        self.assertEqual(ev["status"], "stale")
        for stamp in ("2026-11-07T10:00:00+08:00", "2026-11-06T10:00:00", "bad", None):
            self.assertIsNone(annual_deducted_evidence(normalized(fetched=stamp), "2025-12-31", NOW)[0])
        f = normalized({"20251231": report(published="20261107")})
        self.assertIsNone(annual_deducted_evidence(f, "2025-12-31", NOW)[0])

    def test_new_cache_hash_url_and_fetched_time_survive_roundtrip(self):
        raw = wire()
        with tempfile.TemporaryDirectory() as d, patch("touzi.data.deducted_profit.urllib.request.urlopen", return_value=io.BytesIO(raw)) as net:
            cache = Cache(d)
            first = fetch_deducted_parent_profit("000002", cache)
            self.assertEqual(net.call_args.kwargs["timeout"], 8)
            self.assertIn("paperCode=sz000002", net.call_args.args[0])
            self.assertEqual(first.iloc[0].response_sha256, hashlib.sha256(raw).hexdigest())
            self.assertEqual(first.attrs["deducted_source"]["cache_status"], "refreshed")
            with patch("touzi.data.deducted_profit.urllib.request.urlopen") as hit:
                second = fetch_deducted_parent_profit("000002", cache)
            hit.assert_not_called()
            self.assertEqual(first.iloc[0].fetched_at, second.iloc[0].fetched_at)
            self.assertEqual(first.iloc[0].amount_exact, second.iloc[0].amount_exact)
            self.assertEqual(second.attrs["deducted_source"]["cache_status"], "hit")
            self.assertIsNone(cache.get("statements_v1_000002_lrb"))

    def test_source_failure_uses_old_cache_without_retimestamping(self):
        old = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=3)).isoformat()
        with tempfile.TemporaryDirectory() as d:
            cache = Cache(d)
            key = "deducted_parent_v1_sh600519"
            cache.put(key, normalized(fetched=old))
            with patch("touzi.data.deducted_profit.urllib.request.urlopen", side_effect=TimeoutError("private")), patch("touzi.util.time.sleep"):
                f = fetch_deducted_parent_profit("600519", cache, ttl_days=0)
            self.assertEqual(f.iloc[0].fetched_at, old)
            self.assertEqual(f.attrs["deducted_source"]["cache_status"], "stale_fallback")
            _, ev = annual_deducted_evidence(f, "2025-12-31", datetime.now(ZoneInfo("Asia/Shanghai")))
            self.assertEqual(ev["status"], "stale")
            self.assertEqual(cache.get(key, allow_stale=True).iloc[0].fetched_at, old)

    def test_malformed_or_all_missing_payload_preserves_old_good_cache(self):
        bads = (b"{", b'{"result":{}}', wire({"20251231": report(None)}), b"x" * (MAX_BYTES + 1))
        for raw in bads:
            with self.subTest(size=len(raw)), tempfile.TemporaryDirectory() as d:
                cache = Cache(d)
                cache.put("deducted_parent_v1_sh600519", normalized())
                with patch("touzi.data.deducted_profit.urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(raw)), patch("touzi.util.time.sleep"):
                    f = fetch_deducted_parent_profit("600519", cache, ttl_days=0)
                self.assertEqual(f.attrs["deducted_source"]["cache_status"], "stale_fallback")
                self.assertEqual(f.iloc[0].amount_exact, "100.00")

    def test_partial_response_preserves_missing_reasons(self):
        f = normalized({"20241231": report(), "20251231": report(None)})
        value, ev = annual_deducted_evidence(f, "2025-12-31", NOW)
        self.assertIsNone(value)
        self.assertEqual(ev["reason"], "amount_missing_or_invalid")

    def test_source_failure_without_cache_returns_missing(self):
        with tempfile.TemporaryDirectory() as d, patch("touzi.data.deducted_profit.urllib.request.urlopen", side_effect=TimeoutError("private")), patch("touzi.util.time.sleep"):
            f = fetch_deducted_parent_profit("600519", Cache(d))
        self.assertTrue(f.empty)
        self.assertEqual(f.attrs["deducted_source"]["cache_status"], "source_failed")
        self.assertIsNone(annual_deducted_evidence(f, "2025-12-31", NOW)[0])

    def test_correct_markets_and_reject_non_code(self):
        for code, symbol in (("600519", "sh600519"), ("000002", "sz000002"), ("920001", "bj920001")):
            self.assertIn("paperCode=" + symbol, source_url(code))
        with self.assertRaises(ValueError):
            source_url("600519&source=lrb")

    def test_default_disabled_and_explicit_opt_in_only(self):
        self.assertIs(load_config().general.collect_deducted_parent_profit, False)
        with patch("touzi.data.financial.cached_fetch", return_value=pd.DataFrame()), patch("touzi.data.financial.fetch_deducted_parent_profit", return_value=normalized()) as fetch:
            old = fetch_financial_statements("600519", None)
            fetch.assert_not_called()
            self.assertEqual(set(old), {"fzb", "lrb", "llb"})
            new = fetch_financial_statements("600519", None, include_deducted=True)
            fetch.assert_called_once_with("600519", None, ttl_days=1)
            self.assertIn("deducted_parent", new)

    def test_quality_buy_sell_decisions_and_three_statement_time_unchanged(self):
        baseline = statement_tables(fetched=NOW)
        variants = [None, normalized(), normalized(fetched="2026-11-01T10:00:00+08:00"), normalized({"20251231": report(None)})]
        results = []
        for variant in variants:
            tables = copy.deepcopy(baseline)
            tables["deducted_parent"] = variant
            a = analyze_financials(tables, "制造业", NOW)
            q = score_quality({"roe": 20., "ocf_to_profit": 1., "profit_growth_std": .1,
                               "industry": "制造业", "financial_analysis": a}, load_config())
            s = stock(); s.parts["quality"] = q
            cfg = load_config()
            results.append((q.score, q.detail["sub_scores"], a["metrics"], a["fetched_at"], a["status"],
                            evaluate_buy(s, cfg, 50), evaluate_sell(s, cfg, 50), decide(s, cfg, 50)))
        for r in results[1:]:
            self.assertEqual(r, results[0])

    def test_public_entry_retains_evidence_and_serializes_without_nan(self):
        from touzi.close_snapshot import public_entry, json_bytes
        tables = statement_tables(fetched=NOW); tables["deducted_parent"] = normalized()
        a = analyze_financials(tables, "制造业", NOW)
        s = stock(); s.parts["quality"].detail["financial_analysis"] = a
        e = public_entry(s, load_config(), 50, NOW)
        decoded = json.loads(json_bytes(e))
        row = decoded["financial_analysis"]["years"][-1]
        self.assertEqual(row["deducted_parent_net_profit"], 100.)
        self.assertEqual(row["deducted_parent_evidence"]["response_sha256"], SHA)
        self.assertIsNone(row["deducted_parent_evidence"]["disclosed_at"])

    def test_actual_renderer_shows_independent_time_staleness_and_escapes(self):
        subprocess.run(["node", "tests/test_deducted_profit_ui.cjs"], cwd=ROOT,
                       check=True, capture_output=True)

    def test_historical_scoring_does_not_use_current_deducted_cache(self):
        from touzi.screener import Screener
        s = Screener.__new__(Screener)
        s.cfg, s.live, s.as_of = load_config(), True, pd.Timestamp("2026-10-23")
        s._div_summary = None
        s._industry_policy = lambda: pd.DataFrame()
        s._policy_events = lambda: pd.DataFrame()
        s._bond_yield = lambda: 2.
        tables = statement_tables(fetched=NOW); tables["deducted_parent"] = normalized()
        quote = {"code": "000001", "name": "测试", "price": 10., "pe_ttm": 10., "pb": 1.}
        score = s.score_one(quote, {"statements": tables}, point_in_time=True)
        a = score.parts["quality"].detail["financial_analysis"]
        self.assertTrue(all(r["deducted_parent_net_profit"] is None for r in a["years"]))


if __name__ == "__main__":
    unittest.main()
