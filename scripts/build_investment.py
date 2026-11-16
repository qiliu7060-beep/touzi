"""独立检查宏观来源，生成同一版本的完整环境与v1兼容投影。"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "vendor"), str(ROOT)]

import pandas as pd
from touzi.config import load_config
from touzi.util import Cache
from touzi.regime import build_macro_context
from touzi.investment import bond_signal
from touzi.market_calendar import TZ, CALENDAR
from touzi.macro_context import INPUT_SOURCES, snapshot_id, project_v1, validate_context


def _input(key, series, metadata):
    if metadata.get("cache_status") != "refreshed" or metadata.get("source_status") != "ok" or metadata.get("time_status") != "ok":
        raise ValueError(f"宏观输入{key}未成功检查来源，不晋升旧缓存")
    fetched_at = metadata.get("fetched_at")
    if not fetched_at or datetime.fromisoformat(fetched_at).tzinfo is None:
        raise ValueError(f"宏观输入{key}缺少真实获取时间")
    if isinstance(series, dict):
        observed, value = series.get("observation_date"), series.get("value")
    else:
        valid = pd.to_numeric(series, errors="coerce").dropna()
        if valid.empty:
            raise ValueError(f"宏观输入{key}缺少实际值")
        observed, value = str(valid.index[-1].date()), float(valid.iloc[-1])
    module, source, frequency = INPUT_SOURCES[key]
    return {"module": module, "source": source, "frequency": frequency,
            "observation_date": observed, "value": value, "fetched_at": fetched_at,
            "fetch_status": "network", "release_at": None, "source_updated_at": None,
            "availability_policy": "upstream-observed-live"}


def build(now=None):
    fixed_now = now
    clock = lambda: fixed_now if fixed_now is not None else datetime.now(TZ)
    started = clock().astimezone(TZ)
    cfg = load_config()
    cache = Cache(cfg.resolve(cfg.general["cache_dir"]))
    # 仅独立当前宏观路径选择源已返回的实际值，历史与个股默认语义不变。
    ctx = build_macro_context(cfg, cache=cache, as_of=started.date(), input_policy="upstream-observed-live")
    macro = ctx.as_dict()
    if macro["unavailable"] or macro["position"] is None or macro["macro_score"] is None:
        raise ValueError("宏观必要模块缺失，不发布假成功")
    raw, evidence = ctx.current_series, ctx.input_evidence
    inputs = {"pmi_manufacturing": _input("pmi_manufacturing", raw["pmi"]["manufacturing"], evidence.get("pmi", {}))}
    if cfg.pmi.get("include_non_manufacturing", False) and "non_manufacturing" in raw["pmi"].columns:
        if len(raw["pmi"]["non_manufacturing"].dropna()):
            inputs["pmi_non_manufacturing"] = _input("pmi_non_manufacturing", raw["pmi"]["non_manufacturing"], evidence.get("pmi", {}))
    for key in ("cpi", "ppi", "gdp"):
        inputs[key] = _input(key, raw[key], evidence.get(key, {}))
    inputs["cn_bond_10y"] = _input("cn_bond_10y", raw["bond_10y"], evidence.get("cn_bond_10y", {}))
    fed_subs = ctx.results["fed"].detail.get("sub_scores", {})
    needed_fed = {"policy_rate": "policy_rate" in fed_subs,
                  "us_10y": "long_rate" in fed_subs or "rate_spread" in fed_subs,
                  "dollar": "dollar" in fed_subs, "balance_sheet": "balance_sheet" in fed_subs}
    for key, used in needed_fed.items():
        if used:
            actual = evidence.get("fed", {}).get(key, {})
            inputs[key] = _input(key, actual, actual)
    # 请求完成后才记计算时间，真实获取时间不得晚于本轮计算/构建时间。
    computed = clock().astimezone(TZ)
    bond = bond_signal(raw["bond_10y"], computed, cfg.investment, fetched_at=inputs["cn_bond_10y"]["fetched_at"])
    result = {"schema_version": 2, "kind": "touzi-investment-v2", "formula_version": "touzi-macro-v1",
              "input_policy": "upstream-observed-live", "snapshot_id": "", "rule_version": "yield-trend-v1",
              "as_of": str(computed.date()), "built_at": computed.isoformat(), "trading_calendar": CALENDAR,
              "macro": {"score": macro["macro_score"], "position": macro["position"], "computed_at": computed.isoformat(), "status": "ok",
                        "regime": ctx.regime, "weights": ctx.weights,
                        "rules": {key: {"rule": rule.name, "score": float(rule.score), "state": rule.state, "explain": rule.explain} for key, rule in ctx.results.items()},
                        "inputs": inputs, "freshness": "source-checked"},
              "bond": bond, "errors": []}
    result["snapshot_id"] = snapshot_id(result)
    validate_context(result, computed)
    return result


def write_contexts(context, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    complete = target.with_name("investment-context-v2.json")
    # 文件只是本轮暂存；正式晋升由publish脚本一次CAS提交两个资产。
    for path, value in ((complete, context), (target, project_v1(context))):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
    return target, complete


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".investment-output/investment-context.json")
    args = parser.parse_args()
    write_contexts(build(), args.output)


if __name__ == "__main__":
    main()
