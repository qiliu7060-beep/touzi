"""当前已发布输入、抓取证据及历史默认行为的边界回归。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

import pandas as pd
from touzi.config import load_config
from touzi.market_calendar import TZ
from touzi.regime import MacroContext, build_macro_context
from touzi.data.macro_cn import _indexed, apply_publish_lag, build_cn_macro_panel, fetch_cn_bond_10y
from touzi.data.macro_us import fetch_fed_panel
from touzi.signals.macro import RuleResult
from touzi.util import cached_fetch
from scripts.build_investment import build

NOW = datetime(2026, 11, 16, 18, tzinfo=TZ)
FETCHED = "2026-11-16T17:58:00+08:00"


def metadata():
    return {"cache_status": "refreshed", "source_status": "ok", "time_status": "ok", "fetched_at": FETCHED}


def macro_panel():
    dates = pd.date_range("2022-01-31", "2026-10-31", freq="ME")
    one = pd.Series([float(i % 10) / 10 + 1 for i in range(len(dates))], index=dates)
    pmi = pd.DataFrame({"manufacturing": one + 49, "non_manufacturing": one + 50})
    bond_dates = pd.bdate_range(end="2026-11-13", periods=120)
    bond = pd.Series([1.65] * len(bond_dates), index=bond_dates)
    raw = {"pmi": pmi, "cpi": one, "ppi": one, "gdp": one, "bond_10y": bond, "money": pd.DataFrame()}
    aligned = {key: apply_publish_lag(series, 1 if key in ("cpi", "ppi", "gdp") else 0) for key, series in raw.items()}
    return {"raw": raw, "aligned": aligned, "staleness": {}}


def fed_panel():
    dates = pd.bdate_range("2022-01-03", "2026-11-13")
    return pd.DataFrame({"policy_rate": 3.5, "us_10y": 4.1, "dollar": [100 + i / 10000 for i in range(len(dates))]}, index=dates)


def build_ctx():
    value = json.loads((Path(__file__).parent / "fixtures/investment-context-v2.json").read_text(encoding="utf-8"))
    results = {key: RuleResult(item["rule"], item["score"], item["state"], explain=item["explain"]) for key, item in value["macro"]["rules"].items()}
    results["fed"].detail = {"sub_scores": {"policy_rate": 60, "long_rate": 50, "dollar": 55}}
    ctx = MacroContext(as_of=pd.Timestamp("2026-11-16"), results=results, weights=value["macro"]["weights"], macro_score=55.55, position=.641, regime="中性")
    ctx.current_series = macro_panel()["raw"]
    ctx.input_evidence = {key: metadata() for key in ("pmi", "cpi", "ppi", "gdp", "cn_bond_10y")}
    ctx.input_evidence["fed"] = {key: {**metadata(), "observation_date": item["observation_date"], "value": item["value"]} for key, item in value["macro"]["inputs"].items() if key in ("policy_rate", "us_10y", "dollar")}
    return ctx


class FakeCache:
    ttl = timedelta(days=7)

    def __init__(self, hit=None, old=None, saved=None):
        self.hit, self.old, self.saved = hit, old, saved

    def get(self, key, ttl_days=None, allow_stale=False):
        return self.old if allow_stale else self.hit

    def saved_at(self, key):
        return self.saved

    def put(self, key, value):
        # 模拟缓存写入失败但网络返回有效，取数证据仍必须属于网络响应。
        pass


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


class MacroInputTests(unittest.TestCase):
    def test_current_inputs_use_published_raw_while_default_keeps_historical_lag(self):
        panel = macro_panel()
        seen = []
        def score(series, cfg):
            seen.append(series.index[-1])
            return RuleResult("CPI", 50, "fixture", explain="fixture")
        with patch("touzi.regime.build_cn_macro_panel", return_value=panel) as source, patch("touzi.regime.fetch_fed_panel", return_value=fed_panel()), patch("touzi.regime.score_cpi", side_effect=score):
            original = build_macro_context(load_config(), as_of="2026-11-16")
            current = build_macro_context(load_config(), as_of="2026-11-16", input_policy="upstream-observed-live")
        # 默认11月16日只能用9月值（可见标签10月30日）；当前直接用已抓到10月31日。
        self.assertEqual(seen[0], panel["aligned"]["cpi"].index[-2])
        self.assertEqual(seen[1], pd.Timestamp("2026-10-31"))
        self.assertNotEqual(original.current_series, current.current_series)
        self.assertNotIn("current", source.call_args_list[0].kwargs)
        self.assertTrue(source.call_args_list[1].kwargs["current"])
        self.assertEqual(source.call_args_list[1].kwargs["ttl_days"], 0)
        self.assertNotIn("input_evidence", original.as_dict())

    def test_same_actual_input_preserves_formula_and_historical_output(self):
        panel = macro_panel()
        panel["aligned"] = panel["raw"]
        with patch("touzi.regime.build_cn_macro_panel", return_value=panel), patch("touzi.regime.fetch_fed_panel", return_value=fed_panel()):
            before = build_macro_context(load_config(), as_of="2026-11-16")
            after = build_macro_context(load_config(), as_of="2026-11-16", input_policy="upstream-observed-live")
        self.assertEqual(before.as_dict(), after.as_dict())

    def test_future_or_duplicate_current_observations_rejected(self):
        for mode in ("future", "duplicate"):
            panel = macro_panel()
            if mode == "future":
                panel["raw"]["cpi"].loc[pd.Timestamp("2026-11-30")] = 2
            else:
                panel["raw"]["cpi"] = pd.concat([panel["raw"]["cpi"], panel["raw"]["cpi"].iloc[-1:]])
            with patch("touzi.regime.build_cn_macro_panel", return_value=panel):
                with self.assertRaises(ValueError):
                    build_macro_context(load_config(), as_of="2026-11-16", input_policy="upstream-observed-live")
        raw = pd.DataFrame({"月份": ["2026年10月", "2026年10月"], "value": [1, 2]})
        self.assertEqual(_indexed(raw, "value").iloc[-1], 2)
        with self.assertRaises(ValueError):
            _indexed(raw, "value", strict=True)

    def test_current_bond_rejects_bad_dates_and_conflicting_duplicates_before_cleanup(self):
        for dates, values in [
            (["2026-11-13", "2026-11-31"], [1.8, 1.6]),
            (["2026-11-13", "2026-11-13"], [1.6, 1.8]),
        ]:
            raw = pd.DataFrame({"日期": dates, "中国国债收益率10年": values})
            with patch.dict("sys.modules", {"akshare": SimpleNamespace()}), patch("touzi.data.macro_cn.cached_fetch", return_value=raw):
                self.assertEqual(fetch_cn_bond_10y(None, None).iloc[-1], 1.8)
                with self.assertRaises(ValueError):
                    fetch_cn_bond_10y(None, None, metadata={})

    def test_current_panel_does_not_require_unused_money_supply(self):
        raw = macro_panel()["raw"]
        with patch("touzi.data.macro_cn.fetch_cn_pmi", return_value=raw["pmi"]), patch("touzi.data.macro_cn.fetch_cn_cpi", return_value=raw["cpi"]), patch("touzi.data.macro_cn.fetch_cn_ppi", return_value=raw["ppi"]), patch("touzi.data.macro_cn.fetch_cn_gdp", return_value=raw["gdp"]), patch("touzi.data.macro_cn.fetch_cn_bond_10y", return_value=raw["bond_10y"]), patch("touzi.data.macro_cn.fetch_cn_money_supply", side_effect=AssertionError("unused")) as money:
            panel = build_cn_macro_panel(None, None, metadata={}, current=True)
        money.assert_not_called()
        self.assertTrue(panel["raw"]["money"].empty)

    def test_fed_evidence_retains_each_true_observation_before_ffill(self):
        evidence = {}
        def fetch(column, cache, proxy, ttl_days=0, metadata=None):
            metadata.update(globals()["metadata"]())
            end = "2026-11-13" if column == "policy_rate" else "2026-11-16"
            return pd.Series([3.0, 4.0], index=pd.to_datetime(["2026-11-12", end]))
        with patch("touzi.data.macro_us.fetch_yahoo_series", side_effect=fetch):
            panel = fetch_fed_panel(None, None, series=["policy_rate", "us_10y"], metadata=evidence)
        self.assertEqual(str(panel.index[-1].date()), "2026-11-16")
        self.assertEqual(evidence["policy_rate"]["observation_date"], "2026-11-13")
        self.assertEqual(evidence["us_10y"]["observation_date"], "2026-11-16")

    def test_fetch_evidence_distinguishes_response_cache_and_failed_fallback(self):
        frame = pd.DataFrame({"a": [1]})
        with patch("touzi.util.datetime", FixedClock):
            records = {}
            old = NOW.astimezone(timezone.utc) - timedelta(days=10)
            data = cached_fetch(FakeCache(old=frame, saved=old), "key", lambda: frame, ttl_days=0, metadata=records)
            self.assertIs(data, frame)
            self.assertEqual(records["cache_status"], "refreshed")
            self.assertEqual(records["fetched_at"], NOW.astimezone(timezone.utc).isoformat())
            self.assertEqual(records["cache_saved_at"], old.isoformat())
            cached_fetch(FakeCache(hit=frame, saved=NOW.replace(tzinfo=None)), "key", lambda: frame, metadata=records)
            self.assertIsNone(records["fetched_at"])
            self.assertEqual(records["time_status"], "unknown_timezone")
            def failed():
                raise OSError("fixture source failure")
            cached_fetch(FakeCache(old=frame, saved=old), "key", failed, retries=0, sleep=0, metadata=records)
            self.assertEqual(records["cache_status"], "stale_fallback")
            self.assertEqual(records["fetched_at"], old.isoformat())
            self.assertEqual(records["source_status"], "failed")

    def test_current_build_reuses_bond_fetch_and_exports_actual_times(self):
        ctx = build_ctx()
        with patch("scripts.build_investment.build_macro_context", return_value=ctx) as source:
            value = build(NOW)
        self.assertEqual(source.call_args.kwargs["input_policy"], "upstream-observed-live")
        self.assertEqual(value["bond"]["fetched_at"], FETCHED)
        self.assertNotEqual(value["bond"]["fetched_at"], value["built_at"])
        self.assertIsNone(value["macro"]["inputs"]["cpi"]["release_at"])
        self.assertEqual(value["macro"]["inputs"]["policy_rate"]["observation_date"], "2026-11-13")
        self.assertEqual(set(value["macro"]["rules"]), {"pmi", "ppi", "fed", "cpi", "gdp"})

    def test_failed_required_source_cannot_be_hidden_by_new_compute_time(self):
        for key in ("pmi", "cpi", "cn_bond_10y"):
            ctx = build_ctx()
            ctx.input_evidence[key]["cache_status"] = "stale_fallback"
            with patch("scripts.build_investment.build_macro_context", return_value=ctx):
                with self.assertRaises(ValueError):
                    build(NOW)
        ctx = build_ctx()
        ctx.input_evidence["fed"]["policy_rate"]["cache_status"] = "stale_fallback"
        with patch("scripts.build_investment.build_macro_context", return_value=ctx):
            with self.assertRaises(ValueError):
                build(NOW)


if __name__ == "__main__":
    unittest.main()
