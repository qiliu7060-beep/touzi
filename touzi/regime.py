"""宏观背景分与目标仓位：把 5 条宏观规则合成一个 0~100 的「市场背景分」。

## 它回答的问题

个股打分回答「买什么」，这个模块回答「现在该用多少仓位买」。
用户提的 7 条规则里，有 6 条（PMI / PPI / CPI / GDP / 美联储潮汐 / 政策红利）
本质上都是**大盘择时**变量，而不是选股变量。把它们塞进个股分会让同一只股票
在宏观转好时莫名其妙地"变便宜"，所以这里单独成一层：

    宏观背景分(0~100) --分段线性--> 目标股票仓位(0.10~1.00)

## 权重为什么这么排

    pmi 0.30 > ppi 0.25 > fed 0.20 > cpi 0.15 > gdp 0.10

- **PMI 最高**：月度、及时、且是用户明确点名「大幅度超过 50 或处于上升阶段」
  的规则，对 A 股的短期方向最敏感。
- **PPI 次高**：用户给出了最具体的一条形态判断（跌到一半领先上涨、到底部见顶），
  且 PPI 直接对应企业盈利，是「盈利周期」的代理变量。
- **美联储潮汐第三**：影响的是外资流向与全球风险偏好，对 A 股是间接作用，
  且近年内资定价权上升，所以低于国内基本面。
- **CPI 较低**：用户规则本身成立，但中国 CPI 长期低位、波动小，区分度弱于 PPI。
- **GDP 最低**：季度低频、且滞后公布，信息在公布时已被消化。

## 缺失项如何处理

某个模块拿不到数据时，**不是当 0 分**（那会把背景分无端压低、逼出错误减仓），
而是在可用模块之间重新归一化权重，并把缺失项写进 ``unavailable``。

## 防未来函数

``as_of`` 给定时，所有原始序列先截断到该时点再计算；宏观序列本身已在
``macro_cn.build_cn_macro_panel`` 里按公布滞后推后过时点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .data.macro_cn import build_cn_macro_panel
from .data.macro_us import fetch_fed_panel
from .signals.fed import score_fed
from .signals.macro import RuleResult, score_cpi, score_gdp, score_pmi, score_ppi
from .util import piecewise_linear

__all__ = ["MacroContext", "build_macro_context", "MACRO_KEYS"]

MACRO_KEYS = ("pmi", "ppi", "fed", "cpi", "gdp")

MACRO_LABELS = {
    "pmi": "PMI（制造业景气）",
    "ppi": "PPI（工业品价格/盈利周期）",
    "fed": "美联储潮汐（全球流动性）",
    "cpi": "CPI（通胀速度）",
    "gdp": "GDP（经济增长）",
}


def _ladder_position(score: float, ladder: Any) -> float:
    """按 [position.ladder] 做分段线性插值，把背景分映射成目标仓位。"""
    pts: list[tuple[float, float]] = []
    for item in ladder:
        if isinstance(item, dict):
            pts.append((float(item["score"]), float(item["position"])))
        else:  # 兼容 [score, position] 列表写法
            pts.append((float(item[0]), float(item[1])))
    if not pts:
        return float("nan")
    return float(piecewise_linear(score, sorted(pts)))


def _regime_label(score: float) -> str:
    if not np.isfinite(score):
        return "数据不足"
    if score >= 75:
        return "进攻（宏观顺风，可满仓）"
    if score >= 60:
        return "偏积极（顺风，仓位偏高）"
    if score >= 45:
        return "中性（均衡配置）"
    if score >= 30:
        return "防御（逆风，降仓）"
    return "保守（强逆风，低仓）"


@dataclass
class MacroContext:
    """宏观背景分的完整结果。"""

    as_of: pd.Timestamp
    results: dict[str, RuleResult]
    weights: dict[str, float]
    macro_score: float
    position: float
    regime: str
    ladder: list[tuple[float, float]] = field(default_factory=list)
    staleness: dict[str, Any] = field(default_factory=dict)
    unavailable: list[str] = field(default_factory=list)
    fed_panel: pd.DataFrame | None = None

    def to_frame(self) -> pd.DataFrame:
        rows = []
        for key in MACRO_KEYS:
            res = self.results.get(key)
            if res is None:
                continue
            rows.append({
                "模块": MACRO_LABELS.get(key, key),
                "权重": round(self.weights.get(key, 0.0), 3),
                "得分": None if not np.isfinite(res.score) else round(float(res.score), 1),
                "状态": res.state,
                "说明": res.explain,
            })
        return pd.DataFrame(rows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": str(pd.Timestamp(self.as_of).date()),
            "macro_score": None if not np.isfinite(self.macro_score) else round(self.macro_score, 2),
            "position": None if not np.isfinite(self.position) else round(self.position, 3),
            "regime": self.regime,
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
            "unavailable": list(self.unavailable),
            "rules": {k: v.as_dict() for k, v in self.results.items()},
            "staleness": self.staleness,
        }


def build_macro_context(
    cfg,
    cache=None,
    proxy: str | None = None,
    as_of: pd.Timestamp | str | None = None,
) -> MacroContext:
    """取宏观数据 → 算 5 条规则 → 合成背景分与目标仓位。"""
    import logging

    log = logging.getLogger(__name__)
    if proxy is None:
        proxy = cfg.general.get("proxy") or None
    as_of_ts = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.today()

    from .util import resolve_ttls

    ttls = resolve_ttls(cfg)
    panel = build_cn_macro_panel(cache, proxy, start=cfg.general.get("start_date"),
                                 ttl_days=ttls["macro"], bond_ttl_days=ttls["history"])
    aligned = panel["aligned"]

    def _truncate(obj):
        """把序列/表截断到 as_of，防未来函数。"""
        if obj is None or len(obj) == 0:
            return obj
        return obj[obj.index <= as_of_ts]

    pmi_df = _truncate(aligned["pmi"])
    cpi_s = _truncate(aligned["cpi"])
    ppi_s = _truncate(aligned["ppi"])
    gdp_s = _truncate(aligned["gdp"])
    bond_s = _truncate(aligned["bond_10y"])

    fed_panel = None
    try:
        fed_panel = fetch_fed_panel(cache, proxy, ttl_days=ttls["us"])
        fed_panel = _truncate(fed_panel)
    except Exception as exc:
        log.warning("美联储数据抓取失败，fed 模块将缺席：%s", exc)

    results: dict[str, RuleResult] = {}
    if pmi_df is not None and len(pmi_df):
        results["pmi"] = score_pmi(
            pmi_df["manufacturing"],
            cfg,
            pmi_df["non_manufacturing"] if "non_manufacturing" in getattr(pmi_df, "columns", []) else None,
        )
    if ppi_s is not None and len(ppi_s):
        results["ppi"] = score_ppi(ppi_s, cfg)
    if cpi_s is not None and len(cpi_s):
        results["cpi"] = score_cpi(cpi_s, cfg)
    if gdp_s is not None and len(gdp_s):
        results["gdp"] = score_gdp(gdp_s, cfg)
    if fed_panel is not None and len(fed_panel):
        results["fed"] = score_fed(fed_panel, cfg, china_10y=bond_s)

    # ---------------- 加权合成：缺失项不按 0 计，而是在可用项之间重新归一 ----------------
    raw_weights = dict(cfg.macro_weights)
    total_w = 0.0
    acc = 0.0
    used: dict[str, float] = {}
    unavailable: list[str] = []
    for key in MACRO_KEYS:
        res = results.get(key)
        w = float(raw_weights.get(key, 0.0))
        if res is None or not np.isfinite(res.score) or w <= 0:
            unavailable.append(key)
            continue
        acc += w * float(res.score)
        total_w += w
        used[key] = w
    macro_score = (acc / total_w) if total_w > 0 else float("nan")
    # 把权重归一化到 1，便于报告里展示
    norm_weights = {k: v / total_w for k, v in used.items()} if total_w > 0 else {}
    for k in unavailable:
        norm_weights.setdefault(k, 0.0)

    position = _ladder_position(macro_score, cfg.position["ladder"]) if np.isfinite(macro_score) else float("nan")
    ladder_pts = [(float(d["score"]), float(d["position"])) for d in cfg.position["ladder"]]

    ctx = MacroContext(
        as_of=as_of_ts,
        results=results,
        weights=norm_weights,
        macro_score=macro_score,
        position=position,
        regime=_regime_label(macro_score),
        ladder=ladder_pts,
        staleness=panel.get("staleness", {}),
        unavailable=unavailable,
        fed_panel=fed_panel,
    )
    log.info("宏观背景分 %.1f → 目标仓位 %.0f%%（%s）", macro_score, position * 100, ctx.regime)
    return ctx


# --------------------------------------------------------------------------- #
# 数据新鲜度体检：讲清楚「哪一块是实时的，哪一块天然滞后」
# --------------------------------------------------------------------------- #
_ITEM_LABEL = {
    "quote": "行情快照（价格 / PE-TTM / PB / 市值）",
    "index": "沪深300 指数日线（回测基准）",
    "bond": "中国 10 年期国债收益率",
    "pmi": "PMI（制造业景气）",
    "cpi": "CPI（通胀）",
    "ppi": "PPI（工业品价格）",
    "gdp": "GDP（经济增长）",
    "fed": "美联储潮汐（美债 / 美元指数 / VIX）",
    "valuation": "个股估值历史（百度 PE / PB 五年）",
    "fundamental": "个股财务 / 分红（季度与事件驱动）",
}
# 数据本身的固有滞后（不是缓存造成的），报告里必须讲清楚，避免用户误以为是实时
_INHERENT_NOTE = {
    "index": "日频，收盘后有新值；节假日期间会停留在上一个交易日（这是数据源的真实状态）",
    "bond": "日频，收盘后更新；它是市场数据，没有官方公布滞后",
    "pmi": "月度指标，当月最后一天由国家统计局公布，公布后本系统即可见",
    "cpi": "月度指标，次月中旬公布；本系统按 1 个月公布滞后对齐",
    "ppi": "月度指标，次月中旬公布；本系统按 1 个月公布滞后对齐",
    "gdp": "季度指标，季后约 3 周公布；本系统按 1 个月公布滞后对齐",
    "valuation": "日频，收盘后更新；实时模式下改用盘中快照价重算分位",
    "fundamental": "财报为季度披露、分红为事件披露，天生只有季度级新鲜度",
}


def _fmt_cycle(ttl_days: float) -> str:
    """把「天」为单位的 TTL 显示成人话。"""
    if ttl_days < 1 / 24:
        return f"{ttl_days * 1440:.0f} 分钟"
    if ttl_days < 1:
        return f"{ttl_days * 24:.0f} 小时"
    return f"{ttl_days:g} 天"


def data_freshness(cfg, cache, macro=None, quotes_at=None) -> list[dict[str, Any]]:
    """逐项列出「数据最新到哪里、缓存什么时候抓的、多久刷一次」。

    只做只读探测，任何一项失败都不影响整体运行。
    """
    from .util import is_live, resolve_ttls

    ttls = resolve_ttls(cfg)
    live = is_live(cfg)
    rows: list[dict[str, Any]] = []

    def _add(item: str, latest: Any, ttl: float, note: str = "") -> None:
        key = item
        fetched = None
        try:
            fetched = cache.saved_at(_CACHE_KEY_OF.get(key, "")) if cache is not None else None
        except Exception:
            fetched = None
        rows.append({
            "item": _ITEM_LABEL.get(key, key),
            "latest": "-" if latest is None or (isinstance(latest, float) and not np.isfinite(latest))
                      else str(latest),
            "fetched": fetched.strftime("%Y-%m-%d %H:%M") if fetched is not None else "-",
            "cycle": _fmt_cycle(ttl),
            "note": note or _INHERENT_NOTE.get(key, ""),
        })

    # 行情快照：腾讯接口不返回数据日期，只能报缓存抓取时刻
    snap_at = quotes_at
    if snap_at is None and cache is not None:
        snap_at = cache.saved_at("market_snapshot_tx")
    _add("quote",
         pd.Timestamp(snap_at).strftime("%Y-%m-%d %H:%M") if snap_at is not None else None,
         ttls["quote"],
         note=("盘中实时：每次运行都会重新抓快照，并用最新价重算估值分位"
               if live else "非实时模式：默认 1 天内直接复用缓存快照，不重新抓取"))

    # 指数日线
    try:
        ex = _last_index_date(cache, cfg)
        _add("index", ex, ttls["history"])
    except Exception:
        pass

    # 中国宏观
    if macro is not None and getattr(macro, "staleness", None):
        for key in ("pmi", "cpi", "ppi", "gdp"):
            info = macro.staleness.get(key)
            if not info:
                continue
            val = info.get("value")
            if isinstance(val, dict):
                val = "、".join(f"{k} {v}" for k, v in val.items() if v is not None)
            _add(key, f'{info.get("last_period", "-")}（{val}）', ttls["macro"])
    # 国债收益率单独一行：它没有公布滞后
    try:
        from .data.macro_cn import fetch_cn_bond_10y

        s = fetch_cn_bond_10y(cache, cfg.general.get("proxy") or None, ttl_days=ttls["history"])
        s = s.dropna()
        if len(s):
            _add("bond", f"{s.index[-1].date()}（{float(s.iloc[-1]):.2f}%）", ttls["history"])
    except Exception:
        pass

    # 美联储 / 美股宏观
    try:
        from .data.macro_us import fetch_yahoo_series

        s = fetch_yahoo_series("us_10y", cache, cfg.general.get("proxy") or None, ttl_days=ttls["us"])
        s = s.dropna()
        if len(s):
            _add("fed", f"{s.index[-1].date()}（美国10年 {float(s.iloc[-1]):.2f}%）", ttls["us"])
    except Exception:
        pass

    _add("valuation", None, ttls["history"])
    _add("fundamental", None, ttls["fund"])
    return rows


_CACHE_KEY_OF = {
    "quote": "market_snapshot_tx",
    "index": "index_sh000300",
    "bond": "cn_bond_10y",
    "fed": "yahoo_^TNX",
}


def _last_index_date(cache, cfg) -> Any:
    """从缓存里读沪深300 的最后一根日线日期（不再发网络请求）。"""
    from .data.stock import index_symbol

    key = f"index_{index_symbol(cfg.backtest.get('benchmark', 'sh000300'))}"
    df = cache.get(key, ttl_days=None, allow_stale=True)
    if df is None or len(df) == 0:
        return None
    col = "date" if "date" in df.columns else ("日期" if "日期" in df.columns else None)
    if col is None:
        return None
    return str(pd.to_datetime(df[col]).max().date())
