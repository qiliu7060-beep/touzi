"""版本化宏观公开结果：严格校验、内容身份与旧客户端投影。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
import struct

from .market_calendar import CALENDAR, TZ, trading_day, adjacent_day

MAX_CONTEXT_BYTES = 65536
MACRO_KEYS = ("pmi", "ppi", "fed", "cpi", "gdp")
INPUT_SOURCES = {
    "pmi_manufacturing": ("pmi", "sina:cn_pmi_sina", "monthly"),
    "pmi_non_manufacturing": ("pmi", "sina:cn_pmi_sina", "monthly"),
    "cpi": ("cpi", "sina:cn_cpi_sina", "monthly"),
    "ppi": ("ppi", "sina:cn_ppi_sina", "monthly"),
    "gdp": ("gdp", "sina:cn_gdp_sina", "quarterly"),
    "cn_bond_10y": ("fed", "sina:cn_bond_10y", "daily"),
    "policy_rate": ("fed", "yahoo:^IRX", "daily"),
    "us_10y": ("fed", "yahoo:^TNX", "daily"),
    "dollar": ("fed", "yahoo:DX-Y.NYB", "daily"),
    "balance_sheet": ("fed", "fred:WALCL", "weekly"),
}
REQUIRED_INPUTS = {"pmi_manufacturing", "cpi", "ppi", "gdp", "cn_bond_10y"}
FED_INPUTS = {"policy_rate", "us_10y", "dollar", "balance_sheet"}
INPUT_FIELDS = {"module", "source", "frequency", "observation_date", "value", "fetched_at", "fetch_status", "release_at", "source_updated_at", "availability_policy"}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= 2 ** 53


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("宏观时间缺失")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("宏观时间缺少时区")
    return result.astimezone(TZ)


def canonical_bytes(value):
    """数值叶子用binary64表示，避免Python/JS小数和指数编码差异。"""
    def encode(node):
        if node is None or type(node) in (str, bool):
            return node
        if type(node) in (int, float):
            if not _finite(node):
                raise ValueError("宏观数值非法")
            number = 0.0 if node == 0 else float(node)
            return {"$number": struct.pack(">d", number).hex()}
        if isinstance(node, list):
            return [encode(item) for item in node]
        if isinstance(node, dict) and all(isinstance(key, str) for key in node):
            return {key: encode(item) for key, item in node.items()}
        raise ValueError("宏观内容包含不支持的类型")
    return json.dumps(encode(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")


def snapshot_id(context):
    return hashlib.sha256(canonical_bytes({key: value for key, value in context.items() if key != "snapshot_id"})).hexdigest()


def project_v1(context):
    """同一计算结果的精确v1投影，不再次评分。"""
    result = {key: deepcopy(context[key]) for key in ("rule_version", "as_of", "built_at", "trading_calendar", "bond", "errors")}
    result.update(schema_version=1, kind="touzi-investment-v1", macro={key: context["macro"][key] for key in ("score", "position", "computed_at", "status")})
    return result


def validate_v1(raw, now=None, freshness=True):
    now = now or datetime.now(TZ)
    if len(raw) > MAX_CONTEXT_BYTES:
        raise ValueError("投资环境大小超限")
    value = json.loads(raw)
    allowed = {"schema_version", "kind", "rule_version", "as_of", "built_at", "trading_calendar", "macro", "bond", "errors"}
    if not isinstance(value, dict) or set(value) != allowed or value["schema_version"] != 1 or value["kind"] != "touzi-investment-v1" or value["rule_version"] != "yield-trend-v1":
        raise ValueError("投资环境格式错误")
    if value["trading_calendar"] != CALENDAR or value["errors"] != []:
        raise ValueError("日历未核验或生产者报告错误")
    macro, bond = value["macro"], value["bond"]
    if not isinstance(macro, dict) or set(macro) != {"score", "position", "computed_at", "status"} or macro["status"] != "ok":
        raise ValueError("宏观环境未核验")
    if not isinstance(bond, dict) or set(bond) != {"yield_pct", "observation_date", "fetched_at", "delta20_bp", "delta60_bp", "status", "factor", "reasons"} or bond["status"] != "ok":
        raise ValueError("国债环境未核验")
    def finite(number):
        return type(number) in (int, float) and math.isfinite(number)
    if not all(finite(x) for x in (macro["score"], macro["position"], bond["yield_pct"], bond["delta20_bp"], bond["delta60_bp"], bond["factor"])) or not 0 <= macro["score"] <= 100 or not 0 <= macro["position"] <= 1 or not 0 <= bond["yield_pct"] <= 100 or bond["factor"] not in (0, .5, .75, 1):
        raise ValueError("环境数值越界")
    if not isinstance(bond["reasons"], list) or not bond["reasons"] or not all(isinstance(x, str) and len(x) <= 500 for x in bond["reasons"]):
        raise ValueError("国债判断原因缺失")
    built, computed, fetched = map(_stamp, (value["built_at"], macro["computed_at"], bond["fetched_at"]))
    observed = datetime.strptime(bond["observation_date"], "%Y-%m-%d").date()
    as_of = datetime.strptime(value["as_of"], "%Y-%m-%d").date()
    if as_of != built.date() or max(built, computed, fetched) > now + timedelta(minutes=1) or computed > built or fetched > built or observed > fetched.date() or trading_day(observed) is not True:
        raise ValueError("环境时间不可信")
    if freshness:
        latest = now.date() if trading_day(now.date()) is True and now.hour >= 15 else adjacent_day(now.date(), -1)
        if trading_day(now.date()) is None or latest is None:
            raise ValueError("当前交易日历未知")
        oldest = adjacent_day(adjacent_day(latest, -1), -1)
        if oldest is None or min(computed.date(), fetched.date(), observed) < oldest:
            raise ValueError("投资环境过期，拒绝晋升")
    return value


def validate_context(raw, now=None, freshness=True):
    now = now or datetime.now(TZ)
    if isinstance(raw, dict):
        value = raw
        raw = json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8")
    else:
        value = json.loads(raw)
    if len(raw) > MAX_CONTEXT_BYTES:
        raise ValueError("宏观完整结果大小超限")
    fields = {"schema_version", "kind", "formula_version", "input_policy", "snapshot_id", "rule_version", "as_of", "built_at", "trading_calendar", "macro", "bond", "errors"}
    if not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int or value["schema_version"] != 2 or value["kind"] != "touzi-investment-v2" or value["formula_version"] != "touzi-macro-v1" or value["input_policy"] != "upstream-observed-live":
        raise ValueError("宏观完整结果格式错误")
    macro = value["macro"]
    if not isinstance(macro, dict) or set(macro) != {"score", "position", "computed_at", "status", "regime", "weights", "rules", "inputs", "freshness"} or macro["freshness"] != "source-checked":
        raise ValueError("宏观模块格式或输入状态错误")
    if not isinstance(macro["regime"], str) or not 1 <= len(macro["regime"]) <= 100:
        raise ValueError("宏观环境说明缺失")
    validate_v1(json.dumps(project_v1(value), ensure_ascii=False, allow_nan=False).encode("utf-8"), now, freshness=freshness)
    weights, rules, inputs = macro["weights"], macro["rules"], macro["inputs"]
    if not isinstance(weights, dict) or set(weights) != set(MACRO_KEYS) or not all(_finite(w) and 0 < w <= 1 for w in weights.values()) or abs(sum(weights.values()) - 1) > 1e-8:
        raise ValueError("宏观权重不完整")
    if not isinstance(rules, dict) or set(rules) != set(MACRO_KEYS):
        raise ValueError("五个宏观分项不完整")
    for rule in rules.values():
        if not isinstance(rule, dict) or set(rule) != {"rule", "score", "state", "explain"} or not _finite(rule["score"]) or not 0 <= rule["score"] <= 100 or not all(isinstance(rule[key], str) and 1 <= len(rule[key]) <= (5000 if key == "explain" else 100) for key in ("rule", "state", "explain")):
            raise ValueError("宏观分项缺失或非法")
    if not _finite(macro["score"]) or abs(sum(weights[key] * rules[key]["score"] for key in MACRO_KEYS) - macro["score"]) > .011:
        raise ValueError("宏观总分与分项不一致")
    if not isinstance(inputs, dict) or not REQUIRED_INPUTS <= set(inputs) <= set(INPUT_SOURCES) or not FED_INPUTS.intersection(inputs):
        raise ValueError("宏观输入证据不完整")
    computed = _stamp(macro["computed_at"])
    built = _stamp(value["built_at"])
    for key, item in inputs.items():
        if not isinstance(item, dict) or set(item) != INPUT_FIELDS:
            raise ValueError("宏观输入字段不完整")
        module, source, frequency = INPUT_SOURCES[key]
        if (item["module"], item["source"], item["frequency"]) != (module, source, frequency) or item["fetch_status"] != "network" or item["availability_policy"] != "upstream-observed-live" or not _finite(item["value"]):
            raise ValueError("宏观输入来源未核验")
        fetched = _stamp(item["fetched_at"])
        if not isinstance(item["observation_date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["observation_date"]):
            raise ValueError("宏观观测日期错误")
        observed = datetime.strptime(item["observation_date"], "%Y-%m-%d").date()
        if observed > fetched.date() or fetched > computed or computed > built or built > now + timedelta(minutes=1):
            raise ValueError("宏观输入时间不可信")
        for optional in ("release_at", "source_updated_at"):
            if item[optional] is not None and _stamp(item[optional]) > fetched:
                raise ValueError("宏观源更新时间不可信")
        # 新计算不能复活旧抓取；月度观察期不套用日频交易日门槛。
        if freshness:
            latest = now.date() if trading_day(now.date()) is True and now.hour >= 15 else adjacent_day(now.date(), -1)
            oldest = adjacent_day(adjacent_day(latest, -1), -1) if latest is not None else None
            if oldest is None or fetched.date() < oldest:
                raise ValueError("宏观输入抓取证据过期")
    bond_input = inputs["cn_bond_10y"]
    if value["bond"]["fetched_at"] != bond_input["fetched_at"] or value["bond"]["observation_date"] != bond_input["observation_date"] or value["bond"]["yield_pct"] != bond_input["value"]:
        raise ValueError("国债环境与宏观输入不是同一版本")
    if not isinstance(value["snapshot_id"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["snapshot_id"]) or value["snapshot_id"] != snapshot_id(value):
        raise ValueError("宏观完整结果摘要不一致")
    return value


def input_rollback(new, old):
    """观察期和真实获取时间分别防倒退，同期修订允许推进。"""
    for key, previous in old["macro"]["inputs"].items():
        item = new["macro"]["inputs"].get(key)
        if item is not None and (item["observation_date"] < previous["observation_date"] or _stamp(item["fetched_at"]) < _stamp(previous["fetched_at"])):
            return True
    return False
