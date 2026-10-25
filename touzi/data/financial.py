"""公开合并财报：有界读取新浪三表，年度金额统一为人民币元。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import math
import urllib.request
from zoneinfo import ZoneInfo

import pandas as pd

from ..util import LOG, cached_fetch
from .stock import plain_code, to_tx_symbol

SOURCE = "sina-finance-report"
BASE = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
AMOUNTS = {
    "lrb": {"revenue": ("BIZINCO",), "operating_profit": ("PERPROFIT", "OPERPROFIT"), "net_profit": ("NETPROFIT",)},
    "llb": {"operating_cash_flow": ("MANANETR",), "capex": ("ACQUASSETCASH",)},
    "fzb": {"total_liabilities": ("TOTLIAB",), "total_equity": ("RIGHAGGR", "TOTSHAREQUI")},
}
FIELDS = tuple(k for fields in AMOUNTS.values() for k in fields) + ("deducted_parent_net_profit",)


def number(value):
    try:
        v = float(value)
        return v if math.isfinite(v) and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def normalize_reports(code, source, reports, fetched_at):
    """只接受经核验的合并人民币字段；不从归母权益或季度推算年度。"""
    rows = []
    for raw_date, report in reports.items():
        date = pd.to_datetime(raw_date, format="%Y%m%d", errors="coerce")
        if pd.isna(date):
            continue
        if report.get("rCurrency") != "CNY" or report.get("rType") != "合并期末":
            raise ValueError(f"{code} {source} 财报不是合并人民币口径")
        values = {i.get("item_field"): i.get("item_value") for i in report.get("data", [])}
        row = {"code": plain_code(code), "report_date": str(date.date()), "fiscal_year": date.year,
               "source": SOURCE, "statement_scope": "consolidated", "currency": "CNY", "unit": "yuan",
               "fetched_at": fetched_at, "source_updated_at": None}
        updated = number(report.get("update_time"))
        if updated is not None:
            row["source_updated_at"] = datetime.fromtimestamp(updated, ZoneInfo("Asia/Shanghai")).isoformat()
        for field, aliases in AMOUNTS[source].items():
            row[field] = next((number(values[k]) for k in aliases if number(values.get(k)) is not None), None)
        rows.append(row)
    if not rows:
        raise ValueError(f"{code} {source} 无有效财报")
    return pd.DataFrame(rows)


def fetch_financial_statements(code, cache, ttl_days=1):
    """分别缓存三表；失败旧缓存保留真实获取时间，由纯分析明确降级。"""
    def fetch(source):
        def read():
            url = f"{BASE}?paperCode={to_tx_symbol(code)}&source={source}&type=0&page=1&num=1000"
            with urllib.request.urlopen(url, timeout=8) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError("公开财报响应超出上限")
            data = json.loads(raw)["result"]["data"]["report_list"]
            return normalize_reports(code, source, data, datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
        try:
            return source, cached_fetch(cache, f"statements_v1_{plain_code(code)}_{source}", read,
                                        ttl_days=ttl_days, retries=1, sleep=.2)
        except Exception as exc:
            LOG.warning("%s %s 财报获取失败: %s", code, source, type(exc).__name__)
            return source, pd.DataFrame()
    with ThreadPoolExecutor(max_workers=3) as pool:
        return dict(pool.map(fetch, AMOUNTS))


def analyze_financials(tables, industry="", as_of=None, ttl_days=1):
    """计算连续五个应披露完整年度；日期截止不因缺年退回更旧窗口。"""
    now = pd.Timestamp(as_of or datetime.now(ZoneInfo("Asia/Shanghai")))
    now = now.tz_localize("Asia/Shanghai") if now.tzinfo is None else now.tz_convert("Asia/Shanghai")
    end = now.year - (1 if (now.month, now.day) > (4, 30) else 2)
    window = list(range(end - 4, end + 1))
    frames = {}
    fetched = []
    updated = []
    for source in AMOUNTS:
        frame = (tables or {}).get(source)
        if frame is None or frame.empty:
            frames[source] = {}
            continue
        f = frame.copy()
        f["report_date"] = pd.to_datetime(f["report_date"], errors="coerce")
        f = f[f["report_date"].le(now.tz_localize(None))]
        f = f.sort_values(["report_date", "source_updated_at"], na_position="first").drop_duplicates("report_date", keep="last")
        frames[source] = {str(row["report_date"].date()): row.to_dict() for _, row in f.iterrows()}
        fetched.extend(f["fetched_at"].dropna().tolist())
        updated.extend(f["source_updated_at"].dropna().tolist())
    def fresh(row):
        if not row:
            return False
        try:
            stamp = pd.Timestamp(row["fetched_at"])
            return stamp.tzinfo is not None and now - pd.Timedelta(days=ttl_days) <= stamp <= now + pd.Timedelta(minutes=1)
        except (KeyError, TypeError, ValueError):
            return False
    years = []
    for year in window:
        date = f"{year}-12-31"
        row = {"fiscal_year": year, "report_date": date, **{k: None for k in FIELDS},
               "source_updated_at": None, "net_margin": None, "capex_to_profit": None}
        stamps = []
        for source, mapping in AMOUNTS.items():
            report = frames[source].get(date, {})
            row.update({field: number(report.get(field)) for field in mapping})
            if report.get("source_updated_at"):
                stamps.append(report["source_updated_at"])
        row["source_updated_at"] = max(stamps) if stamps else None
        if row["net_profit"] is not None and row["revenue"] is not None and row["revenue"] > 0:
            row["net_margin"] = row["net_profit"] / row["revenue"]
        if row["net_profit"] is not None and row["net_profit"] > 0 and row["capex"] is not None and row["capex"] >= 0:
            row["capex_to_profit"] = row["capex"] / row["net_profit"]
        years.append(row)
    latest = frames["fzb"][max(frames["fzb"])] if frames["fzb"] else {}
    equity, liabilities = number(latest.get("total_equity")), number(latest.get("total_liabilities"))
    # 新浪49板块使用“金融行业”，不能只匹配申万式细分行业名。
    financial = any(k in industry for k in ("银行", "保险", "证券", "金融"))
    debt_exempt = financial or "房地产" in industry
    debt_status = "missing" if equity is None or liabilities is None else "ok"
    if debt_status == "ok" and (equity <= 0 or liabilities < 0):
        debt_status = "invalid"
    if latest and not fresh(latest):
        debt_status = "stale"
    if latest and str(pd.Timestamp(latest["report_date"]).date()) < f"{end}-12-31":
        debt_status = "stale"
    if debt_exempt:
        debt_status = "not_applicable"
    ratio = liabilities / equity if equity is not None and equity > 0 and liabilities is not None and liabilities >= 0 else None
    complete_profit = all(y["net_profit"] is not None for y in years)
    complete_capex = all(y["capex"] is not None and y["capex"] >= 0 for y in years)
    profit_sum = sum(y["net_profit"] for y in years) if complete_profit else None
    capex_sum = sum(y["capex"] for y in years) if complete_capex else None
    profit_fresh = all(fresh(frames["lrb"].get(f"{year}-12-31")) for year in window)
    capex_fresh = all(fresh(frames["llb"].get(f"{year}-12-31")) for year in window)
    cumulative_status = "missing" if not complete_profit else "ok" if profit_fresh else "stale"
    capex_status = "missing" if not complete_profit or not complete_capex else "invalid" if profit_sum <= 0 else "ok"
    if complete_profit and complete_capex and not (profit_fresh and capex_fresh):
        capex_status = "stale"
    if financial:
        capex_status = "not_applicable"
    if not industry.strip():
        # 缺少行业时无法确认通用阈值适用性，保留金额和比率供核对。
        debt_status = "missing"
        capex_status = "missing"
    capex_ratio = capex_sum / profit_sum if capex_sum is not None and profit_sum is not None and profit_sum > 0 else None
    annual = frames["lrb"].get(f"{end}-12-31", {})
    annual_profit = number(annual.get("net_profit"))
    annual_status = "missing" if annual_profit is None else "ok" if fresh(annual) else "stale"
    statuses = [debt_status, capex_status, annual_status, cumulative_status]
    status = "missing" if not any(frames.values()) else "stale" if "stale" in statuses else "ok" if all(s in ("ok", "not_applicable", "invalid") for s in statuses) else "partial"
    notes = ["低资本开支只是财务竞争力线索，不证明持续竞争优势。", "当前财报适配器未覆盖可靠的扣非归母净利润字段，显示缺失；不以归母或合并净利润替代。"]
    if debt_exempt:
        notes.append("行业债务项不适用，不参与计分或预警。")
    if financial:
        notes.append("金融行业资本开支项不适用，不参与计分或预警。")
    if not industry.strip():
        notes.append("行业归属缺失，债务和资本开支通用标准适用性待核验，暂不参与计分或预警；金额保留供核对。")
    if not complete_profit or not complete_capex:
        notes.append("五年净利润或资本开支缺少完整金额，累计比例无法核验；不跳过缺年。")
    if debt_status == "missing" and industry.strip():
        notes.append("最新合并负债或权益金额缺失，债务指标无法核验。")
    if status == "stale":
        notes.append("实际获取时间或报告期未通过新鲜度核验，相关新指标不参与计分和确定性预警。")
    return {"status": status, "source": SOURCE, "currency": "CNY", "unit": "yuan", "statement_scope": "consolidated",
            "fetched_at": min(fetched) if fetched else None, "source_updated_at": max(updated) if updated else None,
            "window": {"start_year": window[0], "end_year": end, "years": window}, "years": years,
            "metrics": {"debt_equity": {"value": ratio, "status": debt_status, "excellent": ratio < .8 if debt_status == "ok" else None,
                                        "report_date": str(pd.Timestamp(latest["report_date"]).date()) if latest else None, "total_equity": equity},
                        "capex_to_profit": {"value": capex_ratio, "status": capex_status, "excellent": capex_ratio < .25 if capex_status == "ok" else None,
                                            "net_profit_sum": profit_sum, "capex_sum": capex_sum},
                        "annual_net_profit": {"value": annual_profit, "status": annual_status},
                        "cumulative_net_profit": {"value": profit_sum, "status": cumulative_status}},
            "loss_years": [y["fiscal_year"] for y in years if y["net_profit"] is not None and y["net_profit"] < 0], "notes": notes}
