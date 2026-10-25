"""公开动态模型：捕获权威输入，序列化时严格排除私人台账。"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from .plans import evaluate_sell
from .portfolio import load_fees
from .market_calendar import CALENDAR, market_clock, model_expiry
from .financial_coverage import financial_coverage

MODEL_VERSION = "touzi-live-v2"


def validate_export_model(model, expected_codes, now):
    try:
        return _validate_export_model(model, expected_codes, now)
    except (KeyError, TypeError, OverflowError, AttributeError) as exc:
        raise ValueError("公开动态模型结构不完整，停止发布") from exc


def _validate_export_model(model, expected_codes, now):
    """发布前校验实际产物；仅检查协议与边界，不复制评分公式。"""
    def require(ok):
        if not ok:
            raise ValueError("公开动态模型不完整、过期或超出 Worker 契约，停止发布")

    def finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def optional(value):
        return value is None or finite(value)

    def stamp(value):
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai")) if parsed.tzinfo is None else parsed

    require(model["schema_version"] == 1 and model["model_version"] == MODEL_VERSION)
    require(model["provenance"] == "python-score-one-live-inputs")
    require(len(json.dumps(model, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")) <= 12 * 1024 * 1024)
    built, expires, quote = map(stamp, (model["built_at"], model["expires_at"], model["quote_at"]))
    duration = timedelta(hours=36)
    if "trading_calendar" in model:
        require(model["trading_calendar"] == CALENDAR and market_clock(now) is not None)
        require(expires == model_expiry(built, quote) and market_clock(built)["quote_day"] == quote.date())
    else:
        require(expires <= built + duration)
    # 生产门禁不复活旧输入；休市复用仅发生在消费端。
    require(now - duration <= built <= now + timedelta(minutes=1) and now < expires and expires > built)
    require(now - duration <= quote <= min(now + timedelta(minutes=1), built + timedelta(minutes=5)))
    require(str(model["as_of"]) == quote.strftime("%Y-%m-%d"))
    require(finite(model["quote_coverage"]) and .9 <= model["quote_coverage"] <= 1)
    entries, pools, settings = model["entries"], model["pools"], model["settings"]
    require(0 < len(entries) <= 600 and set(entries) == set(expected_codes) and len(expected_codes) == len(entries))
    require(len(pools) <= 4000 and sum(map(len, pools.values())) <= 1000000)
    for values in pools.values():
        require(len(values) <= 10000 and all(finite(v) and v > 0 for v in values))
    for values, keys in ((settings["valuation"], ("pe_weight", "pb_weight", "dividend_yield_weight")),
                         (settings["dividend"], ("level_weight", "years_weight", "spread_weight")),
                         (settings["stock_weights"], ("valuation", "quality", "dividend", "policy"))):
        require(all(finite(values[k]) and values[k] >= 0 for k in keys) and sum(values[k] for k in keys) > 0)
    require(set(settings["stock_weights"]) <= {"valuation", "quality", "dividend", "policy"})
    for k in ("primary_bonus", "graham_number"):
        require(finite(settings["valuation"][k]) and settings["valuation"][k] >= 0)
    require(len(settings["valuation"]["industry_primary"]) <= 1000 and
            all(v in {"pe", "pb", "dividend_yield"} for v in settings["valuation"]["industry_primary"].values()))
    for k in ("excellent_yield", "full_credit_years", "spread_over_bond"):
        require(finite(settings["dividend"][k]) and settings["dividend"][k] >= 0)
    plan, fees = model["plan"], model["fees"]
    require(all(finite(v) and (k.startswith("sell_") or k == "reduce_ratio") for k, v in plan.items()))
    require(all(k in plan for k in ("sell_stop_loss", "sell_take_profit", "sell_pe_percentile", "sell_pb_percentile", "sell_graham", "sell_pe_abs", "sell_dividend_yield", "sell_total", "sell_reduce_total", "sell_macro_score", "sell_macro_reduce_score", "sell_cashflow_floor", "sell_negative_growth_years", "reduce_ratio")))
    require(all(0 <= plan[k] <= 1 for k in ("sell_pe_percentile", "sell_pb_percentile", "reduce_ratio")))
    require(all(0 <= plan[k] <= 100 for k in ("sell_total", "sell_reduce_total", "sell_macro_score", "sell_macro_reduce_score")))
    require(-1 <= plan["sell_stop_loss"] <= 0 and plan["sell_take_profit"] >= 0 and
            isinstance(plan["sell_negative_growth_years"], int) and plan["sell_negative_growth_years"] > 0)
    require(all(finite(fees[k]) and 0 <= fees[k] <= 1 for k in ("commission_rate", "stamp_duty_rate", "transfer_fee_rate")))
    require(finite(fees["commission_min"]) and fees["commission_min"] >= 0)
    rules = {"sell_pe_percentile": "清仓", "sell_pb_percentile": "清仓", "sell_graham": "清仓",
             "sell_pe_abs": "清仓", "sell_dividend_yield": "清仓", "sell_total": "清仓",
             "sell_reduce_total": "减仓", "sell_macro_score": "减仓", "sell_macro_reduce_score": "减仓",
             "sell_cashflow": "预警", "sell_roe_decline": "预警", "sell_growth_negative": "预警"}
    rules.update({key: "预警" for key in ("sell_debt_equity", "sell_equity_nonpositive", "sell_capex_to_profit", "sell_annual_loss", "sell_cumulative_loss")})
    for code, entry in entries.items():
        require(len(code) == 6 and code.isascii() and code.isdigit() and entry["code"] == code)
        require(all(isinstance(entry[k], str) and len(entry[k]) <= 300 for k in ("name", "industry")))
        cutoff = stamp(entry["input_as_of"])
        require(now - duration <= cutoff <= now + timedelta(minutes=1) and cutoff.strftime("%Y-%m-%d") >= model["as_of"])
        require(all(entry[k]["code"] == code for k in ("valuation_input", "dividend_input")))
        require(entry["valuation_input"]["industry"] == entry["industry"])
        for k, value in entry["valuation_input"].items():
            if "percentile" in k:
                require(optional(value) and (value is None or 0 <= value <= 1))
        require(all(optional(entry["valuation_input"][k]) for k in ("pe_ttm", "pb", "dividend_yield")))
        din = entry["dividend_input"]
        require(all(optional(din[k]) for k in ("dps_ttm", "price", "dividend_yield", "bond_yield", "dividend_times", "finance_times")))
        require((din["dps_ttm"] is None or din["dps_ttm"] >= 0) and isinstance(din["consecutive_years"], int) and din["consecutive_years"] >= 0)
        require(set(entry["fixed_parts"]) == {"quality", "policy"} and
                all(optional(v) and (v is None or 0 <= v <= 100) for v in entry["fixed_parts"].values()))
        require(all(isinstance(entry["history_live"][k], bool) for k in ("pe", "pb")))
        require(set(entry["pool_refs"]) == {k + "_" + p for k in ("pe", "pb") for p in ("history", "industry", "market")})
        require(all(ref in pools for ref in entry["pool_refs"].values()))
        checks = entry["sell_checks"]
        analysis = entry["financial_analysis"]
        require(analysis["status"] in {"ok", "partial", "missing", "stale", "invalid", "not_applicable"})
        require(analysis["currency"] == "CNY" and analysis["unit"] == "yuan" and analysis["statement_scope"] == "consolidated")
        years = analysis["window"]["years"]
        require(len(years) == 5 and years == list(range(analysis["window"]["start_year"], analysis["window"]["end_year"] + 1)))
        require(len(analysis["years"]) == 5 and [r["fiscal_year"] for r in analysis["years"]] == years)
        for metric in analysis["metrics"].values():
            require(optional(metric["value"]) and metric["status"] in {"ok", "missing", "stale", "invalid", "not_applicable"})
        require(finite(analysis["quality"]["coverage"]) and 0 <= analysis["quality"]["coverage"] <= 1)
        require(len(checks) == len(rules) and {c["key"] for c in checks} == set(rules))
        for check in checks:
            key = check["key"]
            require(check["level"] == rules[key] and isinstance(check["label"], str) and len(check["label"]) <= 500)
            require(isinstance(check["known"], bool) and isinstance(check["hit"], bool) and (check["known"] or not check["hit"]))
            if key not in {"sell_roe_decline", "sell_growth_negative"}:
                require(check["threshold"] == plan["sell_cashflow_floor" if key == "sell_cashflow" else key])
    return model


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def pool(values):
    if values is None:
        return []
    return [float(x) for x in pd.to_numeric(pd.Series(values), errors="coerce")
            if pd.notna(x) and math.isfinite(float(x)) and x > 0]


def capture_input(screener, code, industry, pe_hist, pb_hist, vin, din, parts, as_of,
                  financials=None, dividends=None):
    """只在 live 非回测入口调用；不从最终总分倒推任何输入。"""
    uni = screener.load_universe()
    sub = uni[uni["industry"] == industry] if industry and "industry" in uni else uni.iloc[:0]
    pools = {}
    def latest_date(values):
        if values is None:
            return None
        dates = pd.to_datetime(values, errors="coerce")
        dates = dates[~pd.isna(dates)]
        return str(dates.max().date()) if len(dates) else None
    for key, col, hist in (("pe", "pe_ttm", pe_hist), ("pb", "pb", pb_hist)):
        pools[key + "_history"] = pool(hist)
        pools[key + "_industry"] = pool(sub[col]) if len(sub) >= 5 else []
        pools[key + "_market"] = pool(uni[col])
    return clean({
        "code": code, "industry": industry, "valuation_input": dict(vin),
        "dividend_input": dict(din), "pools": pools,
        # 判定历史覆盖的原始非空数与正值样本数不是同一回事。
        "history_live": {k: h is not None and len(h.dropna()) > 20
                         for k, h in (("pe", pe_hist), ("pb", pb_hist))},
        "fixed_parts": {k: parts[k].score for k in ("quality", "policy")},
        "financial_analysis": parts["quality"].detail["financial_analysis"],
        "fixed_sell_inputs": {k: parts["quality"].detail.get(k) for k in
                              ("ocf_to_profit", "roe_series", "profit_growth_series")},
        "input_as_of": str(as_of),
        "provenance": {"valuation_history": "clipped-at-input-as-of",
                       "financials": "cached-source-disclosures-not-realtime",
                       "dividend": "ttm-cash-dividends-at-input-as-of",
                       "calculation_cutoff": str(as_of),
                       "pe_history_end": latest_date(pe_hist.index) if pe_hist is not None else None,
                       "pb_history_end": latest_date(pb_hist.index) if pb_hist is not None else None,
                       "financial_report_period": latest_date(financials["report_date"])
                       if financials is not None and "report_date" in financials else None,
                       "dividend_last_ex_date": latest_date(dividends["ex_date"])
                       if dividends is not None and "ex_date" in dividends else None,
                       "source_fetch_times": "not-available-per-input"},
    })


def export_model(res, cfg, now=None):
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    entries, pools = {}, {}
    macro = getattr(res.get("macro"), "macro_score", float("nan"))
    for score in res.get("stock_results") or []:
        record = getattr(score, "live_input", None)
        if not record:
            continue
        record = clean(record)
        refs = {}
        for key, values in record.pop("pools").items():
            digest = hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()[:24]
            pools[digest] = values
            refs[key] = digest
        record["pool_refs"] = refs
        record["name"] = score.name
        record["fixed_sell_inputs"]["macro_score"] = clean(macro)
        # 只导出公开规则；私人成本检查不进入共享资产。
        record["sell_checks"] = [c.as_dict() for c in evaluate_sell(score, cfg, macro_score=macro)
                                 if c.key not in {"sell_stop_loss", "sell_take_profit"}]
        entries[score.code] = record
    flags = res.get("flags") or {}
    fees = load_fees(cfg)
    quote_text = str(flags.get("quote_at") or "")
    try:
        quote_stamp = datetime.fromisoformat(quote_text).replace(tzinfo=ZoneInfo("Asia/Shanghai")) if "+" not in quote_text and not quote_text.endswith("Z") else datetime.fromisoformat(quote_text)
        expiry = model_expiry(now, quote_stamp)
    except ValueError:
        expiry = None
    model = clean({
        "schema_version": 1, "model_version": MODEL_VERSION,
        "built_at": now.isoformat(), "expires_at": (expiry or now + timedelta(hours=36)).isoformat(),
        "trading_calendar": CALENDAR,
        "as_of": str(flags.get("as_of") or ""), "quote_at": str(flags.get("quote_at") or ""),
        "quote_coverage": flags.get("quote_coverage"),
        "provenance": "python-score-one-live-inputs",
        "plan": {k: v for k, v in cfg.get("plan", {}).items()
                 if k.startswith("sell_") or k == "reduce_ratio"},
        "fees": {k: getattr(fees, k) for k in
                 ("commission_rate", "commission_min", "stamp_duty_rate", "transfer_fee_rate")},
        "settings": {"valuation": {k: cfg.valuation.get(k, 0.0) for k in
                     ("pe_weight", "pb_weight", "dividend_yield_weight", "primary_bonus", "industry_primary", "graham_number")},
                     "dividend": {k: cfg.dividend[k] for k in
                     ("excellent_yield", "full_credit_years", "spread_over_bond", "level_weight", "years_weight", "spread_weight")},
                     "stock_weights": dict(cfg.stock_weights)},
        "pools": pools, "entries": entries, "financial_coverage": financial_coverage(entries),
    })
    model["settings"]["valuation"]["industry_primary"] = {
        k: v for k, v in cfg.valuation.get("industry_primary", {}).items()
        if isinstance(v, str)}
    from .util import is_live
    if is_live(cfg):
        validate_export_model(model, [score.code for score in res.get("stock_results") or []], now)
    # allow_nan=False 同时作为发布契约的最后一道检查。
    json.dumps(model, allow_nan=False)
    return model
