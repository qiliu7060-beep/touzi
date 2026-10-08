"""信号层：把原始宏观数据翻译成 0~100 的可解释评分。

设计原则
--------
1. **忠实于用户规则**：每条规则的判定逻辑与阈值都直接对应需求原话，
   并在 `explain` 里写清楚「为什么给这个分」，便于事后复盘。
2. **不含未来函数**：所有 T 时刻的分数只用 T 及之前的数据；宏观数据还额外
   做公布滞后对齐（PMI 月初公布上月值、GDP 季后公布），避免"提前知道"。
3. **可调参**：阈值来自 config/settings.toml，不写死在函数里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..util import (
    clamp,
    find_cycle_high_low,
    linear_score,
    piecewise_linear,
    retracement_ratio,
    safe_pct_change,
)


@dataclass
class RuleResult:
    """单条规则的评分结果。"""

    name: str
    score: float
    state: str
    detail: dict[str, Any] = field(default_factory=dict)
    explain: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.name,
            "score": None if self.score is None else round(float(self.score), 2),
            "state": self.state,
            "explain": self.explain,
            **{k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in self.detail.items()},
        }


def _last_valid(series: pd.Series) -> tuple[float, Any]:
    s = series.dropna()
    if s.empty:
        return float("nan"), None
    return float(s.iloc[-1]), s.index[-1]


def _val_at(series: pd.Series, months_ago: int) -> float:
    """取 months_ago 个月前的值（用于算变化量）。"""
    s = series.dropna()
    if len(s) <= months_ago:
        return float("nan")
    return float(s.iloc[-1 - months_ago])


# ===========================================================================
# 1) PMI 规则
# 用户原话：“GDP、PMI 与股市是正相关，当该值大幅度超过 50 或处于上升阶段，
#            股市大概率可能上涨。”
# 拆成「水平项」+「动量项」：
#   水平项：PMI 在 50 以上越高越好，50 以下越低越差
#   动量项：PMI 是否处于上升阶段（用 3 个月均值的斜率衡量，滤掉单月噪声）
# ===========================================================================
def score_pmi(
    pmi_manufacturing: pd.Series,
    cfg,
    pmi_non_manufacturing: pd.Series | None = None,
) -> RuleResult:
    cfgp = cfg.pmi
    # 合成 PMI：制造业为主，非制造业按配置权重折入
    pmi = pmi_manufacturing.dropna()
    if pmi.empty:
        return RuleResult("PMI", float("nan"), "数据缺失", explain="未取到 PMI 数据")

    if getattr(cfgp, "include_non_manufacturing", False) and pmi_non_manufacturing is not None:
        non = pmi_non_manufacturing.dropna()
        if not non.empty:
            w = float(cfgp.non_manufacturing_weight)
            aligned = pd.concat([pmi.rename("mfg"), non.rename("non")], axis=1).ffill()
            pmi = (aligned["mfg"] * (1 - w) + aligned["non"] * w).dropna()

    level_now, asof = _last_valid(pmi)
    expansion = float(cfgp.expansion_level)
    strong = float(cfgp.strong_level)

    # ---- 水平项 ----
    if level_now < expansion:
        # 50 以下：距荣枯线 4 个点以内线性给 0~50 分
        level_score = float(np.clip(50.0 * (level_now - (expansion - 4.0)) / 4.0, 0.0, 50.0))
    elif level_now >= strong:
        # “大幅度超过 50”：51.5 得 80 分，53 及以上满分
        level_score = float(np.clip(80.0 + 20.0 * (level_now - strong) / 1.5, 80.0, 100.0))
    else:
        # 50 ~ 51.5：50 分线性升到 80 分
        level_score = float(np.clip(50.0 + 30.0 * (level_now - expansion) / (strong - expansion), 50.0, 80.0))

    # ---- 动量项 ----
    lookback = int(cfgp.momentum_lookback)
    ma = pmi.rolling(lookback, min_periods=2).mean()
    delta = float("nan")
    if len(ma.dropna()) > lookback:
        delta = float(ma.iloc[-1] - ma.iloc[-1 - lookback])
    momentum_score = clamp(50.0 + delta * 25.0) if not np.isnan(delta) else float("nan")

    # 连续上行月数（环比）
    diffs = pmi.diff().dropna()
    consecutive_up = 0
    for d in reversed(diffs.to_list()):
        if d > 0:
            consecutive_up += 1
        else:
            break
    uptrend_need = int(cfgp.uptrend_months)
    trending_up = consecutive_up >= uptrend_need
    if trending_up and not np.isnan(momentum_score):
        momentum_score = clamp(momentum_score + 8.0)

    w_level = float(getattr(cfgp, "level_weight", 0.45))
    w_mom = float(getattr(cfgp, "momentum_weight", 0.55))
    if np.isnan(momentum_score):
        total = level_score
    else:
        total = w_level * level_score + w_mom * momentum_score

    if level_now >= strong and trending_up:
        state = f"强扩张（{level_now:.1f} 大幅高于 50，且连续 {consecutive_up} 个月上行）"
    elif level_now >= expansion and trending_up:
        state = f"扩张上行（{level_now:.1f}，连续 {consecutive_up} 个月上行）"
    elif level_now >= expansion:
        state = f"扩张走平（{level_now:.1f}）"
    else:
        state = f"收缩（{level_now:.1f}，低于荣枯线 {expansion:g}）"

    explain = (
        f"最新 PMI {level_now:.2f}（{asof:%Y-%m}）。"
        f"水平项 {level_score:.0f} 分（{'站上' if level_now >= expansion else '低于'}荣枯线 {expansion:g}，"
        f"强扩张门槛 {strong:g}）；"
        f"动量项 {momentum_score:.0f} 分（3 个月均值较 {lookback} 个月前变化 {delta:+.2f}，"
        f"连续上行 {consecutive_up} 个月）。"
        "规则依据：PMI 与股市正相关，大幅超过 50 或处于上升阶段时，股市大概率上涨。"
    )

    return RuleResult(
        name="PMI",
        score=clamp(total),
        state=state,
        detail={
            "pmi": round(level_now, 2),
            "asof": str(asof),
            "level_score": round(level_score, 1),
            "momentum_score": None if np.isnan(momentum_score) else round(momentum_score, 1),
            "ma_delta": None if np.isnan(delta) else round(delta, 3),
            "consecutive_up_months": consecutive_up,
        },
        explain=explain,
    )


# ===========================================================================
# 2) CPI 规则
# 用户原话：“CPI 涨得快利好股市和债市，涨得慢不利于股市和债市。”
# 拆成：
#   速度项（主逻辑）：CPI 同比的 3 个月变化，涨得越快分越高
#   水平项（风险约束）：温和通胀最好；通缩不利，滞胀重罚
# 另加滞胀熔断：CPI 高于过热线且仍在加速时，直接压分。
# ===========================================================================
def score_cpi(cpi_yoy: pd.Series, cfg) -> RuleResult:
    cfgc = cfg.cpi
    s = cpi_yoy.dropna()
    if s.empty:
        return RuleResult("CPI", float("nan"), "数据缺失", explain="未取到 CPI 数据")

    now, asof = _last_valid(s)
    fast = float(cfgc.fast_rise_3m)

    # ---- 速度项 ----
    prev3 = _val_at(s, 3)
    delta3 = now - prev3 if not np.isnan(prev3) else float("nan")
    if np.isnan(delta3):
        speed_score = float("nan")
    else:
        speed_score = linear_score(delta3, bad=-fast, good=fast)

    # ---- 水平项 ----
    lo = float(cfgc.sweet_spot_low)
    hi = float(cfgc.sweet_spot_high)
    defl = float(cfgc.deflation_line)
    over = float(cfgc.overheat_line)
    level_score = piecewise_linear(
        now,
        [
            (defl - 1.5, 0.0),
            (defl, 25.0),
            (lo, 100.0),
            (hi, 100.0),
            (over, 55.0),
            (over + 2.5, 0.0),
        ],
    )

    w_speed = float(getattr(cfgc, "speed_weight", 0.65))
    w_level = float(getattr(cfgc, "level_weight", 0.35))
    if np.isnan(speed_score):
        total = level_score
    else:
        total = w_speed * speed_score + w_level * level_score

    # ---- 滞胀熔断 ----
    stagflation = False
    if getattr(cfgc, "stagflation_override", True) and now > over and (not np.isnan(delta3)) and delta3 > 0:
        total = min(total, 30.0)
        stagflation = True

    if stagflation:
        state = f"滞胀警戒（CPI {now:.2f}% 高于过热线 {over:g}% 且仍在加速）"
    elif not np.isnan(delta3) and delta3 > fast:
        state = f"通胀加速（CPI {now:.2f}%，3 个月上行 {delta3:+.2f}pct）"
    elif not np.isnan(delta3) and delta3 > 0:
        state = f"通胀温和上行（CPI {now:.2f}%，3 个月上行 {delta3:+.2f}pct）"
    elif not np.isnan(delta3) and delta3 < -fast:
        state = f"通胀快速回落（CPI {now:.2f}%，3 个月下行 {delta3:+.2f}pct）"
    else:
        state = f"通胀走平/缓降（CPI {now:.2f}%）"
    if now < defl:
        state += "，已进入通缩区间"

    explain = (
        f"最新 CPI 同比 {now:.2f}%（{asof:%Y-%m}），3 个月变化 {delta3:+.2f} 个百分点。"
        f"速度项 {speed_score:.0f} 分（±{fast:g}pct 为满分/零分界）；"
        f"水平项 {level_score:.0f} 分（温和区间 {lo:g}%~{hi:g}%）。"
        "规则依据：CPI 涨得快利好股市和债市，涨得慢则不利。"
        + ("触发滞胀熔断：高位仍在加速，压至 30 分以下。" if stagflation else "")
    )

    return RuleResult(
        name="CPI",
        score=clamp(total),
        state=state,
        detail={
            "cpi_yoy": round(now, 2),
            "asof": str(asof),
            "delta_3m": None if np.isnan(delta3) else round(delta3, 2),
            "speed_score": None if np.isnan(speed_score) else round(speed_score, 1),
            "level_score": round(level_score, 1),
            "stagflation": stagflation,
        },
        explain=explain,
    )


# ===========================================================================
# 3) PPI 规则（本系统最有特色的一条）
# 用户原话：“PPI 下跌到一半，股市会领先上涨；当 PPI 到底部时，
#            股市已经就到顶峰了。”
#
# 实现：先识别最近一轮 PPI 同比的周期高点和低点，再算「回撤比例」r：
#       r = (周期高点 - 当前) / (周期高点 - 周期低点)
#   r ≈ 0.0  还在高位     → 盈利刚开始下行，看空
#   r ≈ 0.5  下跌到一半   → 用户规则里的「领先上涨」买点，最看多
#   r ≈ 0.85 接近底部     → 用户规则里的「股市已到顶峰」，开始减仓
#   r ≈ 1.0  已到周期底   → 见顶确认，最看空
# 因此分数对 r 呈钟形（hump shape），峰值在 r=0.5 附近。
# ===========================================================================
def score_ppi(ppi_yoy: pd.Series, cfg) -> RuleResult:
    cfgp = cfg.ppi
    s = ppi_yoy.dropna()
    if s.empty:
        return RuleResult("PPI", float("nan"), "数据缺失", explain="未取到 PPI 数据")

    now, asof = _last_valid(s)
    lookback = int(cfgp.cycle_lookback_months)
    hi, lo, hi_pos, lo_pos = find_cycle_high_low(s, lookback_months=lookback)
    r = retracement_ratio(now, hi, lo)

    half = float(cfgp.half_retrace)
    band = float(cfgp.half_band)
    bottom = float(cfgp.bottom_retrace)

    if np.isnan(r):
        score = 50.0
        state = "周期不明确（PPI 波动过小，按中性处理）"
    else:
        score = piecewise_linear(
            r,
            [
                (0.0, 15.0),
                (max(0.05, half - band - 0.12), 45.0),
                (half - band, 72.0),
                (half, 95.0),            # ★ 用户规则：下跌到一半 = 股市领先上涨
                (half + band, 85.0),
                (bottom - 0.10, 50.0),
                (bottom, 30.0),
                (1.0, 15.0),             # ★ 用户规则：PPI 到底部 = 股市已到顶峰
            ],
        )
        score = clamp(score)
        if r < half - band - 0.12:
            state = f"高位回落初期（回撤 {r:.0%}），盈利下行，偏空"
        elif abs(r - half) <= band:
            state = f"回撤至一半（{r:.0%}）—— 用户规则的看多区间"
        elif r < bottom - 0.10:
            state = f"回撤中段（{r:.0%}），中性偏多"
        elif r < 1.0:
            state = f"接近周期底部（回撤 {r:.0%}）—— 用户规则的见顶警戒区"
        else:
            state = f"已到周期底部（回撤 {r:.0%}）—— 用户规则的见顶确认"

    # PPI 方向：区分「下跌到一半」和「回升到一半」，只在说明里体现
    d3 = now - _val_at(s, 3) if len(s) > 3 else float("nan")
    direction = "下行" if (not np.isnan(d3) and d3 < 0) else ("上行" if not np.isnan(d3) else "未知")

    explain = (
        f"最新 PPI 同比 {now:.2f}%（{asof:%Y-%m}），近 {lookback} 个月周期高点 {hi:.2f}%、"
        f"低点 {lo:.2f}%，当前回撤比例 r={r:.2f}（{r:.0%}），PPI 近 3 个月{direction} {d3:+.2f}pct。"
        f"钟形打分峰值设在 r={half:.0%}（用户规则：PPI 下跌到一半，股市领先上涨），"
        f"谷值设在 r={bottom:.0%}~100%（用户规则：PPI 到底部时，股市已到顶峰）。"
    )

    return RuleResult(
        name="PPI",
        score=score,
        state=state,
        detail={
            "ppi_yoy": round(now, 2),
            "asof": str(asof),
            "cycle_high": round(hi, 2),
            "cycle_low": round(lo, 2),
            "retracement": None if np.isnan(r) else round(r, 3),
            "ppi_3m_change": None if np.isnan(d3) else round(d3, 2),
            "direction": direction,
        },
        explain=explain,
    )


# ===========================================================================
# 4) GDP 规则
# 用户原话：“GDP、PMI 与股市是正相关。”
# GDP 是季度、低频、且滞后公布，所以：水平项看增速绝对位置，
# 动能项看是否连续回升（这是股市更敏感的变量），动能权重给得更高。
# ===========================================================================
def score_gdp(gdp_yoy: pd.Series, cfg) -> RuleResult:
    cfgg = cfg.gdp
    s = gdp_yoy.dropna()
    if s.empty:
        return RuleResult("GDP", float("nan"), "数据缺失", explain="未取到 GDP 数据")

    now, asof = _last_valid(s)
    lookback = int(cfgg.lookback_quarters)

    # ---- 水平项：增速绝对水平 ----
    level_score = linear_score(now, bad=3.0, good=7.0)

    # ---- 动能项：相对过去 lookback 个季度均值的偏离 ----
    hist = s.iloc[-(lookback + 1) : -1] if len(s) > lookback else s.iloc[:-1]
    baseline = float(hist.mean()) if len(hist) else float("nan")
    delta = now - baseline if not np.isnan(baseline) else float("nan")
    thr = float(cfgg.rise_threshold)
    momentum_score = linear_score(delta, bad=-thr * 2.5, good=thr * 2.5) if not np.isnan(delta) else float("nan")

    # 连续回升季度数
    diffs = s.diff().dropna()
    consecutive_up = 0
    for d in reversed(diffs.to_list()):
        if d > 0:
            consecutive_up += 1
        else:
            break
    if consecutive_up >= 2 and not np.isnan(momentum_score):
        momentum_score = clamp(momentum_score + 8.0)

    w_level = float(getattr(cfgg, "level_weight", 0.4))
    w_mom = float(getattr(cfgg, "momentum_weight", 0.6))
    total = level_score if np.isnan(momentum_score) else w_level * level_score + w_mom * momentum_score

    state = (
        f"增速{now:.1f}%，{'高于' if delta > 0 else '低于'}近 {lookback} 季均值 "
        f"{delta:+.2f}pct，连续回升 {consecutive_up} 个季度"
    )

    explain = (
        f"最新 GDP 同比 {now:.1f}%（{asof:%Y-%m} 可见）。"
        f"水平项 {level_score:.0f} 分（3%~7% 映射 0~100）；"
        f"动能项 {momentum_score:.0f} 分（较近 {lookback} 季均值 {delta:+.2f}pct）。"
        "规则依据：GDP 与股市正相关。注意 GDP 为季度低频数据且滞后公布，权重低于 PMI。"
    )

    return RuleResult(
        name="GDP",
        score=clamp(total),
        state=state,
        detail={
            "gdp_yoy": round(now, 2),
            "asof": str(asof),
            "level_score": round(level_score, 1),
            "momentum_score": None if np.isnan(momentum_score) else round(momentum_score, 1),
            "delta_vs_mean": None if np.isnan(delta) else round(delta, 2),
            "consecutive_up_quarters": consecutive_up,
        },
        explain=explain,
    )


__all__ = ["RuleResult", "score_pmi", "score_cpi", "score_ppi", "score_gdp"]
