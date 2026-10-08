"""A 股数据层：股票列表、全市场估值快照、行业归属、个股行情/分红/财务。

## 数据源选择（都是实测可用后定下的）

- **全市场 PE/PB/市值**：``stock_zh_a_spot_tx()``（腾讯）一次请求拿到 5000+
  只股票的 ``pe_ttm``、``pn``(市净率)、``ltsz``(流通市值)。东财系的
  ``stock_zh_a_spot_em`` 依赖 ``push2.eastmoney.com``，在本机网络下不可达，
  已弃用。⚠️ 腾讯接口返回的数值都是**字符串**，必须显式转 float。
- **个股日线**：``stock_zh_a_daily()``（新浪，前复权）。
- **个股历史 PE/PB**：``stock_zh_valuation_baidu()``（百度），合法 indicator
  只有 总市值/市盈率(TTM)/市盈率(静)/市净率/市现率，**没有股息率**。
- **分红**：``stock_history_dividend_detail()`` 的「派息」单位是**每 10 股派息(元)**；
  ``stock_history_dividend()`` 一次给全市场的分红次数与年均股息。
- **行业归属**：``stock_sector_spot()`` 列出 49 个新浪板块 → 逐个
  ``stock_sector_detail(label)`` 取成分股。申万官网与同花顺成分接口不可用。

## 成本控制

个股级数据（历史估值、分红明细、财务指标）都是**逐股请求**，全市场 5500 只
不可能抓完。所以先用一次全市场快照做便宜的数量/质量过滤，再按流通市值
截取前 ``universe_max`` 只进入昂贵的逐股抓取。这一步的取舍写在
``build_universe()`` 的文档里。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from ..util import LOG, Cache, cached_fetch

__all__ = [
    "plain_code",
    "to_tx_symbol",
    "fetch_stock_list",
    "fetch_market_snapshot",
    "fetch_realtime_quote",
    "fetch_industry_map",
    "fetch_price_history",
    "fetch_dividend_detail",
    "fetch_dividend_summary",
    "fetch_financial_indicators",
    "fetch_valuation_history",
    "fetch_index_daily",
    "fetch_sw_industry_valuation",
    "parallel_map",
]


# --------------------------------------------------------------------------- #
# 代码格式
# --------------------------------------------------------------------------- #
def plain_code(symbol: str) -> str:
    """``sh600519`` / ``600519.SH`` → ``600519``。"""
    s = str(symbol).strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    for suffix in (".SH", ".SZ", ".BJ"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s.zfill(6) if s.isdigit() else s


def to_tx_symbol(code: str) -> str:
    """6 位代码 → 腾讯/新浪需要的带交易所前缀形式。"""
    c = plain_code(code)
    if c.startswith(("60", "68", "90", "11", "5")):
        return f"sh{c}"
    if c.startswith(("4", "8", "92")):
        return f"bj{c}"
    return f"sz{c}"


def parallel_map(
    fn: Callable[[Any], Any],
    items: Sequence[Any],
    workers: int = 6,
    label: str = "",
) -> dict[Any, Any]:
    """有界并发的 map，返回 ``{item: result}``；单个失败只记日志不中断整体。

    akshare 底层是阻塞式 requests，用线程池就足够，不必上异步。
    """
    results: dict[Any, Any] = {}
    if not items:
        return results
    total = len(items)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fn, item): item for item in items}
        for fut in as_completed(futures):
            item = futures[fut]
            done += 1
            try:
                results[item] = fut.result()
            except Exception as exc:
                LOG.debug("并发任务失败 %s: %s: %s", item, type(exc).__name__, exc)
                results[item] = None
            if label and (done % 50 == 0 or done == total):
                LOG.info("%s 进度 %d/%d", label, done, total)
    ok = sum(1 for v in results.values() if _nonempty(v))
    LOG.info("%s 完成：成功 %d / 共 %d", label or "并发抓取", ok, total)
    return results


def _nonempty(value: Any) -> bool:
    """抓取成功的判定：非 None，且若可求长度则长度大于 0。"""
    if value is None:
        return False
    try:
        return len(value) > 0
    except TypeError:
        return True


# --------------------------------------------------------------------------- #
# 股票列表与快照
# --------------------------------------------------------------------------- #
def fetch_stock_list(cache: Cache, proxy: str | None, ttl_days: float = 7.0) -> pd.DataFrame:
    """沪深两市股票列表（含上市日期）。

    不用 ``stock_info_a_code_name`` —— 它内部要访问北交所 ``www.bse.cn``，
    本机代理会拒连。分别取沪深两个接口再合并即可。

    返回列：``code``(6位) / ``name`` / ``list_date`` / ``exchange`` / ``board``。
    """
    import akshare as ak

    def _fetch() -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        try:
            sh = ak.stock_info_sh_name_code()
            sh = sh.rename(columns={"证券代码": "code", "证券简称": "name", "上市日期": "list_date"})
            sh["exchange"] = "SH"
            frames.append(sh[["code", "name", "list_date", "exchange"]])
        except Exception as exc:
            LOG.warning("沪市列表抓取失败：%s", exc)
        try:
            sz = ak.stock_info_sz_name_code()
            sz = sz.rename(
                columns={"A股代码": "code", "A股简称": "name", "A股上市日期": "list_date", "板块": "board"}
            )
            sz["exchange"] = "SZ"
            if "board" not in sz.columns:
                sz["board"] = ""
            frames.append(sz[["code", "name", "list_date", "exchange", "board"]])
        except Exception as exc:
            LOG.warning("深市列表抓取失败：%s", exc)
        if not frames:
            raise RuntimeError("沪深股票列表均抓取失败")
        return pd.concat(frames, ignore_index=True)

    df = cached_fetch(cache, "stock_list_shsz", _fetch, ttl_days=ttl_days)
    df["code"] = df["code"].map(plain_code)
    df["name"] = df["name"].astype(str).str.strip()
    df["list_date"] = pd.to_datetime(df["list_date"], errors="coerce")
    df = df.dropna(subset=["code"]).drop_duplicates(subset=["code"], keep="first")
    LOG.info("股票列表：%d 只（沪 %d / 深 %d）", len(df),
             int((df["exchange"] == "SH").sum()), int((df["exchange"] == "SZ").sum()))
    return df.reset_index(drop=True)


def fetch_market_snapshot(cache: Cache, proxy: str | None, ttl_days: float = 1.0) -> pd.DataFrame:
    """全市场估值快照（腾讯），一次请求覆盖 5000+ 只股票。

    返回列：``code`` / ``name`` / ``price`` / ``pe_ttm`` / ``pb`` /
    ``float_mktcap_yi``(流通市值，亿元) / ``turnover_rate``。
    """
    import akshare as ak

    raw = cached_fetch(cache, "market_snapshot_tx", lambda: ak.stock_zh_a_spot_tx(), ttl_days=ttl_days)

    def num(col: str) -> pd.Series:
        if col not in raw.columns:
            return pd.Series(np.nan, index=raw.index, dtype=float)
        return pd.to_numeric(raw[col], errors="coerce").astype(float)

    out = pd.DataFrame(
        {
            "code": raw["code"].map(plain_code),
            "name": raw["name"].astype(str).str.strip(),
            "pe_ttm": num("pe_ttm"),
            "pb": num("pn"),                      # 腾讯用 pn 表示市净率
            "float_mktcap_yi": num("ltsz"),       # 流通市值(亿元)
            "turnover_rate": num("hsl"),          # 换手率(%)
        }
    )
    # 腾讯不给最新价，用「流通市值 / 流通股数」拿不到，所以价格单独从新浪快照补
    try:
        sina = cached_fetch(cache, "market_snapshot_sina", lambda: ak.stock_zh_a_spot(), ttl_days=ttl_days)
        price = pd.DataFrame(
            {
                "code": sina["代码"].map(plain_code),
                "price": pd.to_numeric(sina["最新价"], errors="coerce"),
                "amount": pd.to_numeric(sina["成交额"], errors="coerce"),
            }
        ).drop_duplicates(subset=["code"], keep="last")
        out = out.merge(price, on="code", how="left")
    except Exception as exc:
        LOG.warning("新浪快照补充价格失败（估值分位以外不受影响）：%s", exc)
        out["price"] = np.nan
        out["amount"] = np.nan

    out = out.dropna(subset=["code"]).drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)
    LOG.info("全市场快照：%d 只，PE 有效 %d，PB 有效 %d",
             len(out), int(out["pe_ttm"].notna().sum()), int(out["pb"].notna().sum()))
    return out


# --------------------------------------------------------------------------- #
# 秒级实时行情（腾讯批量报价，不走缓存）
# --------------------------------------------------------------------------- #
_TX_QUOTE_URL = "http://qt.gtimg.cn/q="

# 腾讯 ``v_sh600519="1~贵州茅台~600519~..."`` 的字段下标（2026-10 实测 88 个字段，逐个核对过）
_TX_FIELDS: dict[str, int] = {
    "name": 1,
    "code": 2,
    "price": 3,
    "prev_close": 4,
    "open": 5,
    "quote_time": 30,          # YYYYMMDDHHMMSS
    "change": 31,
    "change_pct": 32,
    "high": 33,
    "low": 34,
    "volume_lots": 36,
    "amount_wan": 37,
    "turnover_rate": 38,
    "pe_ttm": 39,
    "amplitude": 43,
    "float_mktcap_yi": 44,
    "total_mktcap_yi": 45,
    "pb": 46,
}
_TX_NUMERIC = [k for k in _TX_FIELDS if k not in ("name", "code", "quote_time")]


def fetch_realtime_quote(
    codes: Sequence[str],
    proxy: str | None = None,
    timeout: float = 8.0,
    chunk: int = 60,
) -> pd.DataFrame:
    """腾讯批量**实时**行情：现价 / 涨跌 / PE(TTM) / PB / 流通市值 / 报价时间。

    和 :func:`fetch_market_snapshot` 的区别很重要：

    * 那个是「全市场 5000+ 只、带磁盘缓存（默认 1 天）」的快照；
    * 这个是「就这几只、**现在**多少钱」，**每次调用真打网络、绝不读缓存**，
      用来让网页上的持仓盈亏按秒级刷新，不必等一轮几分钟的全量重算。

    腾讯该接口一次能查几十只（这里按 ``chunk`` 分批），返回 GBK 编码的
    ``v_sh600519="1~贵州茅台~600519~1255.79~..."``。只取 :data:`_TX_FIELDS` 里
    逐个核对过的下标，不靠猜。任何一只解析失败都只是少一行，不影响其它。

    返回列：``code``(6 位) / ``name`` / ``price`` / ``prev_close`` / ``open`` /
    ``change`` / ``change_pct`` / ``high`` / ``low`` / ``volume_lots`` /
    ``amount_wan`` / ``turnover_rate`` / ``pe_ttm`` / ``amplitude`` /
    ``float_mktcap_yi`` / ``total_mktcap_yi`` / ``pb`` / ``quote_time``。
    """
    import requests

    from ..util import build_proxies

    syms = [to_tx_symbol(c) for c in codes if str(c).strip()]
    # 去重但保持顺序，避免同一只查两遍
    seen: set[str] = set()
    syms = [s for s in syms if not (s in seen or seen.add(s))]
    if not syms:
        return pd.DataFrame(columns=list(_TX_FIELDS) + ["quote_time"])

    proxies = build_proxies(proxy)
    rows: list[dict[str, Any]] = []
    for i in range(0, len(syms), max(1, chunk)):
        batch = syms[i : i + max(1, chunk)]
        url = _TX_QUOTE_URL + ",".join(batch)
        try:
            resp = requests.get(url, proxies=proxies, timeout=timeout)
            resp.encoding = "gbk"
            text = resp.text
        except Exception as exc:  # noqa: BLE001  网络失败只丢这一批
            LOG.warning("实时报价抓取失败（%d 只）：%s: %s", len(batch), type(exc).__name__, exc)
            continue
        for piece in text.split(";"):
            piece = piece.strip()
            if "=" not in piece:
                continue
            _, body = piece.split("=", 1)
            fields = body.strip().strip('"').split("~")
            if len(fields) <= max(_TX_FIELDS.values()):
                continue
            row: dict[str, Any] = {}
            for key, idx in _TX_FIELDS.items():
                row[key] = fields[idx]
            if not row.get("code"):
                continue
            rows.append(row)

    if not rows:
        return pd.DataFrame(columns=list(_TX_FIELDS))

    out = pd.DataFrame(rows)
    out["code"] = out["code"].map(plain_code)
    for col in _TX_NUMERIC:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype(float)
    out["name"] = out["name"].astype(str).str.strip()
    out["quote_time"] = out["quote_time"].astype(str)
    out["quote_ts"] = pd.to_datetime(out["quote_time"], format="%Y%m%d%H%M%S", errors="coerce")
    out = out.dropna(subset=["code"]).drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)
    LOG.info("实时报价：%d 只（%s ~ %s）",
             len(out),
             out["quote_ts"].min() if out["quote_ts"].notna().any() else "?",
             out["quote_ts"].max() if out["quote_ts"].notna().any() else "?")
    return out


def fetch_industry_map(cache: Cache, proxy: str | None, ttl_days: float = 30.0) -> pd.DataFrame:
    """个股 → 行业映射（新浪 49 个板块）。

    做法：先 ``stock_sector_spot()`` 拿到全部板块 label，再逐个
    ``stock_sector_detail(label)`` 取成分股。板块结构变化很慢，缓存 30 天。
    """
    import akshare as ak

    def _fetch() -> pd.DataFrame:
        spot = ak.stock_sector_spot()
        labels = [str(x) for x in spot["label"].dropna().tolist()]
        names = dict(zip(spot["label"].astype(str), spot["板块"].astype(str)))

        def _one(lab: str) -> pd.DataFrame | None:
            try:
                d = ak.stock_sector_detail(sector=lab)
            except Exception:
                return None
            if d is None or len(d) == 0 or "code" not in d.columns:
                return None
            return pd.DataFrame(
                {
                    "code": d["code"].map(plain_code),
                    "industry": names.get(lab, lab),
                    "industry_pe": pd.to_numeric(d.get("per"), errors="coerce"),
                    "industry_pb": pd.to_numeric(d.get("pb"), errors="coerce"),
                }
            )

        parts = [p for p in parallel_map(_one, labels, workers=6, label="行业成分").values() if p is not None]
        if not parts:
            raise RuntimeError("新浪行业成分全部抓取失败")
        return pd.concat(parts, ignore_index=True)

    df = cached_fetch(cache, "industry_map_sina", _fetch, ttl_days=ttl_days)
    df["code"] = df["code"].map(plain_code)
    df = df.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)
    LOG.info("行业映射：%d 只股票，%d 个板块", len(df), df["industry"].nunique())
    return df


def fetch_sw_industry_valuation(cache: Cache, proxy: str | None, ttl_days: float = 7.0) -> pd.DataFrame:
    """申万一级行业估值（31 个行业），用于估值行业中性化的基准。"""
    import akshare as ak

    raw = cached_fetch(cache, "sw_industry_valuation", lambda: ak.sw_index_first_info(), ttl_days=ttl_days)
    out = pd.DataFrame(
        {
            "industry": raw["行业名称"].astype(str).str.strip(),
            "sw_pe_ttm": pd.to_numeric(raw["TTM(滚动)市盈率"], errors="coerce"),
            "sw_pb": pd.to_numeric(raw["市净率"], errors="coerce"),
            "sw_dividend_yield": pd.to_numeric(raw["静态股息率"], errors="coerce"),
        }
    )
    LOG.info("申万一级行业估值：%d 个行业", len(out))
    return out


# --------------------------------------------------------------------------- #
# 个股级数据
# --------------------------------------------------------------------------- #
def fetch_price_history(
    code: str,
    cache: Cache,
    start: str = "2011-01-01",
    end: str | None = None,
    adjust: str = "qfq",
    ttl_days: float = 1.0,
) -> pd.DataFrame:
    """个股日线（新浪，默认前复权）。

    返回 DataFrame，索引为日期，列 ``open/high/low/close/volume/amount``。
    """
    import akshare as ak

    sym = to_tx_symbol(code)
    end = end or pd.Timestamp.today().strftime("%Y%m%d")
    key = f"price_{sym}_{start}_{end}_{adjust}"

    def _fetch() -> pd.DataFrame:
        try:
            df = ak.stock_zh_a_daily(symbol=sym, start_date=start.replace("-", ""),
                                     end_date=end.replace("-", ""), adjust=adjust)
        except TypeError:
            df = ak.stock_zh_a_daily(symbol=sym, start_date=start.replace("-", ""),
                                     end_date=end.replace("-", ""))
        if df is None or len(df) == 0:
            raise RuntimeError("空数据")
        return df

    raw = cached_fetch(cache, key, _fetch, ttl_days=ttl_days, retries=1, sleep=1.0)
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    keep = [c for c in ("open", "high", "low", "close", "volume", "amount") if c in df.columns]
    for c in keep:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[keep]


def fetch_dividend_detail(
    code: str, cache: Cache, proxy: str | None = None, ttl_days: float = 7.0
) -> pd.DataFrame:
    """个股分红明细。

    ⚠️ 原始「派息」列的单位是**每 10 股派息(元)**
    （茅台 2024-06-12 派息 308.76 = 2023 年度每 10 股派 308.76 元），
    因此这里已经除以 10 换成了**每股派息**，列名 ``dps``。
    """
    import akshare as ak

    def _fetch() -> pd.DataFrame:
        df = ak.stock_history_dividend_detail(symbol=plain_code(code), indicator="分红")
        if df is None or len(df) == 0:
            raise RuntimeError("空数据")
        return df

    raw = cached_fetch(cache, f"div_detail_{plain_code(code)}", _fetch,
                       ttl_days=ttl_days, retries=1, sleep=1.0)
    df = raw.copy()
    df["ex_date"] = pd.to_datetime(df.get("除权除息日"), errors="coerce")
    df["plan_date"] = pd.to_datetime(df.get("公告日期"), errors="coerce")
    df["progress"] = df.get("进度", pd.Series("", index=df.index)).astype(str)
    # 单位换算：每 10 股派息(元) -> 每股派息(元)
    df["dps"] = pd.to_numeric(df.get("派息"), errors="coerce") / 10.0
    df["bonus"] = pd.to_numeric(df.get("送股"), errors="coerce").fillna(0.0) / 10.0
    df["transfer"] = pd.to_numeric(df.get("转增"), errors="coerce").fillna(0.0) / 10.0
    df = df[df["progress"].str.contains("实施", na=False)]
    df = df.dropna(subset=["ex_date"]).sort_values("ex_date")
    return df[["ex_date", "plan_date", "dps", "bonus", "transfer", "progress"]].reset_index(drop=True)


def fetch_dividend_summary(cache: Cache, proxy: str | None, ttl_days: float = 7.0) -> pd.DataFrame:
    """全市场分红概览（一次请求）：累计股息、年均股息、分红次数、融资次数。

    「分红次数 + 融资次数」用来判断一家公司是**回报股东**还是**伸手要钱**，
    这是红利质量的重要侧面。
    """
    import akshare as ak

    raw = cached_fetch(cache, "dividend_summary_all", lambda: ak.stock_history_dividend(), ttl_days=ttl_days)
    out = pd.DataFrame(
        {
            "code": raw["代码"].map(plain_code),
            "list_date": pd.to_datetime(raw["上市日期"], errors="coerce"),
            "cum_dividend": pd.to_numeric(raw["累计股息"], errors="coerce"),
            "avg_dividend": pd.to_numeric(raw["年均股息"], errors="coerce"),
            "dividend_times": pd.to_numeric(raw["分红次数"], errors="coerce"),
            "finance_total": pd.to_numeric(raw["融资总额"], errors="coerce"),
            "finance_times": pd.to_numeric(raw["融资次数"], errors="coerce"),
        }
    )
    out = out.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)
    LOG.info("分红概览：%d 只股票", len(out))
    return out


def fetch_financial_indicators(
    code: str, cache: Cache, start_year: str = "2015", ttl_days: float = 30.0
) -> pd.DataFrame:
    """个股财务指标（新浪），按报告期返回 80+ 列。

    只保留质量模块要用的几列并统一改名，避免下游依赖新浪的中文列名。
    """
    import akshare as ak

    def _fetch() -> pd.DataFrame:
        df = ak.stock_financial_analysis_indicator(symbol=plain_code(code), start_year=start_year)
        if df is None or len(df) == 0:
            raise RuntimeError("空数据")
        return df

    raw = cached_fetch(cache, f"fin_{plain_code(code)}_{start_year}", _fetch,
                       ttl_days=ttl_days, retries=1, sleep=1.0)

    def pick(*candidates: str) -> pd.Series:
        for c in candidates:
            if c in raw.columns:
                return pd.to_numeric(raw[c], errors="coerce")
        return pd.Series(np.nan, index=raw.index, dtype=float)

    date_col = next((c for c in ("日期", "报告期", "date") if c in raw.columns), None)
    if date_col is None:
        return pd.DataFrame()
    out = pd.DataFrame(
        {
            "report_date": pd.to_datetime(raw[date_col], errors="coerce"),
            "roe": pick("净资产收益率(%)", "加权净资产收益率(%)"),
            "gross_margin": pick("销售毛利率(%)"),
            "net_margin": pick("销售净利率(%)"),
            "debt_ratio": pick("资产负债率(%)"),
            "current_ratio": pick("流动比率"),
            "profit_growth": pick("净利润增长率(%)"),
            "revenue_growth": pick("主营业务收入增长率(%)"),
            "ocf_to_profit": pick("经营现金净流量与净利润的比率(%)"),
        }
    )
    out = out.dropna(subset=["report_date"]).sort_values("report_date")

    # ⚠️ 量纲陷阱（已用 5 只股票、含银行交叉验证）：
    # 新浪这一列叫「经营现金净流量与净利润的比率(%)」，但它的取值**就是倍数本身**，
    # 与「每股经营性现金流 / 摊薄每股收益」逐期一致到小数点后 4 位（比值恒为 1.0000）。
    # 例：茅台 2025 年报 0.7212、工行 2025 年报 5.0990。
    # 所以**绝不能再除以 100**，否则现金流项会全部塌到 0.007~0.05，把好公司判成垃圾。
    # 同理「销售毛利率(%)」列存在但整列为 NaN（新浪不提供该字段），
    # 保留列只为占位与诊断，质量打分不使用它。

    # 新浪财务表是**年内累计**口径：ROE 3-31→10.39、6-30→19.03、9-30→25.14、12-31→33.65。
    # 只有 12-31 那一期是完整年度值，跨期直接平均会得到无意义的结果。
    # 这里显式标出年度行，由下游（summarize_financials）只对年度行取均值。
    out["is_annual"] = out["report_date"].dt.month.eq(12)
    return out.reset_index(drop=True)


def fetch_valuation_history(
    code: str, cache: Cache, indicator: str = "市盈率(TTM)", period: str = "近五年",
    ttl_days: float = 7.0,
) -> pd.Series:
    """个股历史估值序列（百度）。

    合法 indicator：总市值 / 市盈率(TTM) / 市盈率(静) / 市净率 / 市现率。
    百度**不提供股息率**，股息率必须自己用分红明细算。
    """
    import akshare as ak

    key = f"val_baidu_{plain_code(code)}_{indicator}_{period}"

    def _fetch() -> pd.DataFrame:
        df = ak.stock_zh_valuation_baidu(symbol=plain_code(code), indicator=indicator, period=period)
        if df is None or len(df) == 0:
            raise RuntimeError("空数据")
        return df

    raw = cached_fetch(cache, key, _fetch, ttl_days=ttl_days, retries=1, sleep=1.0)
    s = pd.Series(
        pd.to_numeric(raw["value"], errors="coerce").to_numpy(dtype=float),
        index=pd.DatetimeIndex(pd.to_datetime(raw["date"], errors="coerce")),
    )
    s = s[~s.index.isna()].dropna().sort_index()
    return s[~s.index.duplicated(keep="last")].rename(indicator)


def index_symbol(symbol: str) -> str:
    """把指数代码规范成新浪要的带前缀形式。

    配置里写的是 ``"000300"``（纯代码），而 ``ak.stock_zh_index_daily`` 要的是
    ``"sh000300"``。传错不会报「格式错误」，而是抛一个莫名其妙的
    ``KeyError: 'date'``——踩过一次，所以在这里统一收口。
    """
    s = str(symbol).strip().lower()
    if s.startswith(("sh", "sz", "bj", "csi")):
        return s
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return s
    # 上证系指数以 000/880 开头，深证系以 399 开头
    return ("sh" if digits.startswith(("000", "880")) else "sz") + digits


def fetch_index_daily(
    symbol: str = "sh000300", cache: Cache | None = None, ttl_days: float = 1.0
) -> pd.DataFrame:
    """指数日线（新浪），默认沪深300。``symbol`` 可以是 ``000300`` 或 ``sh000300``。"""
    import akshare as ak

    symbol = index_symbol(symbol)

    def _fetch() -> pd.DataFrame:
        df = ak.stock_zh_index_daily(symbol=symbol)
        if df is None or len(df) == 0:
            raise RuntimeError("空数据")
        return df

    if cache is None:
        raw = _fetch()
    else:
        raw = cached_fetch(cache, f"index_{symbol}", _fetch, ttl_days=ttl_days)
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    for c in ("open", "high", "low", "close", "volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    LOG.info("指数 %s：%d 根日线，%s ~ %s", symbol, len(df),
             df.index[0].date(), df.index[-1].date())
    return df
