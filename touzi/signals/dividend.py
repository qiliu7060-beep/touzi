"""股息打分：绝对股息率 + 连续分红年数 + 相对国债的利差。

## 股息率必须自己算

百度估值接口**不提供股息率**（合法 indicator 只有总市值/市盈率TTM/市盈率静/
市净率/市现率），所以股息率唯一可靠的算法是：

    每股派息（分红明细，原列「派息」是每 10 股派息，需 ÷10）
    ------------------------------------------------  → 近 12 个月加总 / 当前股价
                        股价

用「近 12 个月实际现金分红」而不是「上一年度分红」，是因为很多公司改为
中期+年度两次分红，只看年报会把股息率低估近一半。

## 三个子项

| 子项 | 权重 | 含义 |
|---|---|---|
| 绝对股息率 | 0.45 | 拿到手的现金回报有多厚 |
| 连续分红年数 | 0.35 | 分红是习惯还是偶发（长期投资最看重这个） |
| 相对国债利差 | 0.20 | 股息率 - 10年期国债收益率，风险补偿够不够 |

连续分红 10 年给满分：能连续分红十年的公司，商业模式和现金流都经过了
至少一轮完整经济周期检验，这比某一年 8% 的高股息更可信。

## 一个反向信号

若「融资次数 > 分红次数」，说明这家公司从市场拿走的钱比还给股东的多，
在长期投资框架里降低评价。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..util import clamp, linear_score, piecewise_linear
from .macro import RuleResult

__all__ = [
    "DividendInput",
    "score_dividend",
    "ttm_dividend_per_share",
    "consecutive_dividend_years",
    "build_dividend_input",
]


def ttm_dividend_per_share(div_detail: pd.DataFrame, as_of: pd.Timestamp | str | None = None) -> float:
    """近 12 个月每股现金分红合计（元）。

    ``div_detail`` 来自 :func:`touzi.data.stock.fetch_dividend_detail`，
    ``dps`` 列已经是「每股派息」（原列每 10 股口径已换算）。
    """
    if div_detail is None or len(div_detail) == 0:
        return 0.0
    as_of = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.today()
    d = div_detail.copy()
    d["ex_date"] = pd.to_datetime(d["ex_date"], errors="coerce")
    window = d[(d["ex_date"] > as_of - pd.DateOffset(months=12)) & (d["ex_date"] <= as_of)]
    if len(window) == 0:
        return 0.0
    return float(pd.to_numeric(window["dps"], errors="coerce").fillna(0.0).sum())


def consecutive_dividend_years(div_detail: pd.DataFrame, as_of: pd.Timestamp | str | None = None) -> int:
    """从最近一年往前数，连续每年都分红的年数。

    允许「最近一年」尚未分红（很多公司在次年 6-7 月才派息），
    所以从最近一个完整年度开始数。
    """
    if div_detail is None or len(div_detail) == 0:
        return 0
    as_of = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.today()
    d = div_detail.copy()
    d["ex_date"] = pd.to_datetime(d["ex_date"], errors="coerce")
    d = d.dropna(subset=["ex_date"])
    if len(d) == 0:
        return 0
    paid_years = {int(y) for y in d["ex_date"].dt.year.unique()}
    start = as_of.year
    # 若今年还没有除权除息记录，从去年开始数，避免把「还没派」误判成「不派」
    if start not in paid_years:
        start -= 1
    years = 0
    while start in paid_years:
        years += 1
        start -= 1
    return years


def build_dividend_input(
    code: str,
    name: str,
    div_detail: pd.DataFrame,
    price: float | None,
    div_summary_row: dict[str, Any] | None = None,
    as_of: pd.Timestamp | str | None = None,
) -> dict[str, Any]:
    """把分红明细 + 股价整理成股息打分的输入。"""
    as_of_ts = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.today()
    dps_ttm = ttm_dividend_per_share(div_detail, as_of_ts)
    dy = float("nan")
    if price is not None and np.isfinite(price) and price > 0:
        dy = dps_ttm / float(price) * 100.0
    row = div_summary_row or {}
    return {
        "code": code,
        "name": name,
        "dps_ttm": dps_ttm,
        "price": price,
        "dividend_yield": dy,
        "consecutive_years": consecutive_dividend_years(div_detail, as_of_ts),
        "dividend_times": row.get("dividend_times"),
        "finance_times": row.get("finance_times"),
        "dividend_count_detail": int(len(div_detail)) if div_detail is not None else 0,
        # 每 10 股派息，便于人工核对单位换算是否正确
        "dps_ttm_per10": dps_ttm * 10.0,
    }


class DividendInput(dict):
    """股息打分的输入容器。"""

    FIELDS = (
        "code", "name", "dividend_yield", "consecutive_years",
        "dividend_times", "finance_times", "dps_ttm", "price",
        "dps_ttm_per10", "dividend_count_detail", "bond_yield",
    )

    def __init__(self, **kw: Any):
        super().__init__({k: kw.get(k) for k in self.FIELDS})


def score_dividend(data: DividendInput | dict, cfg) -> RuleResult:
    """股息总分 = 绝对股息率 45% + 连续分红年数 35% + 相对国债利差 20%。"""
    dcfg = cfg.dividend
    d = dict(data)
    notes: list[str] = []
    detail: dict[str, Any] = {}

    dy = d.get("dividend_yield")
    if dy is None or not np.isfinite(dy):
        dy = 0.0

    # ------------------------------------------------------ 绝对股息率
    # 分段而非线性：0~2% 是「聊胜于无」，2~5% 才是真正的现金回报区间，
    # 5% 以上再往上加分的边际意义变小（可能是股价暴跌造成的假高息）。
    level_score = piecewise_linear(
        float(dy),
        [(0.0, 0.0), (1.0, 20.0), (2.0, 45.0), (3.0, 68.0), (4.0, 86.0),
         (float(dcfg.excellent_yield), 100.0), (12.0, 100.0)],
    )
    if float(dy) >= float(dcfg.excellent_yield):
        notes.append(f"股息率 {dy:.2f}% ≥ {dcfg.excellent_yield}%，现金回报丰厚")
    elif float(dy) >= float(dcfg.pass_yield):
        notes.append(f"股息率 {dy:.2f}%，达到及格线 {dcfg.pass_yield}%")
    else:
        notes.append(f"股息率 {dy:.2f}%，低于及格线 {dcfg.pass_yield}%")

    # ------------------------------------------------------ 连续分红年数
    years = int(d.get("consecutive_years") or 0)
    full = float(dcfg.full_credit_years)
    years_score = linear_score(float(years), bad=0.0, good=full)
    if years >= full:
        notes.append(f"已连续分红 {years} 年，股东回报记录扎实")
    elif years >= 5:
        notes.append(f"连续分红 {years} 年，具备分红习惯")
    elif years >= 1:
        notes.append(f"仅连续分红 {years} 年，分红历史偏短")
    else:
        notes.append("无连续分红记录（或从未分红）")

    # ------------------------------------------------------ 相对国债利差
    bond = d.get("bond_yield")
    spread = float("nan")
    if bond is not None and np.isfinite(bond):
        spread = float(dy) - float(bond)
        need = float(dcfg.spread_over_bond)
        spread_score = linear_score(spread, bad=0.0, good=need * 3.0)
        if spread >= need:
            notes.append(f"股息率比 10 年期国债（{bond:.2f}%）高 {spread:.2f} 个百分点，有风险补偿")
        else:
            notes.append(f"股息率相对国债仅高 {spread:.2f} 个百分点（门槛 {need}），补偿不足")
    else:
        spread_score = float("nan")
        notes.append("缺少国债收益率，利差项不参与打分")

    # ------------------------------------------------------ 反向信号
    div_times = d.get("dividend_times")
    fin_times = d.get("finance_times")
    penalty = 0.0
    if (div_times is not None and fin_times is not None
            and np.isfinite(div_times) and np.isfinite(fin_times)):
        detail["dividend_times"] = int(div_times)
        detail["finance_times"] = int(fin_times)
        if fin_times > div_times:
            penalty = 12.0
            notes.append(f"融资 {int(fin_times)} 次 > 分红 {int(div_times)} 次，"
                         f"从市场拿的比给股东的多，扣 {penalty:.0f} 分")

    # 原始值留存，供买卖规则逐条核查与报告展示
    detail["dividend_yield"] = round(float(dy), 4)
    detail["consecutive_years"] = int(years)
    if bond is not None and np.isfinite(float(bond)):
        detail["bond_yield"] = round(float(bond), 4)
    if np.isfinite(spread):
        detail["spread_over_bond"] = round(float(spread), 4)
    detail["dividend_penalty"] = round(float(penalty), 2)

    weights = {
        "level": (float(dcfg.level_weight), level_score),
        "years": (float(dcfg.years_weight), years_score),
        "spread": (float(dcfg.spread_weight), spread_score),
    }
    usable = {k: (w, s) for k, (w, s) in weights.items() if np.isfinite(s)}
    if usable:
        wsum = sum(w for w, _ in usable.values())
        total = sum(w * s for w, s in usable.values()) / wsum
    else:
        total = float("nan")
    total = clamp(total - penalty)

    detail.update(
        {
            "dividend_yield": round(float(dy), 3),
            "dps_ttm": round(float(d.get("dps_ttm") or 0.0), 4),
            "dps_ttm_per10": round(float(d.get("dps_ttm_per10") or 0.0), 3),
            "consecutive_years": years,
            "spread_over_bond": None if not np.isfinite(spread) else round(spread, 3),
            "sub_scores": {
                "level": round(float(level_score), 1),
                "years": round(float(years_score), 1),
                "spread": None if not np.isfinite(spread_score) else round(float(spread_score), 1),
            },
        }
    )

    if not np.isfinite(total):
        state = "数据不足"
    elif total >= 80:
        state = "高股息"
    elif total >= 60:
        state = "分红良好"
    elif total >= 40:
        state = "分红一般"
    else:
        state = "低分红"

    return RuleResult(
        name="股息",
        score=total,
        state=state,
        detail=detail,
        explain=f"{d.get('code', '')} {d.get('name', '')}".strip() + "；" + "；".join(notes),
    )
