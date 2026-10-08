"""中国宏观数据层（PMI / CPI / PPI / GDP / M2 / 国债收益率）。

## 为什么用新浪而不是金十

akshare 里形如 ``macro_china_pmi_yearly``、``macro_china_cpi_yearly`` 的接口
数据来自金十数据日历，**实测只更新到 2025-09**，而行情数据已到 2026-10，
落后整整一年。而 ``macro_china_pmi`` / ``macro_china_cpi`` / ``macro_china_ppi``
/ ``macro_china_gdp`` / ``macro_china_money_supply`` 走新浪，**能取到 2026 年
的最新月份**。宏观择时的输入一旦陈旧，整个仓位建议就是错的，所以这里统一
使用新浪源。

## 两个必须处理的坑

1. **新浪接口返回倒序**（最新月份在第 0 行），必须 ``sort_index()`` 正排。
2. **公布滞后**：8 月的 CPI 要到 9 月上旬才公布。回测时若在 8 月末就用上
   CPI 实际值，就是未来函数。所以每个序列都按 ``publish_lag_months``
   把时点向后推，近似「这条数据在什么时候才真的能看到」。
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd

from ..util import LOG, Cache, cached_fetch

__all__ = [
    "parse_cn_month",
    "apply_publish_lag",
    "fetch_cn_pmi",
    "fetch_cn_cpi",
    "fetch_cn_ppi",
    "fetch_cn_gdp",
    "fetch_cn_money_supply",
    "fetch_cn_bond_10y",
    "build_cn_macro_panel",
]

# 各指标的公布滞后（月）。0 表示当月月末即可见。
PUBLISH_LAG = {
    "pmi": 0,          # 国家统计局在当月最后一天公布当月 PMI
    "cpi": 1,          # 次月上旬公布
    "ppi": 1,
    "gdp": 1,
    "money_supply": 1,
}

_YM = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")
_YQ = re.compile(r"(\d{4})\s*年\s*第\s*(\d{1,2})\s*(?:-\s*(\d{1,2}))?\s*季度")
_RAW_YM = re.compile(r"^(\d{4})(\d{2})$")


def parse_cn_month(value: Any) -> pd.Timestamp:
    """把各种中文/数字月份写法国一成「该期最后一天」。

    支持 ``2026年09月份``、``202603``、``2026年第1-2季度``（累计口径 → 期末）。
    """
    text = str(value).strip()
    if not text:
        return pd.NaT

    m = _YQ.search(text)
    if m:
        year = int(m.group(1))
        # 「第1-2季度」是累计口径，期末取后一个季度
        end_q = int(m.group(3) or m.group(2))
        month = end_q * 3
        return pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)

    m = _YM.search(text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        return pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)

    m = _RAW_YM.match(text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            return pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)

    parsed = pd.to_datetime(text, errors="coerce")
    return pd.Timestamp(parsed) if pd.notna(parsed) else pd.NaT


def apply_publish_lag(series: pd.Series | pd.DataFrame, lag_months: int) -> pd.Series | pd.DataFrame:
    """把数据时点向后推 lag_months，近似「公布之后才可见」。

    回测的防未来函数关键步骤：8 月的 CPI 在 9 月末之前不许出现在特征里。
    对 DataFrame（PMI 的制造业/非制造业、货币供应的 M1/M2）同样适用。
    """
    if lag_months <= 0 or series.empty:
        return series
    shifted = series.copy()
    shifted.index = pd.DatetimeIndex(shifted.index) + pd.DateOffset(months=lag_months)
    return shifted[~shifted.index.duplicated(keep="last")].sort_index()


def _to_float(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype(float)


def _indexed(df: pd.DataFrame, value_col: str, month_col: str = "月份") -> pd.Series:
    """把「月份 + 数值」两列整理成按月末排序的 Series（去重、丢掉无效行）。"""
    if month_col not in df.columns or value_col not in df.columns:
        raise KeyError(f"缺少列 {month_col!r} 或 {value_col!r}，实际列为 {list(df.columns)}")
    idx = df[month_col].map(parse_cn_month)
    out = pd.Series(_to_float(df[value_col]).to_numpy(), index=pd.DatetimeIndex(idx))
    out = out[~out.index.isna()]
    out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()


# --------------------------------------------------------------------------- #
# 各指标抓取
# --------------------------------------------------------------------------- #
def fetch_cn_pmi(cache: Cache, proxy: str | None, ttl_days: float = 7.0) -> pd.DataFrame:
    """制造业与非制造业 PMI（官方口径，来源：新浪）。

    返回 DataFrame，索引月末，列 ``manufacturing`` / ``non_manufacturing``。
    """
    import akshare as ak

    raw = cached_fetch(cache, "cn_pmi_sina", lambda: ak.macro_china_pmi(), ttl_days=ttl_days)
    manu = _indexed(raw, "制造业-指数")
    non = _indexed(raw, "非制造业-指数") if "非制造业-指数" in raw.columns else pd.Series(dtype=float)
    out = pd.DataFrame({"manufacturing": manu, "non_manufacturing": non}).sort_index()
    LOG.info("PMI：%d 个月，最新 %s = %.1f", len(out), out.index[-1].date(), out["manufacturing"].iloc[-1])
    return out


def fetch_cn_cpi(cache: Cache, proxy: str | None, ttl_days: float = 7.0) -> pd.Series:
    """CPI 同比（%），来源：新浪。"""
    import akshare as ak

    raw = cached_fetch(cache, "cn_cpi_sina", lambda: ak.macro_china_cpi(), ttl_days=ttl_days)
    s = _indexed(raw, "全国-同比增长").rename("cpi_yoy")
    LOG.info("CPI：%d 个月，最新 %s = %.2f%%", len(s), s.index[-1].date(), s.iloc[-1])
    return s


def fetch_cn_ppi(cache: Cache, proxy: str | None, ttl_days: float = 7.0) -> pd.Series:
    """PPI 同比（%），来源：新浪。"""
    import akshare as ak

    raw = cached_fetch(cache, "cn_ppi_sina", lambda: ak.macro_china_ppi(), ttl_days=ttl_days)
    s = _indexed(raw, "当月同比增长").rename("ppi_yoy")
    LOG.info("PPI：%d 个月，最新 %s = %.2f%%", len(s), s.index[-1].date(), s.iloc[-1])
    return s


def fetch_cn_gdp(cache: Cache, proxy: str | None, ttl_days: float = 7.0) -> pd.Series:
    """GDP 累计同比（%），来源：新浪，季度频率、索引为该季末。"""
    import akshare as ak

    raw = cached_fetch(cache, "cn_gdp_sina", lambda: ak.macro_china_gdp(), ttl_days=ttl_days)
    s = _indexed(raw, "国内生产总值-同比增长", month_col="季度").rename("gdp_yoy")
    LOG.info("GDP：%d 个季度，最新 %s = %.1f%%", len(s), s.index[-1].date(), s.iloc[-1])
    return s


def fetch_cn_money_supply(cache: Cache, proxy: str | None, ttl_days: float = 7.0) -> pd.DataFrame:
    """M1 / M2 同比（%），来源：新浪。"""
    import akshare as ak

    raw = cached_fetch(cache, "cn_money_sina", lambda: ak.macro_china_money_supply(), ttl_days=ttl_days)
    out = pd.DataFrame(
        {
            "m2_yoy": _indexed(raw, "货币和准货币(M2)-同比增长"),
            "m1_yoy": _indexed(raw, "货币(M1)-同比增长"),
        }
    ).sort_index()
    LOG.info("货币供应：%d 个月，最新 M2 %.1f%% / M1 %.1f%%", len(out), out["m2_yoy"].iloc[-1], out["m1_yoy"].iloc[-1])
    return out


def fetch_cn_bond_10y(cache: Cache, proxy: str | None, ttl_days: float = 1.0) -> pd.Series:
    """中国 10 年期国债收益率（%），来源：新浪 ``bond_zh_us_rate``（日频）。"""
    import akshare as ak

    raw = cached_fetch(
        cache,
        "cn_bond_10y",
        lambda: ak.bond_zh_us_rate(start_date="20100101"),
        ttl_days=ttl_days,
    )
    s = pd.Series(
        _to_float(raw["中国国债收益率10年"]).to_numpy(),
        index=pd.DatetimeIndex(pd.to_datetime(raw["日期"], errors="coerce")),
    )
    s = s[~s.index.isna()].dropna().sort_index()
    s = s[~s.index.duplicated(keep="last")].rename("cn_10y")
    LOG.info("中国10年期国债：%d 个交易日，最新 %s = %.2f%%", len(s), s.index[-1].date(), s.iloc[-1])
    return s


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def build_cn_macro_panel(
    cache: Cache,
    proxy: str | None,
    start: str | None = None,
    ttl_days: float = 7.0,
    bond_ttl_days: float = 1.0,
) -> dict[str, Any]:
    """一次性取齐中国宏观数据，并按公布滞后包装成「可见时点」序列。

    返回字典，含原始序列（``raw`` 子字典，真实时点）与对齐后的序列（顶层，
    时点已按公布滞后推后，可直接用于回测）。

    ttl_days 控制月度/季度宏观（PMI/CPI/PPI/GDP/M2）的缓存时长，
    bond_ttl_days 单独控制 10 年期国债收益率——它是**盘中实时变动**的市场数据。
    """
    pmi = fetch_cn_pmi(cache, proxy, ttl_days=ttl_days)
    cpi = fetch_cn_cpi(cache, proxy, ttl_days=ttl_days)
    ppi = fetch_cn_ppi(cache, proxy, ttl_days=ttl_days)
    gdp = fetch_cn_gdp(cache, proxy, ttl_days=ttl_days)
    money = fetch_cn_money_supply(cache, proxy, ttl_days=ttl_days)
    bond = fetch_cn_bond_10y(cache, proxy, ttl_days=bond_ttl_days)

    raw = {"pmi": pmi, "cpi": cpi, "ppi": ppi, "gdp": gdp, "money": money, "bond_10y": bond}

    if start:
        start_ts = pd.Timestamp(start)
        raw = {k: (v[v.index >= start_ts] if len(v) else v) for k, v in raw.items()}

    aligned = {
        "pmi": apply_publish_lag(raw["pmi"], PUBLISH_LAG["pmi"]),
        "cpi": apply_publish_lag(raw["cpi"], PUBLISH_LAG["cpi"]),
        "ppi": apply_publish_lag(raw["ppi"], PUBLISH_LAG["ppi"]),
        "gdp": apply_publish_lag(raw["gdp"], PUBLISH_LAG["gdp"]),
        "money": apply_publish_lag(raw["money"], PUBLISH_LAG["money_supply"]),
        "bond_10y": raw["bond_10y"],  # 国债收益率本身就是实时市场数据，无公布滞后
    }

    # 数据新鲜度体检：以「真实时点」的最新一期为准，避免拿陈旧数据做择时而不自知
    staleness: dict[str, Any] = {}
    for key in ("pmi", "cpi", "ppi", "gdp"):
        s = raw[key]
        if s is None or len(s) == 0:
            continue
        last = s.index[-1]
        if isinstance(s, pd.DataFrame):
            # PMI 是两列（manufacturing / non_manufacturing），逐列取最新值
            row = s.iloc[-1]
            value = {c: (None if pd.isna(row[c]) else round(float(row[c]), 4)) for c in s.columns}
        else:
            value = round(float(s.iloc[-1]), 4)
        staleness[key] = {"last_period": str(last.date()), "value": value}
    LOG.info("宏观数据新鲜度：%s", {k: v["last_period"] for k, v in staleness.items()})

    return {"raw": raw, "aligned": aligned, "staleness": staleness}
