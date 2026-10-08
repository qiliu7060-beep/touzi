"""美联储潮汐信号（用户 7 条判断条件之一）。

## 概念

「潮汐」指全球美元流动性的涨落。它通过三条路径影响 A 股：

1. **贴现率路径**：美债利率是全球风险资产的定价锚。锚抬升 → 估值压缩。
2. **流动性路径**：美联储降息/扩表 → 美元外溢，新兴市场拿到增量资金。
3. **汇率路径**：美元走强 → 人民币承压 → 外资流出、风险偏好下降。

## 五个子项

    政策利率方向(0.30) · 长端利率(0.22) · 美元指数(0.20) · 中美利差(0.16) · 资产负债表(0.12)

涨潮（宽松）得高分，退潮（紧缩）得低分，50 分为中性。合成时**缺席的子项
在可用子项之间重新归一化权重，不按 0 分算**——否则「拿不到数据」会被误读成
「流动性紧缩」，进而错误减仓。

## 数据源的三个诚实说明

这三条是本模块最容易被误读的地方，报告里要照实写：

1. **政策利率用的是 13 周美债收益率（^IRX），不是联邦基金利率。**
   FRED 的 DFF 端点在本机网络下已不可达；akshare 的
   ``macro_bank_usa_interest_rate``（金十）只更新到 2025-10，末行还是 NaN。
   ^IRX 紧贴联邦基金利率，**方向判断够用**，但不能当成官方利率引用。
2. **长端利率用的是 10 年期美债名义收益率（^TNX），不是 TIPS 实际利率。**
   名义 = 实际 + 通胀预期，高通胀期两者背离，所以这项的含义是
   「长端贴现率」而非「真实利率」。
3. **美联储总资产（WALCL）没有可用的免费源。** 抓不到时该子项缺席并归一化。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..util import clamp, linear_score
from .macro import RuleResult

# 面板列名 -> 中文，用于报错与说明
COLUMN_LABELS = {
    "policy_rate": "短端利率(13周美债)",
    "us_5y": "5年期美债收益率",
    "us_10y": "10年期美债收益率",
    "us_30y": "30年期美债收益率",
    "dollar": "美元指数",
    "vix": "VIX",
    "balance_sheet": "美联储总资产",
}


def _monthly_last(panel: pd.DataFrame, column: str) -> pd.Series:
    """取某列的月末值序列。"""
    if panel is None or column not in getattr(panel, "columns", []):
        return pd.Series(dtype=float)
    s = panel[column].dropna()
    if s.empty:
        return s
    return s.resample("ME").last().dropna()


def _change_over_months(s: pd.Series, months: int) -> float:
    if len(s) <= months:
        return float("nan")
    return float(s.iloc[-1] - s.iloc[-1 - months])


def _pct_change_over_months(s: pd.Series, months: int) -> float:
    if len(s) <= months:
        return float("nan")
    prev = float(s.iloc[-1 - months])
    if prev == 0:
        return float("nan")
    return float((float(s.iloc[-1]) / prev - 1.0) * 100.0)


def score_fed(fed_panel: pd.DataFrame, cfg, china_10y: pd.Series | None = None) -> RuleResult:
    """计算美联储潮汐评分。

    参数
    ----
    fed_panel : 日频面板，列名见 ``macro_us.YAHOO_SYMBOLS``
    china_10y : 中国 10 年期国债收益率（%），日频或月频均可
    """
    fcfg = cfg.fed
    lookback = int(fcfg.get("lookback_months", 6))
    subs: dict[str, float] = {}
    detail: dict[str, object] = {}
    notes: list[str] = []

    # ------------------------------------------------- 1) 政策利率方向（短端）
    short = _monthly_last(fed_panel, "policy_rate")
    if not short.empty:
        now = float(short.iloc[-1])
        delta_bp = _change_over_months(short, lookback) * 100.0  # % -> bp
        thr = float(fcfg.get("rate_change_bp", 100.0))
        direction = linear_score(delta_bp, bad=thr, good=-thr) if np.isfinite(delta_bp) else np.nan
        level = linear_score(now, bad=float(fcfg.get("rate_level_bad", 5.0)),
                             good=float(fcfg.get("rate_level_good", 0.5)))
        sub = 0.7 * direction + 0.3 * level if np.isfinite(direction) else level
        if np.isfinite(sub):
            subs["policy_rate"] = clamp(sub)
            detail.update(
                short_rate=round(now, 2),
                short_rate_6m_change_bp=None if not np.isfinite(delta_bp) else round(delta_bp, 1),
                rate_direction_score=None if not np.isfinite(direction) else round(float(direction), 1),
            )
            notes.append(
                f"短端利率(13周美债，政策利率代理) {now:.2f}%，近 {lookback} 个月 "
                f"{delta_bp:+.0f}bp（{'下行=涨潮' if delta_bp < 0 else '上行/持平=退潮'}）"
            )

    # ------------------------------------------------------ 2) 长端利率(10Y)
    long_ = _monthly_last(fed_panel, "us_10y")
    if not long_.empty:
        now_l = float(long_.iloc[-1])
        d6 = _change_over_months(long_, lookback)
        level = linear_score(now_l, bad=float(fcfg.get("long_rate_bad", 5.5)),
                             good=float(fcfg.get("long_rate_good", 1.5)))
        direction = linear_score(d6, bad=0.8, good=-0.8) if np.isfinite(d6) else np.nan
        sub = 0.6 * level + 0.4 * direction if np.isfinite(direction) else level
        if np.isfinite(sub):
            subs["long_rate"] = clamp(sub)
            detail.update(
                us_10y=round(now_l, 2),
                us_10y_6m_change=None if not np.isfinite(d6) else round(d6, 2),
            )
            notes.append(f"10 年期美债名义收益率 {now_l:.2f}%，近 {lookback} 个月 {d6:+.2f}pct")

    # -------------------------------------------------------- 3) 美元指数
    dxy = _monthly_last(fed_panel, "dollar")
    if not dxy.empty:
        now_x = float(dxy.iloc[-1])
        d6p = _pct_change_over_months(dxy, lookback)
        thr_x = float(fcfg.get("dollar_change_pct", 6.0))
        sub = linear_score(d6p, bad=thr_x, good=-thr_x) if np.isfinite(d6p) else np.nan
        if np.isfinite(sub):
            subs["dollar"] = clamp(sub)
            detail.update(
                dollar_index=round(now_x, 2),
                dollar_6m_change_pct=None if not np.isfinite(d6p) else round(d6p, 2),
            )
            notes.append(
                f"美元指数 DXY {now_x:.1f}，近 {lookback} 个月 {d6p:+.1f}%"
                f"（{'走弱利好新兴市场' if d6p < 0 else '走强压制风险偏好'}）"
            )

    # -------------------------------------------------------- 4) 中美利差
    if china_10y is not None and len(china_10y.dropna()):
        cn = china_10y.dropna()
        us = _monthly_last(fed_panel, "us_10y")
        if not us.empty:
            cn_now = float(cn.iloc[-1])
            us_now = float(us.reindex([cn.index[-1]], method="ffill").iloc[0])
            if np.isfinite(us_now):
                spread_bp = (cn_now - us_now) * 100.0
                # 帐篷形：倒挂深了扣分，适度为正最好，过宽（>high）也扣分
                low = linear_score(spread_bp, bad=float(fcfg.get("spread_low_bp", -400.0)),
                                   good=float(fcfg.get("spread_sweet_bp", 200.0)))
                high = linear_score(spread_bp, bad=float(fcfg.get("spread_high_bp", 900.0)),
                                    good=float(fcfg.get("spread_sweet_bp", 200.0)))
                sub = min(low, high) if np.isfinite(low) and np.isfinite(high) else np.nan
                if np.isfinite(sub):
                    subs["rate_spread"] = clamp(sub)
                    detail.update(
                        china_10y=round(cn_now, 2),
                        us_10y_spread_ref=round(us_now, 2),
                        cn_us_spread_bp=round(spread_bp, 0),
                    )
                    notes.append(
                        f"中美 10 年期利差 {spread_bp:+.0f}bp（中债 {cn_now:.2f}% - 美债 {us_now:.2f}%）"
                        f"（{'倒挂，人民币资产吸引力弱' if spread_bp < 0 else '为正'}）"
                    )

    # ---------------------------------------------------- 5) 资产负债表(WALCL)
    walcl = _monthly_last(fed_panel, "balance_sheet")
    if not walcl.empty:
        ty = float(walcl.iloc[-1])
        ly = float(walcl.iloc[-13]) if len(walcl) > 13 else np.nan
        yoy = (ty / ly - 1.0) * 100.0 if np.isfinite(ly) and ly else np.nan
        sub = linear_score(yoy, bad=float(fcfg.get("bs_shrink_pct", -2.0)), good=8.0) if np.isfinite(yoy) else np.nan
        if np.isfinite(sub):
            subs["balance_sheet"] = clamp(sub)
            detail.update(
                fed_balance_sheet_wan_yi=round(ty / 1e6, 2),  # 百万美元 -> 万亿美元
                fed_bs_yoy_pct=round(float(yoy), 2),
            )
            notes.append(
                f"美联储总资产 {ty / 1e6:.2f} 万亿美元，同比 {yoy:+.1f}%"
                f"（{'扩表=涨潮' if yoy > 0 else '缩表=退潮'}）"
            )

    # ---------------------------------------------------------------- 合成
    if not subs:
        missing = [COLUMN_LABELS.get(c, c) for c in ("policy_rate", "us_10y", "dollar")]
        return RuleResult(
            "美联储潮汐", float("nan"), "数据缺失",
            explain="未取到任何美联储序列（缺：" + "、".join(missing) + "）",
        )

    weight_map = {
        "policy_rate": float(fcfg.get("weight_policy_rate", 0.30)),
        "long_rate": float(fcfg.get("weight_long_rate", 0.22)),
        "dollar": float(fcfg.get("weight_dollar", 0.20)),
        "rate_spread": float(fcfg.get("weight_rate_spread", 0.16)),
        "balance_sheet": float(fcfg.get("weight_balance_sheet", 0.12)),
    }
    num = sum(subs[k] * weight_map[k] for k in subs)
    den = sum(weight_map[k] for k in subs)
    total = clamp(num / den) if den else float("nan")

    absent = [COLUMN_LABELS[k] for k in weight_map if k not in subs]
    if total >= 70:
        tide = "涨潮（全球流动性宽松）"
    elif total >= 55:
        tide = "偏涨潮"
    elif total >= 45:
        tide = "潮位中性"
    elif total >= 30:
        tide = "偏退潮"
    else:
        tide = "退潮（全球流动性紧缩）"

    explain = (
        f"美联储潮汐评分 {total:.0f}，{tide}。" + "；".join(notes) + "。"
        "规则依据：降息/扩表/长端利率下行/美元走弱 = 涨潮，利好 A 股；反之为退潮。"
    )
    if absent:
        explain += f"注意：{'、'.join(absent)} 数据缺席，已在其余子项间重新归一化权重。"

    detail["sub_scores"] = {k: round(v, 1) for k, v in subs.items()}
    detail["absent"] = absent
    return RuleResult("美联储潮汐", total, tide, detail=detail, explain=explain)


__all__ = ["score_fed", "COLUMN_LABELS"]
