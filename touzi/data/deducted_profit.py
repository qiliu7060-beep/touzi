"""新浪关键指标扣非归母金额：独立来源，仅供展示，不作为历史时点证据。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import urllib.request
from zoneinfo import ZoneInfo

import pandas as pd

from ..util import LOG, cached_fetch
from .stock import plain_code, to_tx_symbol

SOURCE = "sina-finance-key-indicators"
BASE = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
MAX_BYTES = 4 * 1024 * 1024
VERSION_BASIS = "vendor_report_year_series_restatement_unverified"


def source_url(code):
    code = plain_code(code)
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("扣非财报代码须为六位数字")
    return f"{BASE}?paperCode={to_tx_symbol(code)}&source=gjzb&type=0&page=1&num=40"


def amount(value):
    """只解析原始元金额；精确字符串保留小数，不乘股本、不猜单位。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and abs(result) <= Decimal("1e20") else None
    except InvalidOperation:
        return None


def normalize_deducted_reports(code, reports, fetched_at, response_sha256, url=None):
    if not isinstance(reports, dict):
        raise ValueError("扣非财报结构无效")
    url = url or source_url(code)
    rows = []
    for raw_date, report in reports.items():
        if not isinstance(raw_date, str) or not re.fullmatch(r"[0-9]{4}1231", raw_date):
            continue
        try:
            date = datetime.strptime(raw_date, "%Y%m%d").date()
        except ValueError:
            continue
        if not isinstance(report, dict) or not isinstance(report.get("data"), list):
            raise ValueError("扣非年度财报结构无效")
        items = [i for i in report["data"] if isinstance(i, dict) and i.get("item_field") == "NPCUT"]
        values = [amount(i.get("item_value")) for i in items]
        reason = "ok"
        if report.get("rCurrency") != "CNY":
            reason = "currency_unverified"
        elif report.get("rType") != "合并期末":
            reason = "scope_unverified"
        elif not items:
            reason = "field_missing"
        elif any(i.get("item_title") != "扣非净利润" or i.get("item_source") != "ysb" for i in items):
            reason = "field_identity_changed"
        elif len(set(values)) > 1:
            reason = "duplicate_conflict"
        elif values[0] is None:
            reason = "amount_missing_or_invalid"
        value = values[0] if reason == "ok" else None
        disclosed = report.get("publish_date")
        try:
            disclosed = datetime.strptime(str(disclosed), "%Y%m%d").date().isoformat()
        except ValueError:
            disclosed = None
        rows.append({
            "code": plain_code(code), "report_date": date.isoformat(),
            "deducted_parent_net_profit": float(value) if value is not None else None,
            "amount_exact": format(value, "f") if value is not None else None,
            "raw_values_json": json.dumps([i.get("item_value") for i in items], ensure_ascii=False),
            "value_status": reason, "source": SOURCE, "source_field": "NPCUT",
            "source_url": url, "response_sha256": response_sha256,
            "currency": report.get("rCurrency"), "unit": "yuan",
            "statement_scope": "consolidated" if report.get("rType") == "合并期末" else None,
            "supplier_report_type": report.get("rType"), "supplier_disclosed_date": disclosed,
            "disclosed_at": None, "disclosure_status": "unverified",
            "fetched_at": fetched_at, "source_updated_at": None,
            "supplier_data_source": report.get("data_source"), "supplier_audit_label": report.get("is_audit"),
            "version_basis": VERSION_BASIS, "point_in_time_eligible": False,
        })
    if not rows:
        raise ValueError("无完整年度扣非财报")
    return pd.DataFrame(rows)


def fetch_deducted_parent_profit(code, cache, ttl_days=1):
    """独立缓存；错误不覆盖好缓存，也不借用三表获取时间。"""
    url = source_url(code)
    metadata = {}
    failure = {}

    def read():
        failure.clear()
        failure["failure_category"] = "network"
        with urllib.request.urlopen(url, timeout=8) as response:
            raw = response.read(MAX_BYTES + 1)
        failure["failure_category"] = "response_limit"
        if len(raw) > MAX_BYTES:
            raise ValueError("扣非财报响应超出上限")
        failure["failure_category"] = "json"
        payload = json.loads(raw)
        failure["failure_category"] = "structure"
        reports = payload["result"]["data"]["report_list"]
        failure["failure_category"] = "normalization"
        frame = normalize_deducted_reports(
            code, reports, datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), hashlib.sha256(raw).hexdigest(), url)
        # 全部无效可能是上游结构变动，按失败处理，保留旧的有效缓存。
        if not frame["value_status"].eq("ok").any():
            failure["failure_reasons"] = sorted(set(frame["value_status"]))
            raise ValueError("扣非字段全部缺失或未通过口径核验")
        failure.clear()
        return frame

    try:
        frame = cached_fetch(cache, f"deducted_parent_v1_{to_tx_symbol(code)}", read,
                             ttl_days=ttl_days, retries=1, sleep=.2, metadata=metadata).copy()
    except Exception as exc:  # 网络/第三方及缓存边界，日志不包含原始异常文本。
        LOG.warning("%s 扣非财报失败: %s；保留缺失，不替代", plain_code(code), type(exc).__name__)
        frame = pd.DataFrame()
    frame.attrs["deducted_source"] = {"source": SOURCE, "source_url": url, **metadata, **failure}
    return frame


def annual_deducted_evidence(frame, report_date, now, ttl_days=1):
    """展示证据不参与三表新鲜度、评分、买卖规则或历史回测。"""
    now = pd.Timestamp(now)
    now = now.tz_localize("Asia/Shanghai") if now.tzinfo is None else now.tz_convert("Asia/Shanghai")
    metadata = (getattr(frame, "attrs", {}) or {}).get("deducted_source", {})
    empty = {"status": "missing", "reason": "source_not_loaded" if frame is None else "year_missing",
             "report_date": report_date, "source": SOURCE, "source_field": "NPCUT",
             "disclosed_at": None, "disclosure_status": "unverified", "point_in_time_eligible": False}
    if frame is None or frame.empty or "report_date" not in frame:
        return None, {**empty, "reason": "source_failed" if metadata.get("cache_status") == "source_failed" else empty["reason"],
                      "fetch_status": metadata}
    rows = frame[frame["report_date"].eq(report_date)]
    if len(rows) != 1:
        return None, {**empty, "reason": "duplicate_report" if len(rows) else "year_missing",
                      "fetch_status": metadata}
    row = rows.iloc[0].to_dict()
    evidence = {k: None if pd.isna(v) else v for k, v in row.items() if k != "deducted_parent_net_profit"}
    evidence.update(status="missing", reason=row.get("value_status"), fetch_status=metadata)
    if row.get("value_status") != "ok":
        return None, evidence
    try:
        fetched = pd.Timestamp(row["fetched_at"])
        if pd.isna(fetched) or fetched.tzinfo is None or fetched > now + pd.Timedelta(minutes=1):
            return None, {**evidence, "reason": "fetch_time_unverified"}
    except (KeyError, TypeError, ValueError):
        return None, {**evidence, "reason": "fetch_time_unverified"}
    published = row.get("supplier_disclosed_date")
    if published and not pd.isna(published) and str(published) > str(now.date()):
        return None, {**evidence, "reason": "supplier_disclosure_after_as_of"}
    value = amount(row.get("amount_exact"))
    if value is None:
        return None, {**evidence, "reason": "amount_missing_or_invalid"}
    stale = (now - fetched > pd.Timedelta(days=ttl_days)
             or metadata.get("cache_status") == "stale_fallback")
    return float(value), {**evidence, "status": "stale" if stale else "ok",
                          "reason": "stale_source" if stale else "ok"}
