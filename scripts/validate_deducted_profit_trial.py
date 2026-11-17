"""Offline-only real-response validation. Never fetches data or runs scoring.

Exit 0 means the inspected benchmark amounts match, NOT production approval.
Exit 2 means missing/invalid amounts or a known/unknown version discrepancy.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from touzi.data.deducted_profit import MAX_BYTES, normalize_deducted_reports

STOCKS = {"600519", "603195", "000002", "600036"}
YEARS = set(range(2021, 2026))


def reject_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate_json_key:{key}")
        obj[key] = value
    return obj


def validate_capture(raw, capture, benchmarks):
    """Use production parser but verify bytes and amount versions separately."""
    code = capture["code"]
    if code not in STOCKS or len(raw) > MAX_BYTES:
        raise ValueError("capture_scope_or_size_invalid")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != capture["sha256"]:
        raise ValueError("response_hash_mismatch")
    payload = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    reports = payload["result"]["data"]["report_list"]
    frame = normalize_deducted_reports(code, reports, capture["completed_at"], digest, capture["url"])
    expected = {b["year"]: b for b in benchmarks if b["code"] == code}
    if set(expected) != YEARS:
        raise ValueError("benchmark_window_incomplete")
    results = []
    for year in sorted(YEARS):
        benchmark = expected[year]
        rows = frame[frame.report_date.eq(f"{year}-12-31")]
        actual = None
        status = "year_missing"
        if len(rows) == 1:
            row = rows.iloc[0]
            status = row.value_status
            if status == "ok":
                actual = Decimal(row.amount_exact)
                reference = Decimal(benchmark["reference_yuan"])
                if actual == reference:
                    status = "matches_inspected_document"
                elif benchmark.get("known_prior_yuan") is not None and actual == Decimal(benchmark["known_prior_yuan"]):
                    status = "known_pre_adjustment_version"
                else:
                    status = "unrecognized_amount_version"
        elif len(rows) > 1:
            status = "duplicate_report"
        results.append({"code": code, "year": year,
                        "actual_yuan": format(actual, "f") if actual is not None else None,
                        "reference_yuan": benchmark["reference_yuan"],
                        "difference_yuan": format(actual - Decimal(benchmark["reference_yuan"]), "f") if actual is not None else None,
                        "validation_status": status, "document": benchmark["document"],
                        "pdf_page": benchmark["pdf_page"], "document_sha256": benchmark["document_sha256"],
                        "response_sha256": digest, "disclosure_status": "unverified",
                        "point_in_time_eligible": False})
    return results


def validate_bundle(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    benchmarks = json.loads((directory / "benchmarks.json").read_text("utf-8"))["rows"]
    captures = manifest["attempts"]
    if len(captures) != 4 or {c["code"] for c in captures} != STOCKS:
        raise ValueError("capture_stock_scope_invalid")
    results = []
    for capture in captures:
        name = capture["raw_file"]
        if Path(name).name != name:
            raise ValueError("capture_path_invalid")
        path = directory / name
        with gzip.open(path, "rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        results.extend(validate_capture(raw, capture, benchmarks))
    matched = sum(r["validation_status"] == "matches_inspected_document" for r in results)
    return {"rows": results, "expected": 20, "matched_inspected_document": matched,
            "validation_passed": matched == 20,
            "production_enable_authorized": False,
            "limitation": "A single four-stock capture cannot establish market coverage, ongoing stability or complete disclosure version chains."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "tests/fixtures/deducted_trial_20261106")
    args = parser.parse_args()
    try:
        report = validate_bundle(args.bundle)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["validation_passed"] else 2
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({"validation_passed": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
