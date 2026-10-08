"""质量打分：ROE、经营现金流/净利润、资产负债率、盈利稳定性。

## 为什么长期投资必须看质量

低估值可能是「便宜的好公司」，也可能是「便宜有便宜的道理」（价值陷阱）。
质量项的作用就是把后者挑出去：一家长期 ROE 高、经营现金流能覆盖净利润、
负债不重、盈利波动小的公司，才有资格谈「长期持有」。

## 四个子项与它们的阈值

| 子项 | 权重 | 口径 | 优 | 及格 |
|---|---|---|---|---|
| ROE | 0.30 | 近 3 年均值(%) | ≥20 | 8 |
| 经营现金流/净利润 | 0.25 | 近 3 年均值(倍) | ≥1.0 | 0.6 |
| 资产负债率 | 0.20 | 最新(%) | ≤40 | 70 |
| 盈利稳定性 | 0.25 | 近 5 年净利润增速标准差 | ≤0.10 | 0.40 |

现金流/净利润 > 1 说明利润是真金白银收到的，不是应收账款堆出来的。

## 两个必要的例外

- **金融与地产豁免负债率**：银行靠负债经营，资产负债率 90%+ 是常态，
  拿制造业的标准去卡银行，会把所有银行判成垃圾。
- **ROE 用近 3 年均值而非单期**：单期 ROE 可能被一次性损益（卖资产、补助）拉高。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..util import clamp, linear_score
from .macro import RuleResult

__all__ = ["QualityInput", "score_quality", "summarize_financials"]


class QualityInput(dict):
    """质量打分的输入容器。"""

    FIELDS = (
        "code", "name", "industry",
        "roe",            # 近 3 年净资产收益率均值(%)
        "roe_series",     # 近若干期 ROE 序列（用于稳定性）
        "ocf_to_profit",  # 近 3 年经营现金流/净利润均值(倍)
        "debt_ratio",     # 最新资产负债率(%)
        "profit_growth_std",   # 近 5 年净利润增速标准差
        "profit_growth_mean",  # 近 5 年净利润增速均值
        "current_ratio",
        "gross_margin",
        "report_date",
        "periods",        # 有效报告期数
    )

    def __init__(self, **kw: Any):
        super().__init__({k: kw.get(k) for k in self.FIELDS})


def summarize_financials(fin: pd.DataFrame, stability_years: int = 5) -> dict[str, Any]:
    """把逐报告期的财务指标压成质量打分需要的几个统计量。

    ``fin`` 来自 :func:`touzi.data.stock.fetch_financial_indicators`。

    **只对年报（12-31）行取均值**，这是必须的：新浪财务表是年内累计口径，
    茅台的 ROE 在 2025 年逐季累加为 10.39 → 19.03 → 25.14 → 33.65，
    把 2025-12-31 与 2026-03-31、2026-06-30 混在一起求平均会得到 20 上下，
    既不是任何一年的 ROE，也掩盖了真实的盈利能力。
    资产负债率与流动比率是时点指标（不是累计量），因此取最新一期即可。
    """
    if fin is None or len(fin) == 0:
        return {}
    f = fin.dropna(subset=["report_date"]).sort_values("report_date").copy()
    if len(f) == 0:
        return {}

    annual = f[f["is_annual"]] if "is_annual" in f.columns else f[f["report_date"].dt.month.eq(12)]
    if len(annual) == 0:
        annual = f  # 只有季度数据时退回全量，并在 periods_annual 里如实标 0

    def tail_mean(frame: pd.DataFrame, col: str, n: int = 3) -> float:
        if col not in frame.columns:
            return float("nan")
        s = pd.to_numeric(frame[col], errors="coerce").dropna()
        return float(s.tail(n).mean()) if len(s) else float("nan")

    growth = pd.to_numeric(annual.get("profit_growth"), errors="coerce").dropna().tail(stability_years)
    # 净利润增速原始单位是百分数；标准差转成「倍」便于与阈值(0.10/0.40)比较
    growth_std = float(growth.std()) / 100.0 if len(growth) >= 2 else float("nan")
    growth_mean = float(growth.mean()) / 100.0 if len(growth) else float("nan")

    roe_series = pd.to_numeric(annual.get("roe"), errors="coerce").dropna().tail(12)
    growth_series = pd.to_numeric(annual.get("profit_growth"), errors="coerce").dropna().tail(12)
    latest = f.iloc[-1]
    return {
        "roe": tail_mean(annual, "roe", 3),
        "roe_series": roe_series.to_list(),
        # 年报顺序由老到新，供「连续 N 年下滑/为负」的卖出规则判定
        "profit_growth_series": growth_series.to_list(),
        "ocf_to_profit": tail_mean(annual, "ocf_to_profit", 3),
        "debt_ratio": float(pd.to_numeric(latest.get("debt_ratio"), errors="coerce")),
        "profit_growth_std": growth_std,
        "profit_growth_mean": growth_mean,
        "current_ratio": float(pd.to_numeric(latest.get("current_ratio"), errors="coerce")),
        "report_date": str(pd.Timestamp(latest["report_date"]).date()),
        "periods": int(len(f)),
        "periods_annual": int(len(annual)),
    }


def score_quality(data: QualityInput | dict, cfg) -> RuleResult:
    """质量总分 = ROE 30% + 现金流 25% + 负债 20% + 稳定性 25%。"""
    qcfg = cfg.quality
    d = dict(data)
    industry = str(d.get("industry") or "")
    notes: list[str] = []
    detail: dict[str, Any] = {}

    # ------------------------------------------------------------------ ROE
    roe = d.get("roe")
    if roe is not None and np.isfinite(roe):
        roe_score = linear_score(roe, bad=float(qcfg.roe_pass), good=float(qcfg.roe_excellent))
        if roe >= float(qcfg.roe_excellent):
            notes.append(f"ROE {roe:.1f}% ≥ {qcfg.roe_excellent}%，盈利能力优秀")
        elif roe < float(qcfg.roe_pass):
            notes.append(f"ROE {roe:.1f}% < {qcfg.roe_pass}%，盈利能力偏弱")
        else:
            notes.append(f"ROE {roe:.1f}%（近3年均值），中等")
    else:
        roe_score = float("nan")
        notes.append("ROE 数据缺失")

    # ------------------------------------------------------ 现金流/净利润
    ocf = d.get("ocf_to_profit")
    if ocf is not None and np.isfinite(ocf):
        ocf_score = linear_score(ocf, bad=float(qcfg.cashflow_pass), good=float(qcfg.cashflow_excellent))
        if ocf >= float(qcfg.cashflow_excellent):
            notes.append(f"经营现金流是净利润的 {ocf:.2f} 倍，利润含金量高")
        elif ocf < 0:
            notes.append(f"经营现金流为负（{ocf:.2f} 倍），利润没有现金支撑，红旗")
        else:
            notes.append(f"经营现金流/净利润 = {ocf:.2f} 倍，偏低")
    else:
        ocf_score = float("nan")
        notes.append("现金流数据缺失")

    # ------------------------------------------------------------ 负债率
    debt = d.get("debt_ratio")
    exempt = [str(x) for x in (qcfg.get("debt_exempt_industries") or [])]
    is_exempt = any(k in industry for k in exempt) if industry else False
    if is_exempt:
        debt_score = float("nan")
        detail["debt_exempt"] = True
        notes.append(f"「{industry}」属高杠杆经营行业，负债率不参与打分（豁免）")
    elif debt is not None and np.isfinite(debt):
        debt_score = linear_score(debt, bad=float(qcfg.debt_pass), good=float(qcfg.debt_excellent),
                                  higher_is_better=False)
        if debt <= float(qcfg.debt_excellent):
            notes.append(f"资产负债率 {debt:.1f}%，财务稳健")
        elif debt > float(qcfg.debt_pass):
            notes.append(f"资产负债率 {debt:.1f}% 偏高，偿债压力大")
        else:
            notes.append(f"资产负债率 {debt:.1f}%，尚可")
    else:
        debt_score = float("nan")
        notes.append("负债率数据缺失")

    # ------------------------------------------------------------ 稳定性
    gstd = d.get("profit_growth_std")
    if gstd is not None and np.isfinite(gstd):
        stab_score = linear_score(gstd, bad=float(qcfg.stability_pass), good=float(qcfg.stability_excellent),
                                  higher_is_better=False)
        if gstd <= float(qcfg.stability_excellent):
            notes.append(f"近{int(qcfg.stability_years)}年净利润增速标准差 {gstd:.2f}，盈利很稳定")
        elif gstd > float(qcfg.stability_pass):
            notes.append(f"净利润增速标准差 {gstd:.2f} 很大，盈利大起大落")
        else:
            notes.append(f"净利润增速标准差 {gstd:.2f}，波动中等")
    else:
        stab_score = float("nan")
        notes.append("盈利稳定性数据不足（报告期太少）")

    # ------------------------------------------------ 原始指标写入 detail
    # 买卖规则（touzi/plans.py）要逐条核查这些原始值，而不是只看加权分；
    # 报告里也直接用它们说明「为什么给这个分」，所以这里如实留存。
    for key, val in (
        ("roe", roe),
        ("ocf_to_profit", ocf),
        ("debt_ratio", debt),
        ("profit_growth_std", gstd),
        ("profit_growth_mean", d.get("profit_growth_mean")),
    ):
        try:
            fv = float(val)
        except (TypeError, ValueError):
            continue
        if np.isfinite(fv):
            detail[key] = round(fv, 4)
    detail["debt_exempt"] = bool(is_exempt)
    detail["roe_series"] = [
        round(float(x), 4) for x in (d.get("roe_series") or []) if x is not None and np.isfinite(float(x))
    ]
    detail["profit_growth_series"] = [
        round(float(x), 4) for x in (d.get("profit_growth_series") or [])
        if x is not None and np.isfinite(float(x))
    ]
    detail["periods_annual"] = d.get("periods_annual")

    # -------------------------------------------------------- 加权（归一）
    weights = {
        "roe": (float(qcfg.weight_roe), roe_score),
        "ocf": (float(qcfg.weight_cashflow), ocf_score),
        "debt": (float(qcfg.weight_debt), debt_score),
        "stab": (float(qcfg.weight_stability), stab_score),
    }
    usable = {k: (w, s) for k, (w, s) in weights.items() if np.isfinite(s)}
    if usable:
        wsum = sum(w for w, _ in usable.values())
        total = sum(w * s for w, s in usable.values()) / wsum
    else:
        total = float("nan")

    total = clamp(total)
    if not np.isfinite(total):
        state = "数据不足"
    elif total >= 80:
        state = "优秀"
    elif total >= 65:
        state = "良好"
    elif total >= 50:
        state = "一般"
    else:
        state = "较差"

    detail.update(
        {
            "industry": industry,
            "roe": None if roe is None or not np.isfinite(roe) else round(float(roe), 2),
            "ocf_to_profit": None if ocf is None or not np.isfinite(ocf) else round(float(ocf), 3),
            "debt_ratio": None if debt is None or not np.isfinite(debt) else round(float(debt), 2),
            "profit_growth_std": None if gstd is None or not np.isfinite(gstd) else round(float(gstd), 4),
            "sub_scores": {
                k: (None if not np.isfinite(s) else round(float(s), 1))
                for k, s in (("roe", roe_score), ("cashflow", ocf_score),
                             ("debt", debt_score), ("stability", stab_score))
            },
        }
    )
    return RuleResult(
        name="质量",
        score=total,
        state=state,
        detail=detail,
        explain=f"{d.get('code', '')} {d.get('name', '')}".strip() + "；" + "；".join(notes),
    )
