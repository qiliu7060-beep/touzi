"""个股买卖规则引擎——把「市盈率/市净率/股息率」变成逐条硬门槛，并给出卖出条件。

## 为什么要有这个模块

``touzi/screener.py`` 算出来的「总分」是**加权平均**：估值 45% + 质量 30% +
股息 15% + 政策 10%。加权平均适合排序，但不适合做买卖决定，因为它可以
「补偿」——一只 PB 高达 8 倍、股息率只有 0.5% 的股票，只要 ROE 足够高、
行业正好在政策目录里，总分照样能到 80 分以上。用户看到的正是这个问题：
「你这个没有结合市盈率、市净率和股息率」。

所以本模块把三个估值指标从「加权里的一项」提升为**一票否决的门槛**：

* **买入**是 **AND**——``[plan]`` 里每一条 ``buy_*`` 都必须满足。
  任何一条不满足，输出里会写明是哪一条、差多少。
* **卖出**是 **OR**——任意一条 ``sell_*`` 触发就给出减仓/清仓建议，
  并写明触发的是哪一条、当时的数值是多少。

每条规则都带 ``value``（实际值）与 ``threshold``（阈值），因此建议是
**可核查**的：把阈值改掉重跑，理由文本会跟着变，不存在模糊表述。

## 规则清单（阈值全部在 ``config/settings.toml`` 的 ``[plan]`` 段）

买入门槛（全部满足才建仓）：

===============  ==================================================
总分 ≥            72（A 级）
质量分 ≥          60（便宜但烂的公司不买）
PE 历史分位 ≤     40%（比自身近 5 年 60% 的时间都便宜）
PB 历史分位 ≤     40%
股息率 ≥          2.0%（真实现金回报）
PE 绝对值 ≤       40（防止分位失真——常年高估的股票「回到中位」仍很贵）
PE × PB ≤         50（格雷厄姆门槛的 A 股化版本；经典 22.5 见 [valuation]）
宏观背景分 ≥      45（大盘环境太差时先等）
候选池排名前      默认关闭（相对候选池算分位，池子大小一变门槛含义就变）
===============  ==================================================

差 1 条以内 → ``接近建仓``（``[plan] buy_near_miss``，默认 1）；差得更多 → ``观望``。
出局级条件一旦触发，无论差几条都是 ``回避``。

卖出条件（任一触发）：

=======================  ==========  ======================================
条件                     级别        含义
PE 分位 ≥ 80%            清仓        回到自身历史高位
PB 分位 ≥ 80%            清仓
PE × PB ≥ 120            清仓        进入泡沫区
PE ≥ 60                  清仓
股息率 ≤ 1.0%            清仓        分红能力下降或股价涨太高
总分 ≤ 62（B 级）        清仓        当初的买入理由不再成立
总分 ≤ 67                减仓
宏观背景分 ≤ 30          减仓        整体转防御
宏观背景分 ≤ 40          减仓
浮亏 ≤ −25%              清仓        风控止损
浮盈 ≥ +150%             减仓        落袋（长期投资也要有纪律）
现金流/净利润 ≤ 0.5      预警        利润没有现金支撑
ROE 连续 2 年下滑        预警        盈利能力走弱
净利润连续 2 年为负      预警
=======================  ==========  ======================================

「预警」不单独触发动作，但会写进理由，并在跟踪表里高亮。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = [
    "Check",
    "PlanDecision",
    "BuyPlan",
    "evaluate_buy",
    "evaluate_sell",
    "decide",
    "plan_settings",
    "format_checks",
]

# 卖出检查的级别
EXIT = "清仓"
REDUCE = "减仓"
WARN = "预警"


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

# 这些键必须存在；缺失说明 settings.toml 的 [plan] 段被删坏了
_REQUIRED = (
    "buy_min_total", "buy_min_quality", "buy_max_pe_percentile", "buy_max_pb_percentile",
    "buy_min_dividend_yield", "buy_max_pe_abs", "buy_max_graham", "buy_min_macro_score",
    "sell_pe_percentile", "sell_pb_percentile", "sell_graham", "sell_pe_abs",
    "sell_dividend_yield", "sell_total", "sell_reduce_total",
    "sell_macro_score", "sell_macro_reduce_score",
    "sell_stop_loss", "sell_take_profit", "sell_cashflow_floor",
)


def plan_settings(cfg) -> dict[str, Any]:
    """取出 ``[plan]`` 段并补齐可选键。

    ``cfg`` 是 :class:`touzi.config.Config`；``[plan]`` 段用 ``.get`` 取，
    因为 ``Config.__getattr__`` 对缺失段会抛 ``AttributeError``。
    """
    raw = dict(cfg.get("plan", {}) or {})
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise KeyError(
            f"config/settings.toml 的 [plan] 段缺少这些键：{missing}；"
            "请对照仓库里的默认配置补齐。"
        )
    raw.setdefault("buy_max_rank_pct", 0.0)
    for key, threshold in {"sell_debt_equity": .8, "sell_equity_nonpositive": 0., "sell_capex_to_profit": .25,
                           "sell_annual_loss": 0., "sell_cumulative_loss": 0.}.items():
        raw.setdefault(key, threshold)
    raw.setdefault("buy_near_miss", 1)
    raw.setdefault("holdings_file", "config/holdings.csv")
    raw.setdefault("tracking_file", "output/tracking.csv")
    raw.setdefault("sell_negative_growth_years", 2)
    raw.setdefault("tracking_top", 40)
    return raw


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Check:
    """一条可核查的规则判定。

    Attributes:
        key: 稳定标识，如 ``"pe_percentile"``。
        label: 人类可读的规则描述，如 ``"PE 历史分位 ≤ 40%"``。
        hit: **买入检查里 = 满足门槛；卖出检查里 = 触发卖出**。
        value: 实际值（可能是 ``nan`` 或 ``None``）。
        threshold: 阈值。
        level: 仅卖出检查用——``清仓`` / ``减仓`` / ``预警``。
        note: 补充说明（例如数据缺失）。
    """

    key: str
    label: str
    hit: bool
    value: Any = None
    threshold: Any = None
    level: str = ""
    note: str = ""
    known: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "hit": bool(self.hit),
            "value": None if _is_nan(self.value) else self.value,
            "threshold": self.threshold,
            "level": self.level,
            "note": self.note,
            "known": bool(self.known),
        }

    def text(self) -> str:
        mark = "?" if not self.known else ("✔" if self.hit else "✘")
        val = "—" if _is_nan(self.value) else _fmt(self.value)
        thr = "—" if _is_nan(self.threshold) else _fmt(self.threshold)
        s = f"{mark} {self.label}（实际 {val}，门槛 {thr}）"
        if self.note:
            s += f"　{self.note}"
        return s


@dataclass
class PlanDecision:
    """一只股票的最终买卖建议。"""

    code: str
    name: str
    action: str
    buy_checks: list[Check] = field(default_factory=list)
    sell_checks: list[Check] = field(default_factory=list)
    holding: dict[str, Any] | None = None
    pnl: float | None = None
    total: float = float("nan")
    macro_score: float = float("nan")
    reasons: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- 统计
    @property
    def buy_pass(self) -> bool:
        return bool(self.buy_checks) and all(c.hit for c in self.buy_checks)

    @property
    def buy_hits(self) -> int:
        return sum(1 for c in self.buy_checks if c.hit)

    @property
    def failed_checks(self) -> list[Check]:
        return [c for c in self.buy_checks if not c.hit]

    @property
    def triggered(self) -> list[Check]:
        return [c for c in self.sell_checks if c.hit]

    def by_level(self, level: str) -> list[Check]:
        return [c for c in self.sell_checks if c.hit and c.level == level]

    # ---------------------------------------------------------------- 输出
    def as_dict(self) -> dict[str, Any]:
        return {
            "代码": self.code,
            "名称": self.name,
            "建议动作": self.action,
            "买入门槛通过": f"{self.buy_hits}/{len(self.buy_checks)}",
            "未通过的门槛": "；".join(c.label for c in self.failed_checks) or "",
            "触发的卖出条件": "；".join(f"[{c.level}] {c.label}" for c in self.triggered) or "",
            "总分": None if _is_nan(self.total) else round(float(self.total), 2),
            "宏观背景分": None if _is_nan(self.macro_score) else round(float(self.macro_score), 2),
            "持仓成本": None if not self.holding else self.holding.get("买入价"),
            "浮动盈亏%": None if self.pnl is None else round(self.pnl * 100.0, 2),
            "理由": " ｜ ".join(self.reasons),
        }

    def explain_text(self) -> str:
        head = f"【{self.code} {self.name}】建议动作：{self.action}"
        if self.holding:
            cost = self.holding.get("买入价")
            head += f"　（持仓成本 {cost}）"
            if self.pnl is not None:
                head += f"　浮动盈亏 {self.pnl * 100:+.2f}%"
        lines = [head]
        if self.reasons:
            lines.append("  判断理由：" + "；".join(self.reasons))
        lines.append(f"  买入门槛（{self.buy_hits}/{len(self.buy_checks)} 通过，必须全部通过）：")
        lines += ["    " + c.text() for c in self.buy_checks]
        trig = self.triggered
        lines.append(f"  卖出条件（{len(trig)} 条触发，任意一条即给建议）：")
        if trig:
            lines += ["    " + c.text() for c in trig]
        else:
            lines.append("    ✔ 无触发")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 取值助手
# ---------------------------------------------------------------------------


def _is_nan(v: Any) -> bool:
    if v is None:
        return True
    try:
        return bool(np.isnan(float(v)))
    except (TypeError, ValueError):
        return False


def _fmt(v: Any) -> str:
    if isinstance(v, str):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f) >= 1000:
        return f"{f:,.0f}"
    if abs(f - round(f)) < 1e-9:
        return f"{f:.0f}"
    return f"{f:.2f}"


def _detail(score, part: str, key: str) -> Any:
    """从 :class:`touzi.screener.StockScore` 的分项里取 ``detail`` 字段。"""
    if score is None:
        return None
    parts = getattr(score, "parts", None) or {}
    res = parts.get(part)
    if res is None:
        return None
    detail = getattr(res, "detail", None) or {}
    return detail.get(key)


def _part_score(score, part: str) -> float:
    parts = getattr(score, "parts", None) or {}
    res = parts.get(part)
    if res is None:
        return float("nan")
    try:
        return float(res.score)
    except (TypeError, ValueError):
        return float("nan")


def _attr(score, name: str) -> Any:
    return getattr(score, name, None) if score is not None else None


# ---------------------------------------------------------------------------
# 买入规则
# ---------------------------------------------------------------------------


def evaluate_buy(score, cfg, macro_score: float = float("nan"),
                 rank_pct: float | None = None) -> list[Check]:
    """逐条核查买入门槛。返回的 ``Check.hit=True`` 表示**这一条满足了**。"""
    p = plan_settings(cfg)
    checks: list[Check] = []

    def add(key, label, value, threshold, ok, note="", known=True):
        checks.append(Check(key=key, label=label, hit=bool(ok) and known, value=value,
                            threshold=threshold, note=note, known=known))

    total_raw = _attr(score, "total")
    total = float(total_raw) if total_raw is not None else float("nan")
    quality = _part_score(score, "quality")
    pe = _attr(score, "pe_ttm")
    pb = _attr(score, "pb")
    dy = _attr(score, "dividend_yield")
    pe_pctl = _detail(score, "valuation", "pe_percentile")
    pb_pctl = _detail(score, "valuation", "pb_percentile")
    pe_source = _detail(score, "valuation", "pe_percentile_source")
    pb_source = _detail(score, "valuation", "pb_percentile_source")

    # 1. 总分
    thr = float(p["buy_min_total"])
    add("total", f"总分 ≥ {_fmt(thr)}（A 级）", total, thr, np.isfinite(total) and total >= thr,
        "总分数据缺失" if not np.isfinite(total) else "", np.isfinite(total))

    # 2. 质量分
    thr = float(p["buy_min_quality"])
    add("quality", f"质量分 ≥ {_fmt(thr)}（便宜但烂的公司不买）", quality, thr,
        np.isfinite(quality) and quality >= thr, "" if np.isfinite(quality) else "质量数据缺失",
        np.isfinite(quality))

    # 3. PE 历史分位
    thr = float(p["buy_max_pe_percentile"])
    pe_known = (pe_pctl is not None and np.isfinite(float(pe_pctl))
                and pe_source == "个股5年历史")
    add("pe_percentile", f"PE 处于自身历史分位 ≤ {thr:.0%}", pe_pctl if pe_known else None, thr,
        pe_known and float(pe_pctl) <= thr,
        "" if pe_known else f"自身历史分位缺失；{pe_source or '无替代数据'}不能代替", pe_known)

    # 4. PB 历史分位
    thr = float(p["buy_max_pb_percentile"])
    pb_known = (pb_pctl is not None and np.isfinite(float(pb_pctl))
                and pb_source == "个股5年历史")
    add("pb_percentile", f"PB 处于自身历史分位 ≤ {thr:.0%}", pb_pctl if pb_known else None, thr,
        pb_known and float(pb_pctl) <= thr,
        "" if pb_known else f"自身历史分位缺失；{pb_source or '无替代数据'}不能代替", pb_known)

    # 5. 股息率绝对水平
    thr = float(p["buy_min_dividend_yield"])
    add("dividend_yield", f"股息率 ≥ {_fmt(thr)}%", dy, thr,
        not _is_nan(dy) and float(dy) >= thr,
        "" if not _is_nan(dy) else "股息率数据缺失，不能据此认定不分红",
        not _is_nan(dy))

    # 6. PE 绝对水平
    thr = float(p["buy_max_pe_abs"])
    add("pe_abs", f"市盈率 ≤ {_fmt(thr)} 倍", pe, thr,
        not _is_nan(pe) and 0 < float(pe) <= thr,
        "PE 为负（亏损）" if not _is_nan(pe) and float(pe) <= 0 else
        ("PE 数据缺失" if _is_nan(pe) else ""), not _is_nan(pe))

    # 7. 格雷厄姆数
    thr = float(p["buy_max_graham"])
    gp = _detail(score, "valuation", "graham_product")
    if gp is None and not _is_nan(pe) and not _is_nan(pb) and float(pe) > 0 and float(pb) > 0:
        gp = float(pe) * float(pb)
    add("graham", f"PE × PB ≤ {_fmt(thr)}（格雷厄姆门槛）", gp, thr,
        gp is not None and np.isfinite(float(gp)) and float(gp) <= thr,
        "PE 或 PB 数据缺失" if _is_nan(pe) or _is_nan(pb) else "",
        not _is_nan(pe) and not _is_nan(pb))

    # 8. 宏观背景分
    thr = float(p["buy_min_macro_score"])
    add("macro", f"宏观背景分 ≥ {_fmt(thr)}（大盘环境太差时先等）", macro_score, thr,
        np.isfinite(macro_score) and macro_score >= thr,
        "宏观评分数据缺失" if not np.isfinite(macro_score) else "", np.isfinite(macro_score))

    # 9. 排名（可选）
    thr = float(p.get("buy_max_rank_pct") or 0.0)
    if thr > 0:
        add("rank", f"本次候选池内总分排名前 {thr:.0%}", rank_pct, thr,
            rank_pct is not None and np.isfinite(float(rank_pct)) and float(rank_pct) <= thr,
            "候选池排名数据缺失" if rank_pct is None else "",
            rank_pct is not None and np.isfinite(float(rank_pct)))
    return checks


# ---------------------------------------------------------------------------
# 卖出规则
# ---------------------------------------------------------------------------


def _declining(series: list[float], periods: int = 2) -> bool:
    """最近 ``periods`` 个年报是否**逐年下滑**（series 由老到新）。"""
    vals = [float(x) for x in (series or []) if x is not None and not _is_nan(x)]
    if len(vals) < periods + 1:
        return False
    recent = vals[-(periods + 1):]
    return all(recent[i + 1] < recent[i] for i in range(len(recent) - 1))


def _negative_streak(series: list[float], periods: int = 2) -> bool:
    """最近 ``periods`` 个年报的净利润增速是否**连续为负**。"""
    vals = [float(x) for x in (series or []) if x is not None and not _is_nan(x)]
    if len(vals) < periods:
        return False
    return all(v < 0 for v in vals[-periods:])


def evaluate_sell(score, cfg, macro_score: float = float("nan"),
                  holding: dict[str, Any] | None = None,
                  price: float | None = None) -> list[Check]:
    """逐条核查卖出条件。返回的 ``Check.hit=True`` 表示**这一条触发了**。"""
    p = plan_settings(cfg)
    checks: list[Check] = []

    def add(key, label, value, threshold, hit, level, note=""):
        checks.append(Check(key=key, label=label, hit=bool(hit), value=value,
                            threshold=threshold, level=level, note=note))

    total_raw = _attr(score, "total")
    total = float(total_raw) if total_raw is not None else float("nan")
    pe = _attr(score, "pe_ttm")
    pb = _attr(score, "pb")
    dy = _attr(score, "dividend_yield")
    pe_pctl = _detail(score, "valuation", "pe_percentile")
    pb_pctl = _detail(score, "valuation", "pb_percentile")
    pe_source = _detail(score, "valuation", "pe_percentile_source")
    pb_source = _detail(score, "valuation", "pb_percentile_source")
    graham = _detail(score, "valuation", "graham_product")
    ocf = _detail(score, "quality", "ocf_to_profit")
    roe_series = _detail(score, "quality", "roe_series") or []
    growth_series = _detail(score, "quality", "profit_growth_series") or []

    # ---- 估值止盈
    thr = float(p["sell_pe_percentile"])
    add("sell_pe_percentile", f"PE 回到自身历史分位 ≥ {thr:.0%}", pe_pctl, thr,
        pe_source == "个股5年历史" and pe_pctl is not None
        and np.isfinite(float(pe_pctl)) and float(pe_pctl) >= thr, EXIT,
        "" if pe_source == "个股5年历史" else "自身历史分位缺失，不能用截面分位触发清仓")

    thr = float(p["sell_pb_percentile"])
    add("sell_pb_percentile", f"PB 回到自身历史分位 ≥ {thr:.0%}", pb_pctl, thr,
        pb_source == "个股5年历史" and pb_pctl is not None
        and np.isfinite(float(pb_pctl)) and float(pb_pctl) >= thr, EXIT,
        "" if pb_source == "个股5年历史" else "自身历史分位缺失，不能用截面分位触发清仓")

    # ---- 绝对估值过高
    thr = float(p["sell_graham"])
    if graham is None and not _is_nan(pe) and not _is_nan(pb) and float(pe) > 0 and float(pb) > 0:
        graham = float(pe) * float(pb)
    add("sell_graham", f"PE × PB ≥ {_fmt(thr)}（泡沫区）", graham, thr,
        graham is not None and np.isfinite(float(graham)) and float(graham) >= thr, EXIT)

    thr = float(p["sell_pe_abs"])
    add("sell_pe_abs", f"市盈率 ≥ {_fmt(thr)} 倍", pe, thr,
        not _is_nan(pe) and 0 < float(pe) >= thr, EXIT,
        "PE 为负（亏损）" if not _is_nan(pe) and float(pe) <= 0 else "")

    # ---- 股息率塌陷
    thr = float(p["sell_dividend_yield"])
    dy_low = (not _is_nan(dy)) and float(dy) < thr
    add("sell_dividend_yield", f"股息率 < {_fmt(thr)}%", dy, thr, dy_low, EXIT,
        "股息率数据缺失，不能据此认定已不分红" if _is_nan(dy) else "")

    # ---- 总分掉档
    thr = float(p["sell_total"])
    add("sell_total", f"总分 ≤ {_fmt(thr)}（B 级，买入理由不再成立）", total, thr,
        np.isfinite(total) and total <= thr, EXIT)

    thr = float(p["sell_reduce_total"])
    add("sell_reduce_total", f"总分 ≤ {_fmt(thr)}（压力位，先减一半）", total, thr,
        np.isfinite(total) and total <= thr, REDUCE)

    # ---- 宏观
    thr = float(p["sell_macro_score"])
    add("sell_macro_score", f"宏观背景分 ≤ {_fmt(thr)}（整体转防御）", macro_score, thr,
        np.isfinite(macro_score) and macro_score <= thr, REDUCE)

    thr = float(p["sell_macro_reduce_score"])
    add("sell_macro_reduce_score", f"宏观背景分 ≤ {_fmt(thr)}（偏防御）", macro_score, thr,
        np.isfinite(macro_score) and macro_score <= thr, REDUCE)

    # ---- 风控（需要持仓成本）
    cost = None
    if holding:
        try:
            cost = float(holding.get("买入价"))
        except (TypeError, ValueError):
            cost = None
    pnl = None
    if cost and price is not None and not _is_nan(price) and float(price) > 0:
        pnl = float(price) / cost - 1.0

    thr = float(p["sell_stop_loss"])
    add("sell_stop_loss", f"相对成本浮亏 ≤ {thr:.0%}（风控止损）",
        None if pnl is None else pnl, thr,
        pnl is not None and pnl <= thr, EXIT,
        "" if pnl is not None else "无持仓成本或价格，无法计算")

    thr = float(p["sell_take_profit"])
    add("sell_take_profit", f"相对成本浮盈 ≥ {thr:.0%}（落袋为安）",
        None if pnl is None else pnl, thr,
        pnl is not None and pnl >= thr, REDUCE,
        "" if pnl is not None else "无持仓成本或价格，无法计算")

    # ---- 基本面恶化
    thr = float(p["sell_cashflow_floor"])
    add("sell_cashflow", f"经营现金流/净利润 ≤ {_fmt(thr)} 倍",
        ocf, thr,
        ocf is not None and np.isfinite(float(ocf)) and float(ocf) <= thr, WARN,
        "" if ocf is not None else "现金流数据缺失")

    n = int(p.get("sell_negative_growth_years") or 2)
    add("sell_roe_decline", f"ROE 连续 {n} 个年报下滑",
        "→".join(_fmt(x) for x in list(roe_series)[-(n + 1):]) or None, f"连降 {n} 年",
        _declining(roe_series, n), WARN,
        "" if len(list(roe_series)) >= n + 1 else "年报样本不足")

    add("sell_growth_negative", f"净利润增速连续 {n} 个年报为负",
        "→".join(_fmt(x) for x in list(growth_series)[-n:]) or None, f"连负 {n} 年",
        _negative_streak(growth_series, n), WARN,
        "" if len(list(growth_series)) >= n else "年报样本不足")

    # 公共卖出快照必须区分“确认未触发”和“根本无法核验”。
    def finite(value):
        try:
            return value is not None and bool(np.isfinite(float(value)))
        except (TypeError, ValueError):
            return False

    known = {
        "sell_pe_percentile": pe_source == "个股5年历史" and finite(pe_pctl),
        "sell_pb_percentile": pb_source == "个股5年历史" and finite(pb_pctl),
        "sell_graham": finite(graham),
        "sell_pe_abs": finite(pe),
        "sell_dividend_yield": finite(dy),
        "sell_total": finite(total),
        "sell_reduce_total": finite(total),
        "sell_macro_score": finite(macro_score),
        "sell_macro_reduce_score": finite(macro_score),
        "sell_stop_loss": pnl is not None,
        "sell_take_profit": pnl is not None,
        "sell_cashflow": finite(ocf),
        "sell_roe_decline": sum(finite(v) for v in roe_series) >= n + 1,
        "sell_growth_negative": sum(finite(v) for v in growth_series) >= n,
    }
    analysis = _detail(score, "quality", "financial_analysis") or {}
    metrics = analysis.get("metrics", {})
    debt = metrics.get("debt_equity", {})
    fresh_analysis = analysis.get("status") not in ("stale", "missing")
    for key, label, metric_name, field, comparison in (
        ("sell_debt_equity", "债务股权比率 ≥ 0.8，提示复核", "debt_equity", "value", "ge"),
        ("sell_equity_nonpositive", "合并股东权益非正，提示复核", "debt_equity", "total_equity", "le"),
        ("sell_capex_to_profit", "五年资本开支/净利润 ≥ 25%，提示复核", "capex_to_profit", "value", "ge"),
        ("sell_annual_loss", "最新完整年度净利润非正，提示复核", "annual_net_profit", "value", "le"),
        ("sell_cumulative_loss", "五年累计净利润非正，提示复核", "cumulative_net_profit", "value", "le"),
    ):
        metric = metrics.get(metric_name, {})
        value = metric.get(field)
        eligible = metric.get("status") == "ok" or (key == "sell_equity_nonpositive" and debt.get("status") == "invalid")
        is_known = fresh_analysis and eligible and finite(value)
        threshold = float(p[key])
        hit = is_known and (float(value) >= threshold if comparison == "ge" else float(value) <= threshold)
        add(key, label, value, threshold, hit, WARN,
            "" if is_known else "财务数据：" + str(metric.get("status", "missing")))
        known[key] = bool(is_known)
    for check in checks:
        check.known = known.get(check.key, True)
        if not check.known and not check.note:
            check.note = "数据缺失，未核验"

    return checks


# ---------------------------------------------------------------------------
# 综合决策
# ---------------------------------------------------------------------------


def decide(score, cfg, macro_score: float = float("nan"),
           holding: dict[str, Any] | None = None,
           rank_pct: float | None = None,
           price: float | None = None) -> PlanDecision:
    """把买入门槛与卖出条件合成一个明确的动作建议。

    动作取值：

    * 已持仓 → ``清仓`` / ``减仓`` / ``持有`` / ``加仓``
    * 未持仓 → ``建仓`` / ``接近建仓`` / ``观望`` / ``回避``

    ``接近建仓`` = 买入门槛只差 ``[plan] buy_near_miss`` 条以内（默认 1 条）。
    它不是「可以买了」，而是「值得盯着的下一批」，让跟踪表有可操作的分层。
    """
    buy = evaluate_buy(score, cfg, macro_score=macro_score, rank_pct=rank_pct)
    px = price if price is not None else _attr(score, "price")
    sell = evaluate_sell(score, cfg, macro_score=macro_score, holding=holding, price=px)

    total_raw = _attr(score, "total")
    total = float(total_raw) if total_raw is not None else float("nan")
    code = str(_attr(score, "code") or "")
    name = str(_attr(score, "name") or "")

    exit_hits = [c for c in sell if c.hit and c.level == EXIT]
    reduce_hits = [c for c in sell if c.hit and c.level == REDUCE]
    warn_hits = [c for c in sell if c.hit and c.level == WARN]
    buy_pass = all(c.hit for c in buy) if buy else False

    cost = None
    if holding:
        try:
            cost = float(holding.get("买入价"))
        except (TypeError, ValueError):
            cost = None
    pnl = None
    if cost and px is not None and not _is_nan(px) and float(px) > 0:
        pnl = float(px) / cost - 1.0

    reasons: list[str] = []
    if holding:
        if exit_hits:
            action = "清仓"
            reasons.append("触发清仓条件：" + "；".join(c.label for c in exit_hits))
        elif reduce_hits:
            action = "减仓"
            reasons.append("触发减仓条件：" + "；".join(c.label for c in reduce_hits))
        elif buy_pass:
            action = "加仓"
            reasons.append("买入门槛全部通过，且无卖出条件触发，可继续加仓")
        else:
            action = "持有"
            reasons.append("卖出条件未触发，继续持有；买入门槛未全通过（"
                           + "；".join(c.label for c in buy if not c.hit) + "），不加仓")
    else:
        if exit_hits:
            action = "回避"
            reasons.append("触发出局级条件（估值过高或基本面转差），不建仓："
                           + "；".join(c.label for c in exit_hits))
        elif buy_pass:
            action = "建仓"
            reasons.append("买入门槛全部通过：" + "；".join(c.label for c in buy))
        else:
            missed = [c for c in buy if not c.hit]
            unknown = [c for c in missed if not c.known]
            slack = int(plan_settings(cfg).get("buy_near_miss") or 0)
            if unknown:
                action = "观望"
                reasons.append("买入数据待核验：" + "；".join(c.label for c in unknown)
                               + "；不能算作仅差一项已知条件")
            elif len(missed) <= slack:
                action = "接近建仓"
                reasons.append(
                    f"买入门槛只差 {len(missed)} 条"
                    + (f"（容差 {slack} 条）" if slack else "")
                    + "：" + "；".join(c.label for c in missed)
                )
            else:
                action = "观望"
                reasons.append(f"未通过 {len(missed)} 条门槛："
                               + "；".join(c.label for c in missed))

    for c in warn_hits:
        reasons.append(f"预警：{c.label}（实际 {_fmt(c.value)}）")

    return PlanDecision(
        code=code, name=name, action=action,
        buy_checks=buy, sell_checks=sell,
        holding=holding, pnl=pnl, total=total, macro_score=macro_score,
        reasons=reasons,
    )


def format_checks(checks: list[Check], only_missed: bool = False) -> str:
    """把检查列表渲染成多行文本（给报告与「逐项理由」用）。"""
    return "\n".join(c.text() for c in checks if not (only_missed and c.hit))
