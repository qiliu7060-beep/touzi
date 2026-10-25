"""公开财报覆盖纯聚合，分开报告状态、评分适用性和展示字段。"""
import math


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def financial_coverage(entries):
    result = {"total": 0, "reports": {}, "metrics": {}, "quality": {"full": 0, "partial": 0, "missing": 0},
              "fields": {"expected": 0, "deducted_parent_missing": 0}, "reasons": {"industry_unknown": 0, "amounts_missing": 0}}
    for entry in entries.values():
        result["total"] += 1
        a = entry.get("financial_analysis") or {}
        state = a.get("status") or "missing"
        result["reports"][state] = result["reports"].get(state, 0) + 1
        for key in ("debt_equity", "capex_to_profit", "annual_net_profit", "cumulative_net_profit"):
            state = ((a.get("metrics") or {}).get(key) or {}).get("status") or "missing"
            bucket = result["metrics"].setdefault(key, {})
            bucket[state] = bucket.get(state, 0) + 1
        coverage = (a.get("quality") or {}).get("coverage")
        result["quality"]["full" if coverage == 1 else "partial" if finite(coverage) and coverage > 0 else "missing"] += 1
        result["fields"]["expected"] += 5
        years = a.get("years") or []
        result["fields"]["deducted_parent_missing"] += 5 - sum(finite(row.get("deducted_parent_net_profit")) for row in years[:5])
        if not str(entry.get("industry") or "").strip():
            result["reasons"]["industry_unknown"] += 1
        if len(years) != 5 or any(not finite(row.get(key)) for row in years for key in ("revenue", "operating_profit", "net_profit", "operating_cash_flow", "capex")):
            result["reasons"]["amounts_missing"] += 1
    return result
