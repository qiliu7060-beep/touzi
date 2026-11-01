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


def quality_evidence(entry):
    """只读解释已发布分数的证据；不把行业豁免算成数据丢失。"""
    analysis = entry.get("financial_analysis") or {}
    quality = analysis.get("quality") or {}
    weights, scores = quality.get("weights") or {}, quality.get("sub_scores") or {}
    keys = ("roe", "cashflow", "debt", "stability", "capex")
    covered = sum(weights[k] for k in keys if finite(scores.get(k)) and finite(weights.get(k)))
    configured = sum(weights[k] for k in keys if finite(weights.get(k)))
    result = {"coverage": covered / configured if configured > 0 else None,
              "effective_weights": {}, "statuses": {}, "pending": [], "exempt": []}
    for key in keys:
        metric = (analysis.get("metrics") or {}).get({"debt": "debt_equity", "capex": "capex_to_profit"}.get(key, "")) or {}
        state = "scored" if finite(scores.get(key)) else metric.get("status") or "missing"
        result["statuses"][key] = state
        result["effective_weights"][key] = weights.get(key) / covered if finite(scores.get(key)) and covered > 0 else None
        if state == "not_applicable":
            result["exempt"].append(key)
        elif state != "scored":
            result["pending"].append(key)
    return result
