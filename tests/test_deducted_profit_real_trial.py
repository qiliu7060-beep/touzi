"""Complete saved HTTP bytes, never live requests. Known version drift must block."""
import copy
from decimal import Decimal
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from scripts.validate_deducted_profit_trial import validate_bundle, validate_capture
from financial_fixtures import statement_tables
from test_buy_decision import stock
from touzi.config import load_config
from touzi.data.deducted_profit import fetch_deducted_parent_profit
from touzi.data.financial import analyze_financials
from touzi.plans import decide, evaluate_buy, evaluate_sell
from touzi.signals.quality import score_quality
from touzi.util import Cache

BUNDLE = ROOT / "tests/fixtures/deducted_trial_20261106"
MANIFEST = json.loads((BUNDLE / "manifest.json").read_text())
BENCHMARKS = json.loads((BUNDLE / "benchmarks.json").read_text())["rows"]


def capture(code="600519"):
    meta = copy.deepcopy(next(c for c in MANIFEST["attempts"] if c["code"] == code))
    return gzip.decompress((BUNDLE / meta["raw_file"]).read_bytes()), meta


class RealDeductedTrialTests(unittest.TestCase):
    def test_complete_real_captures_detect_all_five_version_differences(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("offline only")):
            report = validate_bundle(BUNDLE)
        self.assertEqual(len(report["rows"]), 20)
        self.assertEqual(report["matched_inspected_document"], 15)
        self.assertFalse(report["validation_passed"])
        self.assertFalse(report["production_enable_authorized"])
        drift = {(r["code"],r["year"]) for r in report["rows"] if r["validation_status"] == "known_pre_adjustment_version"}
        self.assertEqual(drift, {("600519",2021),("600519",2022),("603195",2021),("603195",2022),("000002",2022)})
        self.assertTrue(all(not r["point_in_time_eligible"] for r in report["rows"]))

    def test_tampered_response_cannot_pass_hash_check(self):
        raw,meta=capture()
        with self.assertRaisesRegex(ValueError,"response_hash_mismatch"):
            validate_capture(raw+b" ",meta,BENCHMARKS)

    def test_duplicate_report_key_not_silently_overwritten(self):
        raw,meta=capture()
        raw=b'{"result":{"data":{"report_list":{"20211231":{},"20211231":{}}}}}'
        meta["sha256"]=hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(ValueError,"duplicate_json_key"):
            validate_capture(raw,meta,BENCHMARKS)

    def test_missing_year_field_change_and_new_version_stay_blocked(self):
        original,meta=capture()
        for change,expected in (("missing","year_missing"),("identity","field_identity_changed"),("value","unrecognized_amount_version")):
            data=json.loads(original)
            reports=data["result"]["data"]["report_list"]
            if change=="missing":
                del reports["20251231"]
            else:
                for item in reports["20251231"]["data"]:
                    if item.get("item_field")=="NPCUT":
                        item["item_title" if change=="identity" else "item_value"] = "changed" if change=="identity" else "1"
            raw=json.dumps(data).encode();meta["sha256"]=hashlib.sha256(raw).hexdigest()
            rows=validate_capture(raw,meta,BENCHMARKS)
            self.assertEqual(next(r for r in rows if r["year"]==2025)["validation_status"],expected)

    def test_bank_unit_and_property_loss_are_not_rescaled(self):
        rows=validate_bundle(BUNDLE)["rows"]
        bank=next(r for r in rows if r["code"]=="600036" and r["year"]==2025)
        loss=next(r for r in rows if r["code"]=="000002" and r["year"]==2025)
        self.assertEqual(Decimal(bank["actual_yuan"]),Decimal("150007")*1000000)
        self.assertEqual(Decimal(loss["actual_yuan"]),Decimal("-85917363120.34"))

    def test_real_response_cache_roundtrip_and_decisions_do_not_change(self):
        cfg=load_config()
        now="2026-11-06T22:00:00+08:00"
        base=statement_tables(fetched=now)
        def decisions(tables):
            analysis=analyze_financials(tables,"制造业",now)
            q=score_quality({"roe":20.,"ocf_to_profit":1.,"profit_growth_std":.1,"industry":"制造业","financial_analysis":analysis},cfg)
            s=stock();s.parts["quality"]=q
            return (q.score,q.detail["sub_scores"],analysis["metrics"],evaluate_buy(s,cfg,50),evaluate_sell(s,cfg,50),decide(s,cfg,50))
        baseline=decisions(base)
        for meta in MANIFEST["attempts"]:
            raw,meta=capture(meta["code"])
            with tempfile.TemporaryDirectory() as d, patch("touzi.data.deducted_profit.urllib.request.urlopen",return_value=io.BytesIO(raw)) as net:
                cache=Cache(d)
                frame=fetch_deducted_parent_profit(meta["code"],cache)
                self.assertEqual(net.call_count,1)
                self.assertEqual(frame.iloc[0].response_sha256,meta["sha256"])
                with patch("touzi.data.deducted_profit.urllib.request.urlopen",side_effect=AssertionError("cache must hit")):
                    cached=fetch_deducted_parent_profit(meta["code"],cache)
                self.assertEqual(cached.attrs["deducted_source"]["cache_status"],"hit")
                tables=copy.deepcopy(base);tables["deducted_parent"]=cached
                self.assertEqual(decisions(tables),baseline)


if __name__ == "__main__":
    unittest.main()
