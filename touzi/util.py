"""通用工具：缓存、取数会话、分位、插值映射、周期识别。

这些函数被数据层与信号层共用，保持无副作用、可单测。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd

LOG = logging.getLogger("touzi")

_CACHE_SUBDIR = ""


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def setup_logging(level: int = logging.INFO) -> None:
    if logging.getLogger("touzi").handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger("touzi")
    root.addHandler(handler)
    root.setLevel(level)


# --------------------------------------------------------------------------- #
# 网络会话（本机需要走代理才能访问外网）
# --------------------------------------------------------------------------- #
def build_proxies(proxy: str | None) -> dict[str, str] | None:
    """把配置里的代理字符串转成 requests 需要的格式。"""
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def apply_proxy_env(proxy: str | None) -> None:
    """akshare 内部大量使用 requests，通过环境变量统一挂代理。"""
    if not proxy:
        return
    os.environ.setdefault("HTTP_PROXY", proxy)
    os.environ.setdefault("HTTPS_PROXY", proxy)
    os.environ["http_proxy"] = proxy
    os.environ["https_proxy"] = proxy


# --------------------------------------------------------------------------- #
# 缓存 TTL 策略：实时模式把「天」压到「分钟」
# --------------------------------------------------------------------------- #
# 五类数据的刷新节奏不一样，分开配：
#   quote   行情快照（价格 / PE-TTM / PB / 市值）—— 可以做到盘中实时
#   history 个股估值历史（百度）与指数日线 —— T+0 收盘后才有新的，盘中刷新意义有限
#   us      美股 / 美元指数 / 美债利率 —— 隔夜数据，但盘中也会变
#   macro   中国宏观（PMI/CPI/PPI/GDP/M2/国债）—— 月度/季度公布，做不到实时
#   fund    个股财务、分红、行业归属 —— 季度/事件
TTL_KEYS = ("quote", "history", "us", "macro", "fund")


def is_live(cfg: Any) -> bool:
    """是否处于实时模式。

    优先看 `general.live`（由 run_all.py 的 --live 注入），
    其次看 `[realtime] enabled`。
    """
    g = cfg.get("general", {}) if hasattr(cfg, "get") else {}
    if "live" in g and g.get("live") is not None:
        return bool(g.get("live"))
    rt = cfg.get("realtime", {}) if hasattr(cfg, "get") else {}
    return bool(rt.get("enabled", False))


def resolve_ttls(cfg: Any, live: bool | None = None) -> dict[str, float]:
    """算出各类数据的缓存 TTL，**单位是天**（Cache 内部就是按天算的）。

    非实时模式沿用 [general] 的按天配置，保证「抓一次能用很久、断网可复现」；
    实时模式把行情压到分钟级，每次重跑都会重新抓最新值。
    """
    g = cfg.get("general", {}) if hasattr(cfg, "get") else {}
    rt = cfg.get("realtime", {}) if hasattr(cfg, "get") else {}
    macro_d = float(g.get("cache_ttl_macro_days", 7) or 7)
    price_d = float(g.get("cache_ttl_price_days", 1) or 1)
    offline = {"quote": price_d, "history": price_d, "us": macro_d,
               "macro": macro_d, "fund": 30.0}
    if not (is_live(cfg) if live is None else bool(live)):
        return offline

    def _minutes(key: str, default: float) -> float:
        return max(float(rt.get(key, default) or default), 0.0) / 1440.0

    def _hours(key: str, default: float) -> float:
        return max(float(rt.get(key, default) or default), 0.0) / 24.0

    return {
        "quote": _minutes("quote_ttl_minutes", 3.0),
        "history": _minutes("history_ttl_minutes", 30.0),
        "us": _minutes("us_ttl_minutes", 30.0),
        "macro": _hours("macro_ttl_hours", 6.0),
        "fund": _hours("fund_ttl_hours", 24.0),
    }


# --------------------------------------------------------------------------- #
# 磁盘缓存
# --------------------------------------------------------------------------- #
class Cache:
    """极简 parquet/csv 缓存。

    宏观数据抓一次能用很久，行情数据每天更新一次即可；
    这样既省接口调用，也让整个系统在断网时仍可复现历史结果。
    """

    def __init__(self, cache_dir: str | Path, ttl_days: float = 7.0):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(days=ttl_days)

    # 本机 pyarrow 是 NumPy 1.x 时代编译的，在 NumPy 2.1.3 下 import 就会抛
    # ``AttributeError: _ARRAY_API not found``，于是 ``to_parquet`` 必然失败。
    # 缓存如果只有 parquet 一条路，就会静默地「永远不缓存」——每次运行都重新
    # 抓一遍全网数据。所以这里按 parquet -> pickle 的顺序降级，pickle 无需任何
    # 额外依赖，且能原样保留索引与 dtype。
    _FORMATS = ("parquet", "pkl")

    def _key_path(self, key: str) -> tuple[Path, Path]:
        safe = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
        stem = f"{_sanitize(key)[:60]}_{safe}"
        return self.dir / stem, self.dir / f"{stem}.meta.json"

    @staticmethod
    def _write_frame(df: pd.DataFrame, stem: Path) -> str:
        errors: list[str] = []
        for fmt in Cache._FORMATS:
            path = stem.with_suffix(f".{fmt}")
            try:
                if fmt == "parquet":
                    df.to_parquet(path, index=False)
                else:
                    df.to_pickle(path)
                return fmt
            except Exception as exc:
                errors.append(f"{fmt}: {type(exc).__name__}")
        raise RuntimeError("所有缓存格式都写入失败 -> " + "; ".join(errors))

    @staticmethod
    def _read_frame(stem: Path) -> pd.DataFrame | None:
        for fmt in Cache._FORMATS:
            path = stem.with_suffix(f".{fmt}")
            if not path.exists():
                continue
            try:
                return pd.read_parquet(path) if fmt == "parquet" else pd.read_pickle(path)
            except Exception:
                continue
        return None

    @staticmethod
    def _has_data(stem: Path) -> bool:
        return any(stem.with_suffix(f".{fmt}").exists() for fmt in Cache._FORMATS)

    def _fresh(self, meta_path: Path, ttl_days: float | None) -> bool:
        if not meta_path.exists():
            return False
        ttl = self.ttl if ttl_days is None else timedelta(days=ttl_days)
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
            ts = datetime.fromisoformat(meta["saved_at"])
        except Exception:
            return False
        return datetime.now() - ts < ttl

    def saved_at(self, key: str) -> datetime | None:
        """缓存最后一次写入的时间（用于在看板上披露数据新鲜度）。"""
        _, meta_path = self._key_path(key)
        if not meta_path.exists():
            return None
        try:
            return datetime.fromisoformat(json.loads(meta_path.read_text("utf-8"))["saved_at"])
        except Exception:
            return None

    def get(self, key: str, ttl_days: float | None = None, allow_stale: bool = False) -> pd.DataFrame | None:
        stem, meta_path = self._key_path(key)
        if not self._has_data(stem):
            return None
        if not allow_stale and not self._fresh(meta_path, ttl_days):
            return None
        return self._read_frame(stem)

    def put(self, key: str, df: pd.DataFrame) -> None:
        if df is None or len(df) == 0:
            return
        stem, meta_path = self._key_path(key)
        try:
            fmt = self._write_frame(df, stem)
            meta_path.write_text(
                json.dumps(
                    {
                        "key": key,
                        "saved_at": datetime.now().isoformat(),
                        "rows": int(len(df)),
                        "format": fmt,
                    },
                    ensure_ascii=False,
                ),
                "utf-8",
            )
        except Exception as exc:  # pragma: no cover
            LOG.warning("缓存写入失败 %s: %s", key, exc)


def _sanitize(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text)


def cached_fetch(
    cache: Cache,
    key: str,
    fetcher: Callable[[], pd.DataFrame],
    ttl_days: float | None = None,
    retries: int = 2,
    sleep: float = 1.5,
    allow_stale_on_error: bool = True,
) -> pd.DataFrame:
    """带缓存与重试的取数。失败时若存在旧缓存则降级使用旧数据。"""
    hit = cache.get(key, ttl_days=ttl_days)
    if hit is not None and len(hit) > 0:
        LOG.debug("缓存命中：%s", key)
        return hit

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            df = fetcher()
            if df is not None and len(df) > 0:
                cache.put(key, df)
                return df
            last_exc = RuntimeError("接口返回空数据")
        except Exception as exc:
            last_exc = exc
        if attempt < retries:
            time.sleep(sleep * (attempt + 1))

    stale = cache.get(key, allow_stale=True)
    if stale is not None and len(stale) > 0 and allow_stale_on_error:
        LOG.warning("接口 %s 抓取失败(%s)，降级使用旧缓存", key, last_exc)
        return stale
    raise RuntimeError(f"取数失败且无可用缓存：{key}，原因：{last_exc}")


# --------------------------------------------------------------------------- #
# 数值工具
# --------------------------------------------------------------------------- #
def safe_pct_change(series: pd.Series, periods: int = 1) -> pd.Series:
    return series.pct_change(periods, fill_method=None).replace([np.inf, -np.inf], np.nan)


def roll_percentile(series: pd.Series, window: int, ascending: bool = True) -> pd.Series:
    """滚动历史分位（0~1）。

    ascending=True 表示值越大分位越高。返回的是「当前值在最近 window 个观测
    中的位置」，只使用当期及之前的数据，不含未来函数。
    """
    def _rank(arr: np.ndarray) -> float:
        valid = arr[~np.isnan(arr)]
        if len(valid) < 2:
            return np.nan
        cur = arr[-1]
        if np.isnan(cur):
            return np.nan
        pct = (valid < cur).sum() / (len(valid) - 1) if len(valid) > 1 else np.nan
        return float(pct)

    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    min_periods = max(6, int(window * 0.4))
    for i in range(len(values)):
        start = max(0, i - window + 1)
        chunk = values[start : i + 1]
        if np.sum(~np.isnan(chunk)) < min_periods:
            continue
        out[i] = _rank(chunk)
    result = pd.Series(out, index=series.index)
    return result if ascending else 1.0 - result


def piecewise_linear(x: float, points: Sequence[tuple[float, float]]) -> float:
    """分段线性插值。points 需按 x 升序，如 [(0,0.1),(30,0.3),(100,1.0)]。"""
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return float("nan")
    pts = sorted(points, key=lambda p: p[0])
    if x <= pts[0][0]:
        return float(pts[0][1])
    if x >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return float(y1)
            w = (x - x0) / (x1 - x0)
            return float(y0 + w * (y1 - y0))
    return float(pts[-1][1])


def linear_score(value: float, bad: float, good: float, higher_is_better: bool = True) -> float:
    """把原始值线性映射到 0~100 分。

    higher_is_better=True：value>=good 得 100，value<=bad 得 0。
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return float("nan")
    lo, hi = (bad, good) if higher_is_better else (good, bad)
    if higher_is_better:
        if good == bad:
            return 50.0
        return float(np.clip((value - bad) / (good - bad) * 100.0, 0.0, 100.0))
    if good == bad:
        return 50.0
    return float(np.clip((bad - value) / (bad - good) * 100.0, 0.0, 100.0))


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return float("nan")
    return float(np.clip(value, low, high))


def to_month_end(series: pd.Series) -> pd.Series:
    """把索引统一成月末时点（月度宏观数据的标准对齐方式）。"""
    s = series.copy()
    s.index = pd.to_datetime(s.index)
    return s


def period_to_offset(freq: str) -> str:
    """调仓频率转 pandas 频率别名。"""
    return {"M": "ME", "Q": "QE", "A": "YE", "Y": "YE"}.get(freq.upper(), "QE")


# --------------------------------------------------------------------------- #
# 周期识别（PPI 规则的核心）
# --------------------------------------------------------------------------- #
def find_cycle_high_low(
    series: pd.Series, lookback_months: int = 60
) -> tuple[float, float, int, int]:
    """在最近 lookback 个月的窗口里找 PPI 同比的周期高点与低点。

    返回 (高点值, 低点值, 高点位置, 低点位置)，位置是序列内的整数下标。
    设计取舍：不用 scipy 的 argrelextrema（对噪声敏感、需要调参），
    直接用窗口极值——宏观同比序列本身已足够平滑。
    """
    s = series.dropna()
    if len(s) < 6:
        return float("nan"), float("nan"), -1, -1
    window = s.iloc[-lookback_months:] if len(s) > lookback_months else s
    hi_pos = int(np.argmax(window.to_numpy(dtype=float)))
    lo_pos = int(np.argmin(window.to_numpy(dtype=float)))
    offset = len(s) - len(window)
    return (
        float(window.iloc[hi_pos]),
        float(window.iloc[lo_pos]),
        hi_pos + offset,
        lo_pos + offset,
    )


def retracement_ratio(current: float, cycle_high: float, cycle_low: float) -> float:
    """PPI 同比从周期高点回撤的比例。

    r = (高点 - 当前) / (高点 - 低点)
      r≈0   → 还在周期高点附近
      r≈0.5 → 下跌到一半（用户规则里的最优买点）
      r≈1   → 已到周期底部（用户规则里的见顶警告）
    若高点低点几乎相等（无波动周期），返回 nan。
    """
    if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in (current, cycle_high, cycle_low)):
        return float("nan")
    span = cycle_high - cycle_low
    if abs(span) < 1e-6:
        return float("nan")
    return float((cycle_high - current) / span)


def yoy_from_index(series: pd.Series, months: int = 12) -> pd.Series:
    """由定基指数序列推导同比（%）。"""
    return (series / series.shift(months) - 1.0) * 100.0


def rolling_volatility(series: pd.Series, window: int = 12) -> pd.Series:
    return series.rolling(window, min_periods=max(3, window // 2)).std()


__all__ = [
    "LOG",
    "setup_logging",
    "build_proxies",
    "apply_proxy_env",
    "Cache",
    "cached_fetch",
    "safe_pct_change",
    "roll_percentile",
    "piecewise_linear",
    "linear_score",
    "clamp",
    "to_month_end",
    "period_to_offset",
    "find_cycle_high_low",
    "retracement_ratio",
    "yoy_from_index",
    "rolling_volatility",
]
