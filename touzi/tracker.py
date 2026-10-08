"""个股跟踪表——把买卖规则套到「你实际持有的 + 值得盯的」股票上，逐只给动作。

## 它解决什么

``touzi/plans.py`` 只回答「这一只该买还是该卖」，本模块负责：

1. **读你的持仓台账** ``config/holdings.csv``（可以自己用 Excel 维护），
   算出浮动盈亏，并判断是否触发止损/止盈；
2. 对**持仓股**输出 清仓/减仓/持有/加仓；
3. 对**未持仓但门槛接近**的股票输出 建仓/接近建仓/观望/回避，形成买入候选清单；
4. 落盘到 ``output/tracking.csv``，并给看板一个显著章节。

## 持仓台账格式

``config/holdings.csv`` —— 没有这个文件时只输出买入候选，不会报错。
首次运行会自动生成一个带表头的模板。

=======  ================================================
列       说明
代码     6 位股票代码，如 ``600519``（也接受 ``sh600519``）
名称     随便写，只用于显示；留空会用系统抓到的名称
买入日期  ``YYYY-MM-DD``
买入价    成交均价，用于算浮盈浮亏与止损/止盈
股数      股数，可选（留空不影响买卖建议）
备注      自己的记录，系统不读
=======  ================================================

台账里的行如果「代码」不是 6 位数字（例如模板里的示例行、或者误填的文字），
会被自动跳过并在日志里说明，免得污染跟踪结果。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .config import PROJECT_ROOT
from .plans import PlanDecision, decide, plan_settings

log = logging.getLogger(__name__)

__all__ = [
    "HOLDING_COLUMNS",
    "load_holdings",
    "ensure_holdings_template",
    "Tracker",
    "TRACKING_COLUMNS",
]

HOLDING_COLUMNS = ["代码", "名称", "买入日期", "买入价", "股数", "备注"]

TRACKING_COLUMNS = [
    "代码", "名称", "行业", "建议动作",
    "现价", "持仓成本", "浮动盈亏%",
    "总分", "评级",
    "PE_TTM", "PB", "股息率%", "PE历史分位", "PB历史分位", "PE×PB",
    "宏观背景分",
    "未通过的门槛", "触发的卖出条件", "预警", "理由",
]


def _holdings_path(cfg) -> Path:
    p = plan_settings(cfg).get("holdings_file") or "config/holdings.csv"
    path = Path(str(p))
    return path if path.is_absolute() else PROJECT_ROOT / path


def ensure_holdings_template(cfg) -> Path:
    """持仓台账不存在时创建带表头的模板。

    模板里那一行的「代码」故意写成 ``示例-这行会被跳过``（**不是** 6 位数字），
    这样它既能提示填写格式，又不会被当成一笔真实持仓——否则每个首次运行的人
    都会看到一只莫名其妙的股票被建议「清仓」。
    """
    path = _holdings_path(cfg)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        [
            {"代码": "示例-这行会被跳过", "名称": "贵州茅台（示例，请照这个格式填你的持仓）",
             "买入日期": "2025-01-10", "买入价": 1450.0, "股数": 100,
             "备注": "把「代码」换成真实的 6 位代码；不需要的行直接删掉"},
        ],
        columns=HOLDING_COLUMNS,
    )
    df.to_csv(path, index=False, encoding="utf-8-sig")
    log.info("已生成持仓台账模板：%s（里面那行的「代码」不是 6 位数字，会被自动跳过）", path)
    return path


def _norm_code(v: Any) -> str:
    """把 ``sh600519`` / ``600519.SH`` / ``600519`` 统一成 6 位数字，非法返回空串。"""
    s = str(v or "").strip()
    if not s:
        return ""
    for ch in ("sh", "sz", "bj", "SH", "SZ", "BJ"):
        s = s.replace(ch, "")
    s = s.split(".")[0].strip()
    return s if s.isdigit() and len(s) == 6 else ""


def load_holdings(cfg, path: Path | str | None = None) -> pd.DataFrame:
    """读持仓台账；文件不存在就返回空表（不报错）。"""
    p = Path(path) if path is not None else _holdings_path(cfg)
    if not p.exists():
        return pd.DataFrame(columns=HOLDING_COLUMNS)

    try:
        df = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(p, dtype=str, encoding="gbk")
    except Exception as exc:  # pragma: no cover - 文件损坏时不该中断整个流程
        log.warning("读持仓台账失败（%s）：%s", p, exc)
        return pd.DataFrame(columns=HOLDING_COLUMNS)

    for col in HOLDING_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    codes = df["代码"].map(_norm_code)
    bad = int((codes == "").sum())
    if bad:
        log.info("持仓台账有 %d 行「代码」不是 6 位数字，已跳过：%s",
                 bad, list(df.loc[codes == "", "代码"].astype(str))[:5])
    df = df[codes != ""].copy()
    df["代码"] = codes[codes != ""].values
    df["买入价"] = pd.to_numeric(df["买入价"], errors="coerce")
    df["买入日期"] = pd.to_datetime(df["买入日期"], errors="coerce")
    return df.reset_index(drop=True)


def aggregate_holdings(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """把同一代码的多笔持仓合成一条：股数相加、成本用**加权平均**。

    为什么必须合并：你分两次买入同一只股票时，止损/止盈要对着**整体成本**算，
    只取第一笔会得出错误的盈亏。台账里想保留批号就多写几行，本函数负责合并。

    返回 ``{代码: {名称, 买入日期, 买入价, 股数, 成本金额, 备注, lots, 最早买入日, 最晚买入日}}``。
    股数缺失或买入价非法的行会被跳过（它们算不出成本）。
    """
    agg: dict[str, dict[str, Any]] = {}
    if df is None or len(df) == 0:
        return agg

    for _, r in df.iterrows():
        code = _norm_code(r.get("代码"))
        if not code:
            continue
        price = pd.to_numeric(r.get("买入价"), errors="coerce")
        shares = pd.to_numeric(r.get("股数"), errors="coerce")
        price = float(price) if pd.notna(price) else float("nan")
        shares = float(shares) if pd.notna(shares) else 0.0
        # 只有「买入价」是必需的：股数可以留空（那样只给买卖建议，不算金额盈亏）
        if not np.isfinite(price):
            continue
        if shares < 0:
            shares = 0.0
        d = r.get("买入日期")
        date = str(pd.Timestamp(d).date()) if pd.notna(d) else ""

        a = agg.setdefault(code, {
            "代码": code, "名称": "", "买入日期": date, "买入价": float("nan"),
            "股数": 0.0, "成本金额": 0.0, "备注": "", "lots": 0,
            "最早买入日": date, "最晚买入日": date, "_价格样本": [],
        })
        a["股数"] += shares
        a["成本金额"] += price * shares if shares else 0.0
        a["lots"] += 1
        a["_价格样本"].append(price)
        if str(r.get("名称") or "").strip() and not a["名称"]:
            a["名称"] = str(r.get("名称")).strip()
        if str(r.get("备注") or "").strip():
            a["备注"] = str(r.get("备注")).strip()
        if date:
            a["最早买入日"] = min(a["最早买入日"] or date, date)
            a["最晚买入日"] = max(a["最晚买入日"] or date, date)

    for a in agg.values():
        if a["股数"] > 0:
            a["买入价"] = a["成本金额"] / a["股数"]
        else:
            # 只有价格、没有股数：取最后一笔的价格，仅供「卖出触发」判断用
            a["买入价"] = a["_价格样本"][-1] if a["_价格样本"] else float("nan")
        a["买入日期"] = a["最早买入日"] or ""
        a.pop("_价格样本", None)
    return agg


class Tracker:
    """把 :func:`touzi.plans.decide` 套到一批打分结果上，产出跟踪表。"""

    def __init__(self, cfg, macro_score: float = float("nan"),
                 holdings: pd.DataFrame | None = None):
        self.cfg = cfg
        self.macro_score = float(macro_score)
        self._settings = plan_settings(cfg)
        self.holdings = holdings if holdings is not None else load_holdings(cfg)
        self._agg_cache: dict[str, dict[str, Any]] | None = None

    # ------------------------------------------------------------------ 内部
    def _holdings_agg(self) -> dict[str, dict[str, Any]]:
        """按代码合并后的持仓（加权平均成本）。懒加载并缓存，避免每只股票重算一遍。"""
        if getattr(self, "_agg_cache", None) is None:
            self._agg_cache = aggregate_holdings(self.holdings)
        return self._agg_cache

    def _holding_of(self, code: str) -> dict[str, Any] | None:
        a = self._holdings_agg().get(str(code))
        if not a or not np.isfinite(a["买入价"]):
            return None
        return {
            "代码": str(code),
            "名称": a["名称"],
            "买入日期": a["买入日期"],
            "买入价": float(a["买入价"]),
            "股数": float(a["股数"]),
            "备注": a["备注"],
            "批数": a["lots"],
        }

    @staticmethod
    def _rank_pct_map(scores: list[Any]) -> dict[str, float]:
        """按总分降序给出每只股票的分位排名（0 = 第一名）。"""
        vals = [(getattr(s, "code", ""), getattr(s, "total", float("nan"))) for s in scores]
        vals = [(c, float(t)) for c, t in vals if c and np.isfinite(float(t or float("nan")))]
        vals.sort(key=lambda x: -x[1])
        n = max(1, len(vals))
        return {c: i / n for i, (c, _) in enumerate(vals)}

    def _row(self, score, decision: PlanDecision, rank_pct: float | None) -> dict[str, Any]:
        detail = (score.parts.get("valuation").detail if score.parts.get("valuation") else {}) or {}
        pe_pctl = detail.get("pe_percentile")
        pb_pctl = detail.get("pb_percentile")
        pe = getattr(score, "pe_ttm", float("nan"))
        pb = getattr(score, "pb", float("nan"))
        gp = detail.get("graham_product")
        if gp is None and not _nan(pe) and not _nan(pb) and float(pe) > 0 and float(pb) > 0:
            gp = float(pe) * float(pb)

        holding = decision.holding
        warn = [c.label for c in decision.by_level("预警")]
        # 排名门槛默认关闭（buy_max_rank_pct=0）时不显示名次，否则「排名前 0%」会误导
        try:
            _rank_on = float((self.cfg.get("plan", {}) or {}).get("buy_max_rank_pct") or 0.0) > 0.0
        except Exception:  # noqa: BLE001
            _rank_on = False
        show_rank = _rank_on and rank_pct is not None
        ratio = (f"{decision.buy_hits}/{len(decision.buy_checks)}"
                 + (f"（排名前 {rank_pct:.0%}）" if show_rank else ""))
        return {
            "代码": score.code,
            "名称": score.name,
            "行业": getattr(score, "industry", "") or "",
            "建议动作": decision.action,
            "现价": _r(getattr(score, "price", float("nan")), 2),
            "持仓成本": None if not holding else _r(holding.get("买入价"), 2),
            "浮动盈亏%": None if decision.pnl is None else round(decision.pnl * 100, 2),
            "总分": _r(decision.total, 2),
            "评级": getattr(score, "grade", ""),
            "PE_TTM": _r(pe, 2),
            "PB": _r(pb, 2),
            "股息率%": _r(getattr(score, "dividend_yield", float("nan")), 3),
            "PE历史分位": None if pe_pctl is None else _pct(pe_pctl),
            "PB历史分位": None if pb_pctl is None else _pct(pb_pctl),
            "PE×PB": _r(gp, 2),
            "宏观背景分": _r(self.macro_score, 2),
            "未通过的门槛": "；".join(c.label for c in decision.failed_checks) or "（全部通过）",
            "触发的卖出条件": "；".join(f"[{c.level}] {c.label}" for c in decision.triggered) or "",
            "预警": "；".join(warn),
            "理由": " ｜ ".join(decision.reasons),
            "_buy_ratio": ratio,
            "_decision": decision,
        }

    # ------------------------------------------------------------------ 对外
    def build(self, scores: Iterable[Any], top: int | None = None) -> pd.DataFrame:
        """生成跟踪表。

        Args:
            scores: :class:`touzi.screener.StockScore` 列表（按总分降序更佳）。
            top: 未持仓的候选最多列多少只；``None`` 用 ``[plan] tracking_top``。

        Returns:
            DataFrame，列为 :data:`TRACKING_COLUMNS`；另在 ``attrs["decisions"]``
            里带上 :class:`PlanDecision` 列表与 ``attrs["holdings_missing"]``。
        """
        items = [s for s in scores if s is not None]
        rank_pct = self._rank_pct_map(items)
        top = int(top if top is not None else self._settings.get("tracking_top", 40))

        decisions: list[PlanDecision] = []
        rows: list[dict[str, Any]] = []
        held_codes: set[str] = set()

        # 1) 持仓股优先，不受 top 限制——每一只持仓都必须给动作
        for s in items:
            h = self._holding_of(getattr(s, "code", ""))
            if h is None:
                continue
            d = decide(s, self.cfg, macro_score=self.macro_score, holding=h,
                       rank_pct=rank_pct.get(s.code), price=getattr(s, "price", None))
            decisions.append(d)
            rows.append(self._row(s, d, rank_pct.get(s.code)))
            held_codes.add(s.code)

        # 2) 未持仓：优先给「已触发建仓」的，然后是「接近建仓」，最后按总分补足到 top 只
        candidates = [s for s in items if getattr(s, "code", "") not in held_codes]
        cand_decisions: list[tuple[Any, PlanDecision]] = []
        for s in candidates:
            d = decide(s, self.cfg, macro_score=self.macro_score,
                       rank_pct=rank_pct.get(s.code), price=getattr(s, "price", None))
            cand_decisions.append((s, d))

        # 动作优先级：建仓 > 接近建仓 > 观望 > 回避（同级内按总分降序）
        _PRIORITY = {"建仓": 0, "接近建仓": 1, "观望": 2, "回避": 3}
        cand_decisions.sort(key=lambda sd: (
            _PRIORITY.get(sd[1].action, 9),
            -(float(getattr(sd[0], "total", 0) or 0)),
        ))
        chosen = cand_decisions[: max(0, top)]
        buy_n = sum(1 for _, d in chosen if d.action == "建仓")
        near_n = sum(1 for _, d in chosen if d.action == "接近建仓")
        if buy_n or near_n:
            log.info("跟踪清单：建仓 %d 只、接近建仓 %d 只（共 %d 只）", buy_n, near_n, len(chosen))

        for s, d in chosen:
            decisions.append(d)
            rows.append(self._row(s, d, rank_pct.get(s.code)))

        # 3) 台账里有、但打分结果里没有的持仓（例如被预筛剔除了）——如实报出来
        missing: list[str] = []
        held_in_ledger = set(self.holdings["代码"].astype(str)) if len(self.holdings) else set()
        for code in sorted(held_in_ledger - held_codes):
            missing.append(code)
            rows.append({
                "代码": code, "名称": "", "行业": "", "建议动作": "无法评估",
                "现价": None, "持仓成本": None, "浮动盈亏%": None,
                "总分": None, "评级": "", "PE_TTM": None, "PB": None, "股息率%": None,
                "PE历史分位": None, "PB历史分位": None, "PE×PB": None,
                "宏观背景分": _r(self.macro_score, 2),
                "未通过的门槛": "", "触发的卖出条件": "", "预警": "",
                "理由": "该股不在本次打分结果里（被预筛剔除、代码写错、或本次未列入候选），"
                        "请核对代码与筛选条件",
                "_buy_ratio": "", "_decision": None,
            })

        df = pd.DataFrame(rows)
        for col in TRACKING_COLUMNS:
            if col not in df.columns:
                df[col] = None
        df = df[TRACKING_COLUMNS + ["_buy_ratio", "_decision"]]
        df.attrs["decisions"] = decisions
        df.attrs["holdings_missing"] = missing
        df.attrs["held_count"] = len(held_codes)
        return df

    def save(self, df: pd.DataFrame, path: Path | str | None = None) -> Path:
        """把跟踪表落盘（不含内部列）。"""
        rel = self._settings.get("tracking_file") or "output/tracking.csv"
        p = Path(path) if path is not None else (PROJECT_ROOT / str(rel))
        p.parent.mkdir(parents=True, exist_ok=True)
        out = df[[c for c in TRACKING_COLUMNS if c in df.columns]]
        out.to_csv(p, index=False, encoding="utf-8-sig")
        log.info("跟踪表已写出：%s（%d 行）", p, len(out))
        return p


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _nan(v: Any) -> bool:
    try:
        return not np.isfinite(float(v))
    except (TypeError, ValueError):
        return True


def _r(v: Any, nd: int) -> float | None:
    return None if _nan(v) else round(float(v), nd)


def _pct(v: Any) -> str | None:
    return None if _nan(v) else f"{float(v) * 100:.0f}%"
