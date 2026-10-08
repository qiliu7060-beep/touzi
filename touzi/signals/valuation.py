"""估值打分：PE 分位 + PB 分位 + 股息率分位（可行业中性化）。

## 打分逻辑

分位（percentile）在这里一律定义为 **0 = 最便宜、1 = 最贵**，
所以「估值分」= ``100 × (1 - 分位)``，越便宜分越高。

三种分位来源，按可靠性从高到低降级，并在 explain 里写明用了哪一种：

1. **个股自身历史分位**（首选）：用百度估值序列的近 5 年数据滚动计算。
   它回答的是「这家公司现在的估值，比它自己过去 5 年便宜还是贵」。
2. **行业内截面分位**：拿全市场同行业股票的当期 PE/PB 排位。
   银行股 PE 天然低、科技股天然高，跨行业直接比 PE 没有意义，
   所以这一层是 ``industry_neutral`` 想解决的核心问题。
3. **全市场截面分位**：行业数据缺失时的兜底。

## 行业主指标

不同行业看不同指标才合理：银行/地产看 PB（净资产是主要资产），
煤炭/公用事业看股息率（现金回报是主要价值），成长行业看 PE。
``cfg.valuation.industry_primary`` 配置了这张映射表，
命中主指标的那一项会拿到 ``primary_bonus`` 的加成。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..util import clamp, linear_score, piecewise_linear
from .macro import RuleResult

__all__ = ["ValuationInput", "score_valuation", "pe_pb_band", "percentile_of"]

# 分位映射到分数：分位 0(最便宜) → 100 分，分位 1(最贵) → 0 分。
# 用分段线性而不是纯线性，是因为极低分位与极高分位的信息量更大，
# 中间段拉开区分度意义不大。
_PCTL_TO_SCORE: tuple[tuple[float, float], ...] = (
    (0.00, 100.0),
    (0.10, 95.0),
    (0.25, 85.0),
    (0.40, 72.0),
    (0.50, 63.0),
    (0.60, 52.0),
    (0.75, 35.0),
    (0.90, 15.0),
    (1.00, 0.0),
)


def percentile_of(value: float, pool: pd.Series | np.ndarray) -> float:
    """value 在 pool 中的分位（0~1，越小越便宜）。pool 需为同口径的正值样本。"""
    if value is None or (isinstance(value, float) and np.isnan(value)) or value <= 0:
        return float("nan")
    arr = np.asarray(pool, dtype=float)
    arr = arr[np.isfinite(arr) & (arr > 0)]
    if len(arr) < 5:
        return float("nan")
    return float((arr < value).sum() / len(arr))


def pe_pb_band(metric: str, values: pd.Series, roll_window: int = 250) -> pd.Series:
    """把估值序列转成滚动历史分位（0~1）。

    直接用全样本分位会有未来函数（用到了今天之后的数据），所以滚动计算。
    负值（亏损导致的负 PE）没有估值含义，先剔除再打分位。
    """
    s = pd.to_numeric(values, errors="coerce").astype(float)
    s = s.where(s > 0)  # 负 PE/PB 不参与分位
    pctl = s.rolling(roll_window, min_periods=max(60, roll_window // 5)).apply(
        lambda w: float((w[:-1] < w[-1]).sum() / max(1, len(w) - 1)) if np.isfinite(w[-1]) else np.nan,
        raw=True,
    )
    return pctl


class ValuationInput(dict):
    """估值打分的输入容器（用 dict 存，便于直接落表追溯）。"""

    FIELDS = (
        "code", "name", "industry",
        "pe_ttm", "pb", "dividend_yield",
        "pe_percentile_ts", "pb_percentile_ts", "dy_percentile_ts",
        "pe_percentile_cs", "pb_percentile_cs", "dy_percentile_cs",
        "industry_median_pe", "industry_median_pb",
        "pe_percentile_industry", "pb_percentile_industry",
    )

    def __init__(self, **kw: Any):
        super().__init__({k: kw.get(k) for k in self.FIELDS})


def _resolve_percentile(*candidates: tuple[str, float]) -> tuple[float, str]:
    """按优先级挑第一个可用的分位，并回传它来自哪儿（写进 explain）。"""
    for label, value in candidates:
        if value is not None and not (isinstance(value, float) and np.isnan(value)):
            return float(value), label
    return float("nan"), "缺失"


def score_valuation(data: ValuationInput | dict, cfg) -> RuleResult:
    """估值总分 = PE 分位 40% + PB 分位 30% + 股息率分位 30%（权重可配）。"""
    vcfg = cfg.valuation
    d = dict(data)
    code = d.get("code", "")
    industry = d.get("industry") or "未知"

    pe_score = pb_score = dy_score = float("nan")
    detail: dict[str, Any] = {}
    notes: list[str] = []

    # ---------------------------------------------------------------- PE
    pe = d.get("pe_ttm")
    if pe is not None and np.isfinite(pe) and pe > 0:
        pctl, src = _resolve_percentile(
            ("个股5年历史", d.get("pe_percentile_ts")),
            ("行业内截面", d.get("pe_percentile_industry")),
            ("全市场截面", d.get("pe_percentile_cs")),
        )
        if np.isfinite(pctl):
            pe_score = piecewise_linear(pctl, _PCTL_TO_SCORE)
            band = "便宜" if pctl <= 0.35 else ("偏贵" if pctl >= 0.7 else "中性")
            notes.append(f"PE {pe:.1f}，处于{src}分位 {pctl:.0%}（{band}），得 {pe_score:.0f} 分")
            detail["pe_percentile"] = round(pctl, 4)
            detail["pe_percentile_source"] = src
        else:
            notes.append(f"PE {pe:.1f}，无可用分位参考（历史与同行业样本都不足）")
    elif pe is not None and np.isfinite(pe) and pe <= 0:
        notes.append(f"PE {pe:.1f} 为负（亏损），估值项不给分——长期投资应回避亏损公司")
        pe_score = 0.0

    # ---------------------------------------------------------------- PB
    pb = d.get("pb")
    if pb is not None and np.isfinite(pb) and pb > 0:
        pctl, src = _resolve_percentile(
            ("个股5年历史", d.get("pb_percentile_ts")),
            ("行业内截面", d.get("pb_percentile_industry")),
            ("全市场截面", d.get("pb_percentile_cs")),
        )
        if np.isfinite(pctl):
            pb_score = piecewise_linear(pctl, _PCTL_TO_SCORE)
            band = "便宜" if pctl <= 0.35 else ("偏贵" if pctl >= 0.7 else "中性")
            notes.append(f"PB {pb:.2f}，处于{src}分位 {pctl:.0%}（{band}），得 {pb_score:.0f} 分")
            detail["pb_percentile"] = round(pctl, 4)
            detail["pb_percentile_source"] = src
        else:
            notes.append(f"PB {pb:.2f}，无可用分位参考")

    # ------------------------------------------------------ 股息率分位
    dy = d.get("dividend_yield")
    if dy is not None and np.isfinite(dy) and dy > 0:
        pctl, src = _resolve_percentile(
            ("个股5年历史", d.get("dy_percentile_ts")),
            ("全市场截面", d.get("dy_percentile_cs")),
        )
        if np.isfinite(pctl):
            # 股息率是越高越好，分位越低（越低=越差）→ 分数越低，方向与 PE 相反
            dy_score = piecewise_linear(1.0 - pctl, _PCTL_TO_SCORE)
            notes.append(f"股息率 {dy:.2f}%，处于{src}分位 {pctl:.0%}，得 {dy_score:.0f} 分")
            detail["dy_percentile"] = round(pctl, 4)
        else:
            # 没有历史分位时，退化为绝对水平判断
            dy_score = piecewise_linear(dy, [(0.0, 0.0), (1.0, 30.0), (2.0, 55.0),
                                             (3.0, 75.0), (4.5, 95.0), (6.0, 100.0)])
            notes.append(f"股息率 {dy:.2f}%，无历史分位，按绝对水平得 {dy_score:.0f} 分")
    else:
        notes.append("无股息率数据（不分红或数据缺失），股息率项不给分")
        dy_score = 0.0

    # ------------------------------------------------- 加权合成（按可用项归一）
    weights = {
        "pe": float(vcfg.pe_weight),
        "pb": float(vcfg.pb_weight),
        "dy": float(vcfg.dividend_yield_weight),
    }
    scores = {"pe": pe_score, "pb": pb_score, "dy": dy_score}
    usable = {k: (w, scores[k]) for k, w in weights.items()
              if np.isfinite(scores[k])}
    if usable:
        wsum = sum(w for w, _ in usable.values())
        total = sum(w * s for w, s in usable.values()) / wsum
    else:
        total = float("nan")

    # ------------------------------------------------ 行业主指标加成
    primary = None
    try:
        primary = cfg.valuation.industry_primary.get(industry)
    except Exception:
        primary = None
    if primary and np.isfinite(total):
        key = {"pe": "pe", "pb": "pb", "dividend_yield": "dy"}.get(str(primary))
        if key and np.isfinite(scores.get(key, float("nan"))):
            bonus = float(vcfg.get("primary_bonus", 0.0) or 0.0)
            total = clamp(total + bonus * 100.0 * 0.5)
            notes.append(f"「{industry}」的主指标是 {primary}，加成后 {total:.0f} 分")
            detail["industry_primary"] = str(primary)

    # ------------------------------------------------ 格雷厄姆数检查
    if pe is not None and pb is not None and np.isfinite(pe) and np.isfinite(pb) and pe > 0 and pb > 0:
        graham = float(vcfg.graham_number)
        detail["graham_product"] = round(float(pe) * float(pb), 2)
        if pe * pb <= graham:
            notes.append(f"PE×PB = {pe * pb:.1f} ≤ {graham}（格雷厄姆门槛），通过")
            detail["graham_pass"] = True
        else:
            detail["graham_pass"] = False

    total = clamp(total)
    if not np.isfinite(total):
        state = "数据不足"
    elif total >= 75:
        state = "显著低估"
    elif total >= 55:
        state = "合理偏低"
    elif total >= 40:
        state = "估值中性"
    else:
        state = "偏贵"

    detail["industry"] = industry
    return RuleResult(
        name="估值",
        score=total,
        state=state,
        detail=detail,
        explain=f"{code} {d.get('name', '')}".strip() + "；" + "；".join(notes),
    )
