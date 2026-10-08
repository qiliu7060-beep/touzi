"""美联储潮汐数据（美国利率、美元指数、美联储资产负债表）。

## 为什么换掉了 FRED

最初这个模块直接抓 **FRED 的公开 CSV 端点**（``fredgraph.csv?id=DFF``），
理由是它不需要 API key、字段干净。实测在本机网络下它**一段时间后就完全不可达**：

    HTTPSConnectionPool(host='fred.stlouisfed.org', port=443):
    SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING] ...'))

直连是 ReadTimeout，走代理是 SSLEOFError/ReadTimeout，而同时 google 走同一个
代理是 HTTP 200 —— 说明是目标站点侧的问题，不是代理坏了。重试 3 次仍全灭。
所以现在的**主源改成 Yahoo Finance 的 chart 接口**：

    https://query1.finance.yahoo.com/v8/finance/chart/^TNX?range=10y&interval=1d

实测 6 个符号全部 HTTP 200、日频、数据到 **2026-10-07/08**：

    ^IRX      13周美债收益率   → policy_rate（美联储政策利率的市场代理）
    ^FVX      5年期美债收益率   → us_5y
    ^TNX      10年期美债收益率  → us_10y
    ^TYX      30年期美债收益率  → us_30y
    DX-Y.NYB  美元指数 DXY      → dollar
    ^VIX      VIX 波动率        → vix

⚠️ ``range=max`` 会悄悄降级成**月频**（只有 169 个点），必须用
``range=10y&interval=1d`` 才拿到日频（2512 个点）。这是个很容易上当的坑。

## 三处口径上的诚实说明（报告里要照实写）

1. **联邦基金目标利率没有免费日频源**。FRED 的 DFF 拿不到了，
   akshare 的 ``macro_bank_usa_interest_rate``（金十）**只更新到 2025-10**，
   且末行数值还是 NaN，完全不可用。所以这里用 **13 周美债收益率（^IRX）**
   作为政策利率的市场代理——它紧贴联邦基金利率，方向判断上够用，
   但不能当成「美联储官方利率」引用。
2. **10 年期 TIPS 实际利率（原 DFII10）拿不到**，改用 **10 年期名义收益率
   （^TNX）**。名义利率 = 实际利率 + 通胀预期，在高通胀期两者会背离，
   因此这个子项的含义是「长端贴现率」而不是「真实利率」。
3. **美联储总资产（WALCL）没有可用的免费源**。保留了这个子项，
   抓不到时就**在可用子项之间重新归一化权重**（不是按 0 分算），
   并把缺席情况写进 detail。

另外附一个 **akshare ``bond_zh_us_rate`` 的备用源**（``fetch_us_treasury_from_ak``）：
它是中债登口径，日频、新鲜到 2026-10-07，而且**有 2 年期**（Yahoo 没有）。
缺点是不含美元指数、也不含政策利率。两者互为交叉验证。
"""

from __future__ import annotations

import io
import json
from typing import Iterable

import pandas as pd

from ..util import LOG, Cache, build_proxies, cached_fetch

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
YAHOO_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Yahoo 符号 -> (面板列名, 中文说明)
YAHOO_SYMBOLS: dict[str, tuple[str, str]] = {
    "^IRX": ("policy_rate", "13周美债收益率（美联储政策利率的市场代理）"),
    "^FVX": ("us_5y", "5年期美债收益率"),
    "^TNX": ("us_10y", "10年期美债收益率"),
    "^TYX": ("us_30y", "30年期美债收益率"),
    "DX-Y.NYB": ("dollar", "美元指数(DXY)"),
    "^VIX": ("vix", "VIX 波动率指数"),
}
SYMBOL_OF: dict[str, str] = {v[0]: k for k, v in YAHOO_SYMBOLS.items()}

# FRED 端点保留，仅用于尝试抓美联储总资产（WALCL）；不可达时会静默缺席
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_OPTIONAL: dict[str, str] = {"WALCL": "balance_sheet"}
FRED_START = "2000-01-01"


# --------------------------------------------------------------------- Yahoo
def _fetch_yahoo_chart(symbol: str, proxy: str | None, rng: str = "10y") -> pd.DataFrame:
    """抓 Yahoo chart 接口，返回 date/value 两列。"""
    import requests

    url = f"{YAHOO_CHART}{requests.utils.quote(symbol)}"
    resp = requests.get(
        url,
        params={"range": rng, "interval": "1d"},
        timeout=30,
        proxies=build_proxies(proxy),
        headers={"User-Agent": YAHOO_UA, "Accept": "application/json"},
    )
    resp.raise_for_status()
    payload = resp.json()
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"Yahoo 返回错误：{chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo 无数据：{symbol}")
    res = results[0]
    stamps = res.get("timestamp") or []
    quote = (res.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    rows = [
        (pd.Timestamp(ts, unit="s").normalize(), float(c))
        for ts, c in zip(stamps, closes)
        if c is not None
    ]
    if not rows:
        raise RuntimeError(f"Yahoo 收盘价全为空：{symbol}")
    df = pd.DataFrame(rows, columns=["date", "value"])
    return df.dropna().sort_values("date").reset_index(drop=True)


def fetch_yahoo_series(column: str, cache: Cache, proxy: str | None, ttl_days: float = 7.0) -> pd.Series:
    """按面板列名取一条 Yahoo 序列（如 ``us_10y`` / ``dollar``）。"""
    if column not in SYMBOL_OF:
        raise KeyError(f"未知的 Yahoo 列名：{column}，可选 {sorted(SYMBOL_OF)}")
    symbol = SYMBOL_OF[column]
    df = cached_fetch(cache, f"yahoo_{symbol}", lambda: _fetch_yahoo_chart(symbol, proxy), ttl_days=ttl_days)
    return pd.Series(df["value"].to_numpy(dtype=float), index=pd.to_datetime(df["date"])).dropna().sort_index()


# --------------------------------------------------------- FRED（仅 WALCL，可选）
def _fetch_fred_series(series_id: str, proxy: str | None, start: str = FRED_START) -> pd.DataFrame:
    import requests

    resp = requests.get(
        FRED_CSV,
        params={"id": series_id, "cosd": start},
        timeout=40,
        proxies=build_proxies(proxy),
        headers={"User-Agent": "Mozilla/5.0 touzi/0.1"},
    )
    resp.raise_for_status()
    text = resp.text
    if "<html" in text[:200].lower():
        raise RuntimeError(f"FRED 返回了 HTML 而非 CSV：{series_id}")
    df = pd.read_csv(io.StringIO(text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")  # FRED 用 "." 表示缺失
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def fetch_fed_balance_sheet(cache: Cache, proxy: str | None, ttl_days: float = 7.0) -> pd.Series:
    """美联储总资产（周频，百万美元）。源不可达时抛异常，由调用方决定缺席。"""
    df = cached_fetch(cache, "fred_WALCL", lambda: _fetch_fred_series("WALCL", proxy), ttl_days=ttl_days)
    return pd.Series(df["value"].to_numpy(dtype=float), index=pd.to_datetime(df["date"])).dropna().sort_index()


# --------------------------------------------------- akshare 中债登备用源
def fetch_us_treasury_from_ak(cache: Cache, proxy: str | None, ttl_days: float = 7.0) -> pd.DataFrame:
    """备用源：akshare ``bond_zh_us_rate``，日频，含美国 2/5/10/30 年期。"""
    def _get() -> pd.DataFrame:
        from ..util import apply_proxy_env
        import akshare as ak

        apply_proxy_env(proxy)
        raw = ak.bond_zh_us_rate()
        ren = {
            "日期": "date",
            "美国国债收益率2年": "us_2y",
            "美国国债收益率5年": "us_5y",
            "美国国债收益率10年": "us_10y",
            "美国国债收益率30年": "us_30y",
            "中国国债收益率10年": "cn_10y",
        }
        keep = {k: v for k, v in ren.items() if k in raw.columns}
        out = raw[list(keep)].rename(columns=keep)
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        for c in out.columns:
            if c != "date":
                out[c] = pd.to_numeric(out[c], errors="coerce")
        return out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    return cached_fetch(cache, "ak_bond_zh_us_rate", _get, ttl_days=ttl_days)


# ------------------------------------------------------------------ 面板
def fetch_fed_panel(
    cache: Cache,
    proxy: str | None,
    series: Iterable[str] | None = None,
    ttl_days: float = 7.0,
    include_balance_sheet: bool = True,
) -> pd.DataFrame:
    """取美联储相关序列，按日对齐（向前填充，避免周末空洞）。

    返回列名是 ``policy_rate / us_5y / us_10y / us_30y / dollar / vix``
    以及可选的 ``balance_sheet``。任何一条失败都只记日志，不影响其它列。
    """
    wanted = list(series) if series else [c for c, _ in YAHOO_SYMBOLS.values()]
    frames: list[pd.Series] = []
    for column in wanted:
        if column == "balance_sheet":
            continue
        try:
            s = fetch_yahoo_series(column, cache, proxy, ttl_days=ttl_days)
            s.name = column
            frames.append(s)
        except Exception as exc:
            LOG.warning("Yahoo 序列 %s(%s) 抓取失败：%s", column, SYMBOL_OF.get(column), exc)

    if include_balance_sheet and "balance_sheet" in wanted:
        try:
            s = fetch_fed_balance_sheet(cache, proxy, ttl_days=ttl_days)
            s.name = "balance_sheet"
            frames.append(s)
        except Exception as exc:
            LOG.warning("美联储总资产(WALCL)抓取失败，该子项将缺席：%s", exc)

    if not frames:
        raise RuntimeError("美联储相关序列全部抓取失败，请检查网络/代理设置")

    panel = pd.concat(frames, axis=1).sort_index().ffill()
    return panel


def to_monthly(panel: pd.DataFrame) -> pd.DataFrame:
    """把日频面板压成月末值——宏观信号是月度决策，日频只用于取月末快照。"""
    if panel.empty:
        return panel
    return panel.resample("ME").last()


__all__ = [
    "YAHOO_SYMBOLS",
    "SYMBOL_OF",
    "FRED_OPTIONAL",
    "fetch_yahoo_series",
    "fetch_fed_balance_sheet",
    "fetch_us_treasury_from_ak",
    "fetch_fed_panel",
    "to_monthly",
]
