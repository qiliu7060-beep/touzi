"""固定收盘评分的公开、分片快照；不包含持仓或历史估值样本池。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re

from .financial_coverage import financial_coverage
from .market_calendar import CALENDAR, TZ, adjacent_day, trading_day
from .plans import decide, plan_settings
from .portfolio import load_fees

MAX_CODES = 10000
MAX_PART_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
CODE = re.compile(r"\d{6}\Z")


def clean(value):
    """在序列化边界保留缺失语义，拒绝将非有限数写入JSON。"""
    if value is None or type(value).__name__ in ("NAType", "NaTType"):
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    # 分红汇总按代码缓存的实际行可能是Series，不能当单元素标量调用item()。
    if callable(getattr(value, "to_dict", None)):
        return clean(value.to_dict())
    if callable(getattr(value, "tolist", None)):
        return clean(value.tolist())
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def json_bytes(value):
    return json.dumps(clean(value), ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def close_target(now: datetime) -> date | None:
    """最近已完成的交易日收盘，盘中不把昨天的分数判成失效。"""
    now = now.astimezone(TZ)
    state = trading_day(now.date())
    if state is None:
        return None
    if state and (now.hour, now.minute) >= (15, 0):
        return now.date()
    return adjacent_day(now.date(), -1)


def validate_close_time(score_date, quote_at, built_at, now=None):
    day = date.fromisoformat(score_date)
    quote, built = datetime.fromisoformat(quote_at), datetime.fromisoformat(built_at)
    if quote.tzinfo is None or built.tzinfo is None:
        raise ValueError("收盘时间必须注明时区")
    quote, built = quote.astimezone(TZ), built.astimezone(TZ)
    if trading_day(day) is not True or quote.date() != day or not (15 <= quote.hour < 18):
        raise ValueError("不是经核验交易日的完整收盘行情")
    if built < quote or (now and built > now.astimezone(TZ) + timedelta(minutes=1)):
        raise ValueError("收盘生成时间不可信")
    return day


def public_entry(score, cfg, macro_score, quote_at, rank_pct=None):
    """只从权威StockScore/PlanDecision构造公开股票记录，杜绝私有台账混入。"""
    decision = decide(score, cfg, macro_score=macro_score, rank_pct=rank_pct)
    parts = getattr(score, "parts", {}) or {}
    quality = parts.get("quality")
    qdetail = getattr(quality, "detail", {}) or {}
    analysis = qdetail.get("financial_analysis")
    checks = [c.as_dict() for c in decision.sell_checks
              if c.key not in {"sell_stop_loss", "sell_take_profit"}]
    snapshot = {key: [c["label"] for c in checks if c["known"] and c["hit"] and c["level"] == level]
                for key, level in (("exit", "清仓"), ("reduce", "减仓"), ("warn", "预警"))}
    snapshot.update(missing=[c["label"] for c in checks if not c["known"] and c["level"] != "预警"],
                    missing_warn=[c["label"] for c in checks if not c["known"] and c["level"] == "预警"])
    # 静态持仓规则仍使用原始个股历史分位标签，新的全池导出不能丢掉旧协议字段。
    snapshot["percentile_labels"] = {c["key"]: c["label"] for c in checks
                                     if c["key"] in {"sell_pe_percentile", "sell_pb_percentile"}}
    financial_keys = {"sell_debt_equity", "sell_equity_nonpositive", "sell_capex_to_profit",
                      "sell_annual_loss", "sell_cumulative_loss"}
    metric_keys = {"sell_equity_nonpositive": "debt_equity", "sell_debt_equity": "debt_equity",
                   "sell_capex_to_profit": "capex_to_profit", "sell_annual_loss": "annual_net_profit",
                   "sell_cumulative_loss": "cumulative_net_profit"}
    metrics = (analysis or {}).get("metrics", {})
    snapshot["financial_warn"] = [c["label"] for c in checks if c["key"] in financial_keys and c["known"] and c["hit"]]
    snapshot["financial_missing"] = [c["label"] for c in checks if c["key"] in financial_keys and not c["known"]
                                      and metrics.get(metric_keys[c["key"]], {}).get("status") != "not_applicable"]
    total = score.total
    grade = "N/A" if not math.isfinite(float(total)) else "A+" if total >= 80 else "A" if total >= 72 else "B" if total >= 62 else "C" if total >= 50 else "D"
    result = {"code": score.code, "name": score.name, "industry": getattr(score, "industry", ""),
              "total": total, "grade": getattr(score, "grade", grade), "price": score.price,
              "pe": score.pe_ttm, "pb": score.pb, "dy": score.dividend_yield,
              "quote_ts": quote_at, "action": decision.action,
              "buy_checks": [c.as_dict() for c in decision.buy_checks],
              "sell_checks": checks, "sell_snapshot": snapshot,
              "financial_analysis": analysis, "financial_capability": analysis is not None,
              "quality_state": getattr(quality, "state", ""),
              "valuation_detail": getattr(parts.get("valuation"), "detail", {}) or {},
              "failed": "；".join(c.label for c in decision.failed_checks),
              "warn": "；".join(snapshot["warn"]),
              "triggers": "；".join(snapshot["exit"] + snapshot["reduce"])}
    for key in ("valuation", "quality", "dividend", "policy"):
        result[key] = getattr(parts.get(key), "score", None)
    return clean(result)


def validate_counts(counts, entry_count):
    names = ("market_total", "eligible_total", "attempted", "scored", "failed", "excluded")
    if any(type(counts.get(k)) is not int or counts[k] < 0 for k in names):
        raise ValueError("市场覆盖数量无效")
    if (counts["market_total"] != counts["eligible_total"] + counts["excluded"]
            or counts["attempted"] != counts["scored"] + counts["failed"]
            or counts["attempted"] != counts["eligible_total"]
            or counts["scored"] != entry_count or not 0 < entry_count <= MAX_CODES):
        raise ValueError("全市场覆盖数量不完整或不一致")


def export_snapshot(entries, cfg, score_date, quote_at, counts, built_at=None):
    """构造完整manifest和不可变分片；调用方全部验证成功后再原子发布。"""
    built_at = built_at or datetime.now(TZ).isoformat()
    validate_close_time(score_date, quote_at, built_at)
    validate_counts(counts, len(entries))
    for code, entry in entries.items():
        if not CODE.fullmatch(code) or entry.get("code") != code:
            raise ValueError("收盘条目身份或报价时间不一致")
        validate_close_time(score_date, entry.get("quote_ts", ""), built_at)
        if datetime.fromisoformat(entry["quote_ts"]) > datetime.fromisoformat(quote_at):
            raise ValueError("条目报价不能晚于清单报价")
        total = entry.get("total")
        if type(total) not in (float, int) or not math.isfinite(total) or not 0 <= total <= 100:
            raise ValueError("评分缺失或无效，不能计入成功覆盖")
    order = sorted(entries, key=lambda c: (-entries[c]["total"], c))
    files, shards, index = {}, [], []
    for offset in range(0, len(order), 100):
        codes = order[offset:offset + 100]
        part = {"schema_version": 1, "kind": "touzi-close-part-v1", "score_date": score_date,
                "entries": {code: entries[code] for code in codes}}
        data = json_bytes(part)
        if len(data) > MAX_PART_BYTES:
            raise ValueError("收盘分片超过4MiB，不发布")
        digest = hashlib.sha256(data).hexdigest()
        path = f"close-score-parts/{score_date}-{digest[:16]}-{offset // 100:03d}.json"
        files[path] = data
        shards.append({"path": path, "sha256": digest, "codes": codes})
        index.extend({"code": c, "name": entries[c]["name"], "action": entries[c]["action"],
                      "total": entries[c]["total"], "shard": path} for c in codes)
    fee = load_fees(cfg)
    manifest = {"schema_version": 1, "kind": "touzi-close-v1", "score_date": score_date,
                "quote_at": quote_at, "built_at": built_at, "trading_calendar": CALENDAR,
                "universe": dict(counts), "plan": {k: v for k, v in plan_settings(cfg).items()
                    if (k.startswith("buy_") or k.startswith("sell_") or k == "reduce_ratio")
                    and type(v) in (int, float)},
                "fees": {k: getattr(fee, k) for k in ("commission_rate", "commission_min", "stamp_duty_rate", "transfer_fee_rate")},
                "financial_coverage": financial_coverage(entries), "index": index, "shards": shards}
    encoded = json_bytes(manifest)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("收盘清单超过2MiB，不发布")
    files["close-scores.json"] = encoded
    return manifest, files


def write_snapshot(folder: Path, files):
    """本地先写不可变分片，最后切换清单；失败不会破坏上一版清单。"""
    folder.mkdir(parents=True, exist_ok=True)
    for name in sorted(files, key=lambda name: name == "close-scores.json"):
        destination = folder / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = files[name]
        if destination.exists() and destination.read_bytes() == data:
            continue
        tmp = destination.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(destination)
