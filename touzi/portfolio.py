"""个人持仓：录入成本、盯住盈亏、到点提醒「卖多少股」、并算清扣费后到底赚多少。

## 它和 `touzi/tracker.py` 的分工

* ``tracker.py`` 回答「**这只股票**该买还是该卖」——纯粹基于打分与规则；
* ``portfolio.py``（本模块）回答「**我这一笔**现在值多少、该不该动手、动手卖多少股、
  卖完扣掉手续费净赚多少」——它把台账、现价和规则建议合成一张给你看的表。

## A 股手续费口径（这是最容易算错的地方）

| 项目 | 费率 | 方向 | 备注 |
|---|---|---|---|
| 佣金 | ``commission_rate``（默认 0.025%） | 买 + 卖 | **单笔最低 5 元**，券商可调 |
| 印花税 | ``stamp_duty_rate``（默认 0.05%） | **仅卖出** | 2023-08-28 起由 0.1% 减半 |
| 过户费 | ``transfer_fee_rate``（默认 0.001%） | 买 + 卖 | 沪深两市均收 |

所以同一笔买卖的**净盈利 ≠ (现价 - 成本) × 股数**：买入要加费用、卖出要扣费用。
本模块 `FeeModel.net_profit()` 就是干这件事，并且把每一块钱都写进输出列里，
方便你拿券商对账单核对。

## 提醒口径

* ``清仓`` → 建议卖出 **100%**；
* ``减仓`` → 建议卖出 ``[plan] reduce_ratio``（默认 50%）；
* 有 ``预警`` → 不卖，但在提醒里标出来；
* 其余 → «继续持有，无动作»。

「建议卖出股数」按 100 股向下取整（A 股卖出允许零股，所以不足一手时按实际股数给），
并同时给出「卖出金额 / 卖出费用 / 到账金额 / 扣费后净盈利」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .config import PROJECT_ROOT
from .tracker import (
    HOLDING_COLUMNS,
    _norm_code,
    aggregate_holdings,
    ensure_holdings_template,
    load_holdings,
)

log = logging.getLogger(__name__)

__all__ = [
    "FeeModel",
    "load_fees",
    "Position",
    "Portfolio",
    "PORTFOLIO_COLUMNS",
    "save_holdings",
    "holdings_device",
]

#: 持仓视图的列（顺序即看板/CSV 的展示顺序）
PORTFOLIO_COLUMNS = [
    "代码", "名称", "买入日期", "买入价", "股数",
    "成本金额", "买入费用", "含费成本", "含费成本价",
    "现价", "市值", "浮动盈亏", "浮动盈亏%",
    "提醒", "建议动作", "建议卖出%", "建议卖出股数",
    "卖出金额", "卖出费用", "到账金额", "扣费后净盈利", "扣费后净盈利%",
    "触发的卖出条件", "未通过的门槛", "备注",
]


# ---------------------------------------------------------------------------
# 费用
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeeModel:
    """A 股买卖费用模型。所有费率都是小数（0.00025 = 万分之 2.5）。"""

    commission_rate: float = 0.00025
    commission_min: float = 5.0
    stamp_duty_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    note: str = ""

    # ---- 单边费用 ----
    def commission(self, amount: float) -> float:
        if amount <= 0:
            return 0.0
        return max(float(amount) * self.commission_rate, self.commission_min)

    def buy_fees(self, amount: float) -> float:
        """买入费用 = 佣金 + 过户费（买入不收印花税）。"""
        if amount <= 0:
            return 0.0
        return self.commission(amount) + float(amount) * self.transfer_fee_rate

    def sell_fees(self, amount: float) -> float:
        """卖出费用 = 佣金 + 印花税 + 过户费。"""
        if amount <= 0:
            return 0.0
        return (self.commission(amount)
                + float(amount) * self.stamp_duty_rate
                + float(amount) * self.transfer_fee_rate)

    # ---- 整笔 ----
    def buy_outlay(self, price: float, shares: float) -> float:
        """买入实际掏出去的钱（成交额 + 费用）。"""
        amount = float(price) * float(shares)
        return amount + self.buy_fees(amount)

    def sell_net(self, price: float, shares: float) -> float:
        """卖出实际到账的钱（成交额 − 费用）。"""
        amount = float(price) * float(shares)
        return amount - self.sell_fees(amount)

    def net_profit(self, buy_price: float, sell_price: float, shares: float) -> float:
        """扣掉全部手续费后的净盈利（元）。"""
        return self.sell_net(sell_price, shares) - self.buy_outlay(buy_price, shares)

    def as_dict(self) -> dict[str, float]:
        return {
            "佣金费率": self.commission_rate,
            "佣金最低": self.commission_min,
            "印花税率": self.stamp_duty_rate,
            "过户费率": self.transfer_fee_rate,
        }


def load_fees(cfg) -> FeeModel:
    """从 ``[fees]`` 段读费率；段缺失时用 A 股常见默认值。"""
    raw: dict[str, Any] = {}
    try:
        got = cfg.get("fees", {}) if hasattr(cfg, "get") else {}
        if isinstance(got, dict):
            raw = got
    except Exception:  # noqa: BLE001
        raw = {}

    def f(key: str, default: float) -> float:
        try:
            v = raw.get(key, default)
            return default if v is None else float(v)
        except (TypeError, ValueError):
            return default

    return FeeModel(
        commission_rate=f("commission_rate", 0.00025),
        commission_min=f("commission_min", 5.0),
        stamp_duty_rate=f("stamp_duty_rate", 0.0005),
        transfer_fee_rate=f("transfer_fee_rate", 0.00001),
        note=str(raw.get("note", "") or ""),
    )


# ---------------------------------------------------------------------------
# 持仓
# ---------------------------------------------------------------------------


@dataclass
class Position:
    """一笔持仓的**展示用**快照（真正的合并逻辑在 :func:`touzi.tracker.aggregate_holdings`）。"""

    code: str
    name: str = ""
    buy_date: str = ""
    buy_price: float = float("nan")
    shares: float = 0.0
    note: str = ""

    @property
    def cost_amount(self) -> float:
        if not np.isfinite(self.buy_price) or self.shares <= 0:
            return 0.0
        return float(self.buy_price) * float(self.shares)

    def as_row(self) -> dict[str, Any]:
        return {
            "代码": self.code,
            "名称": self.name,
            "买入日期": self.buy_date,
            "买入价": self.buy_price,
            "股数": self.shares,
            "备注": self.note,
        }


@dataclass
class _Lot:
    """内部：单行台账的解析结果（供 :func:`touzi.tracker.aggregate_holdings` 使用）。"""

    code: str
    name: str = ""
    buy_date: str = ""
    buy_price: float = float("nan")
    shares: float = 0.0
    note: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def _fnum(v: Any, default: float = float("nan")) -> float:
    try:
        f = float(v)
        return f if np.isfinite(f) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 建议卖出
# ---------------------------------------------------------------------------


def sell_ratio_for(action: str, plan: dict[str, Any] | None = None) -> float:
    """动作 → 建议卖出比例。清仓=100%，减仓=``[plan] reduce_ratio``（默认 50%）。"""
    plan = plan or {}
    try:
        reduce_ratio = float(plan.get("reduce_ratio", 0.5) or 0.5)
    except (TypeError, ValueError):
        reduce_ratio = 0.5
    reduce_ratio = min(1.0, max(0.0, reduce_ratio))
    if action == "清仓":
        return 1.0
    if action == "减仓":
        return reduce_ratio
    return 0.0


def plan_sell_shares(shares: float, ratio: float, lot: int = 100) -> int:
    """按比例折算建议卖出股数。

    A 股卖出**允许零股**（不足 100 股必须一次性卖出），所以这里不强制整手，
    但为了下单方便，够一手时按 100 股向下取整；不足一手就按实际股数给。
    """
    sh = int(max(0.0, float(shares)))
    if sh <= 0 or ratio <= 0:
        return 0
    if ratio >= 1.0:
        return sh
    raw = int(sh * float(ratio))
    if raw >= lot:
        raw = raw // lot * lot
    if raw <= 0:
        raw = min(sh, lot)
    return max(1, min(sh, raw))


# ---------------------------------------------------------------------------
# 组装视图
# ---------------------------------------------------------------------------


class Portfolio:
    """把「持仓台账 + 当前打分/跟踪结果」合成一张可直接照做的持仓表。"""

    def __init__(self, cfg, fees: FeeModel | None = None, plan: dict[str, Any] | None = None):
        self.cfg = cfg
        self.fees = fees or load_fees(cfg)
        if plan is None:
            try:
                plan = cfg.get("plan", {}) or {}
            except Exception:  # noqa: BLE001
                plan = {}
        self.plan = plan or {}

    # ---- 单笔计算 ----
    def _one(self, a: dict[str, Any], t: dict[str, Any] | None) -> dict[str, Any]:
        code = a["代码"]
        shares = float(a["股数"])
        cost_amount = float(a["成本金额"])
        buy_fees = self.fees.buy_fees(cost_amount)
        cost_with_fees = cost_amount + buy_fees

        price = _fnum((t or {}).get("现价"))
        action = str((t or {}).get("建议动作") or "")
        triggers = str((t or {}).get("触发的卖出条件") or "")
        failed = str((t or {}).get("未通过的门槛") or "")
        warn = str((t or {}).get("预警") or "")
        has_price = bool(np.isfinite(price) and price > 0)
        has_shares = shares > 0

        market_value = price * shares if has_price and has_shares else float("nan")
        unreal = market_value - cost_amount if np.isfinite(market_value) else float("nan")
        unreal_pct = (unreal / cost_amount * 100.0
                      if np.isfinite(unreal) and cost_amount > 0 else float("nan"))

        ratio = sell_ratio_for(action, self.plan)
        sell_sh = plan_sell_shares(shares, ratio)
        if has_price and sell_sh > 0:
            sell_amount = price * sell_sh
            sell_fee = self.fees.sell_fees(sell_amount)
            net_in = sell_amount - sell_fee
            # 这一部分卖出的净盈利 = 到账金额 − 对应比例的「含费成本」
            # 注意用「含费成本」而不是「成本金额」：买入时付的佣金也要算进成本，
            # 否则会把净盈利算高（小额买入尤其明显，佣金最低 5 元会吃掉不少）。
            part_cost = cost_with_fees * (sell_sh / shares) if shares > 0 else float("nan")
            net_profit = net_in - part_cost if np.isfinite(part_cost) else float("nan")
        else:
            sell_amount = sell_fee = net_in = net_profit = float("nan")
        # 净收益率按**卖出的那一部分**算，才和「净盈利」对得上
        net_profit_pct = (net_profit / (cost_with_fees * (sell_sh / shares)) * 100.0
                          if np.isfinite(net_profit) and shares > 0 and sell_sh > 0
                          and cost_with_fees > 0 else float("nan"))

        # ---- 提醒文案 ----
        alert, level = self._alert(action, ratio, sell_sh, net_profit, net_profit_pct,
                                   triggers, warn, has_price, has_shares)

        return {
            "代码": code,
            "名称": a["名称"] or (t or {}).get("名称") or "",
            "买入日期": a["买入日期"],
            "买入价": round(float(a["买入价"]), 4) if np.isfinite(a["买入价"]) else None,
            "股数": int(shares) if has_shares else None,
            "成本金额": round(cost_amount, 2) if has_shares else None,
            "买入费用": round(buy_fees, 2) if has_shares else None,
            "含费成本": round(cost_with_fees, 2) if has_shares else None,
            "含费成本价": round(cost_with_fees / shares, 4) if has_shares else None,
            "现价": round(price, 3) if np.isfinite(price) else None,
            "市值": round(market_value, 2) if np.isfinite(market_value) else None,
            "浮动盈亏": round(unreal, 2) if np.isfinite(unreal) else None,
            "浮动盈亏%": round(unreal_pct, 2) if np.isfinite(unreal_pct) else None,
            "提醒": alert,
            "_level": level,
            "建议动作": action or "无法评估",
            "建议卖出%": round(ratio * 100.0, 1) if ratio > 0 else 0.0,
            "建议卖出股数": sell_sh,
            "卖出金额": round(sell_amount, 2) if np.isfinite(sell_amount) else None,
            "卖出费用": round(sell_fee, 2) if np.isfinite(sell_fee) else None,
            "到账金额": round(net_in, 2) if np.isfinite(net_in) else None,
            "扣费后净盈利": round(net_profit, 2) if np.isfinite(net_profit) else None,
            "扣费后净盈利%": round(net_profit_pct, 2) if np.isfinite(net_profit_pct) else None,
            "触发的卖出条件": triggers,
            "未通过的门槛": failed,
            "备注": a.get("备注") or "",
        }

    @staticmethod
    def _alert(action: str, ratio: float, sell_sh: int, net_profit: float,
               net_pct: float, triggers: str, warn: str,
               has_price: bool, has_shares: bool) -> tuple[str, str]:
        """生成人话提醒，返回 (文案, 级别)。级别：act 要动手 / watch 留意 / hold 持有 / na 无数据。"""
        money = ""
        if np.isfinite(net_profit):
            money = f"。这部分扣费后净盈利 ¥{net_profit:,.2f}（{net_pct:+.2f}%）"
        if not has_price:
            return "没有现价（不在本次打分范围内，或已停牌），无法计算盈亏", "na"
        if not has_shares:
            return ("没填「股数」，只给买卖建议、不算金额。"
                    "想看到「卖多少股 / 净赚多少」，请在台账里补上股数。"), "watch"
        if action == "清仓":
            return (f"🚨 **清仓**：卖出全部 {sell_sh} 股（100%）。触发："
                    f"{triggers or '详见规则'}{money}"), "act"
        if action == "减仓":
            return (f"⚠️ **减仓 {ratio:.0%}**：卖出 {sell_sh} 股。触发："
                    f"{triggers or '详见规则'}{money}"), "act"
        if action == "加仓":
            return "买入门槛全部通过、无卖出条件触发，且未到止损止盈——可考虑加仓", "watch"
        if warn:
            return f"继续持有。留意：{warn}", "watch"
        if action == "持有":
            return "继续持有，卖出条件均未触发", "hold"
        return f"继续持有（规则给的动作是「{action}」）", "hold"

    # ---- 整表 ----
    def _summarize(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """汇总。只对**有现价**的持仓求和：没有现价的行（停牌 / 不在本次打分范围）
        如果把成本算进总数，会出现「总浮动盈亏 -87%」这种吓人但无意义的数字。"""

        def _sum(key: str) -> tuple[float, int]:
            vals = [r.get(key) for r in rows if r.get("市值") is not None]
            nums = [float(v) for v in vals if v is not None and np.isfinite(float(v))]
            return (float(sum(nums)), len(nums))

        tot_cost, n_cost = _sum("含费成本")
        tot_mv, n_mv = _sum("市值")
        tot_unreal = tot_mv - tot_cost
        return {
            "持仓只数": len(rows),
            "有现价只数": n_mv,
            "无现价只数": len(rows) - n_mv,
            "总成本(含费)": round(tot_cost, 2),
            "总市值": round(tot_mv, 2),
            "总浮动盈亏": round(tot_unreal, 2),
            "总浮动盈亏%": round(tot_unreal / tot_cost * 100, 2) if tot_cost > 0 else None,
            "需要动手": sum(1 for r in rows if r.get("_level") == "act"),
            "留意": sum(1 for r in rows if r.get("_level") == "watch"),
            "无法评估": sum(1 for r in rows if r.get("_level") == "na"),
            "fees": self.fees.as_dict(),
            "reduce_ratio": sell_ratio_for("减仓", self.plan),
        }

    def requote(self, df: pd.DataFrame, quotes: dict[str, dict[str, Any]]) -> pd.DataFrame:
        """用**秒级实时报价**重算一份已经建好的持仓表。

        ``quotes`` 形如 ``{code: {"price": 1255.79, "pe_ttm": 19.28, "pb": 6.25}}``。

        只重算与现价有关的列（现价 / 市值 / 浮动盈亏 / 卖出金额 / 到账金额 /
        扣费后净盈利 / 提醒）。**动作（清仓/减仓/持有）不在这里改**——那取决于估值
        分位与总分，要动就得重跑整个打分流程，那是后台重算的活。所以这个方法的定位是
        「让页面上你已经看到的结论，用最新价格重新算一遍钱」，不是「重新得出结论」。
        """
        if df is None or not len(df):
            return df
        rows = df.to_dict("records")
        changed = False
        for r in rows:
            q = quotes.get(str(r.get("代码"))) or {}
            price = _fnum(q.get("price"))
            if not (np.isfinite(price) and price > 0):
                continue
            a = {
                "代码": r.get("代码"),
                "名称": r.get("名称"),
                "买入日期": r.get("买入日期"),
                "买入价": _fnum(r.get("买入价")),
                "股数": _fnum(r.get("股数"), 0.0),
                "成本金额": _fnum(r.get("成本金额"), 0.0),
                "备注": r.get("备注"),
            }
            t = {
                "现价": price,
                "名称": r.get("名称"),
                "建议动作": r.get("建议动作"),
                "触发的卖出条件": r.get("触发的卖出条件"),
                "未通过的门槛": r.get("未通过的门槛"),
                "预警": r.get("预警"),
            }
            fresh = self._one(a, t)
            for k, v in fresh.items():
                if k in r:
                    r[k] = v
            changed = True
        if not changed:
            return df

        out = pd.DataFrame(rows)
        for col in df.columns:
            if col not in out.columns:
                out[col] = None
        out = out[list(df.columns)]
        out.attrs["summary"] = self._summarize(rows)
        return out

    def build(self, holdings: pd.DataFrame | None = None,
              tracking: pd.DataFrame | None = None) -> pd.DataFrame:
        """生成持仓视图。

        Args:
            holdings: 台账（``load_holdings`` 的产物）；``None`` 时自己读文件。
            tracking: 跟踪表（``output/tracking.csv``）；用来取现价与建议动作。
        """
        if holdings is None:
            holdings = load_holdings(self.cfg)
        agg = aggregate_holdings(holdings)

        tmap: dict[str, dict[str, Any]] = {}
        if tracking is not None and len(tracking):
            for _, r in tracking.iterrows():
                c = _norm_code(r.get("代码"))
                if c:
                    tmap[c] = r.to_dict()

        rows = [self._one(a, tmap.get(code)) for code, a in sorted(agg.items())]
        df = pd.DataFrame(rows)
        for col in PORTFOLIO_COLUMNS:
            if col not in df.columns:
                df[col] = None
        keep = [c for c in df.columns if c not in PORTFOLIO_COLUMNS]  # _level 等
        df = df[PORTFOLIO_COLUMNS + [c for c in keep if c.startswith("_")]]
        df.attrs["summary"] = self._summarize(rows)
        return df


    def save(self, df: pd.DataFrame, path: Path | str | None = None) -> Path:
        p = Path(path) if path is not None else (PROJECT_ROOT / "output" / "portfolio.csv")
        p.parent.mkdir(parents=True, exist_ok=True)
        out = df[[c for c in PORTFOLIO_COLUMNS if c in df.columns]]
        out.to_csv(p, index=False, encoding="utf-8-sig")
        log.info("持仓表已写出：%s（%d 行）", p, len(out))
        return p


# ---------------------------------------------------------------------------
# 台账写回（网站表单用）
# ---------------------------------------------------------------------------


def holdings_device(cfg) -> Path:
    """持仓台账路径（供网站读写）。"""
    try:
        rel = (cfg.get("plan", {}) or {}).get("holdings_file") or "config/holdings.csv"
    except Exception:  # noqa: BLE001
        rel = "config/holdings.csv"
    p = Path(str(rel))
    return p if p.is_absolute() else PROJECT_ROOT / p


def save_holdings(cfg, rows: Iterable[dict[str, Any]], path: Path | str | None = None) -> Path:
    """把持仓行写回台账。

    ⚠️ 这里**不做** ``load_holdings`` 的过滤：用户刚在网页上敲进去的东西必须原样保存，
    否则一刷新就"丢"了。非法行由读取端跳过即可。
    """
    p = Path(path) if path is not None else holdings_device(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean: list[dict[str, Any]] = []
    for r in rows:
        clean.append({c: r.get(c, "") for c in HOLDING_COLUMNS})
    df = pd.DataFrame(clean, columns=HOLDING_COLUMNS)
    df.to_csv(p, index=False, encoding="utf-8-sig")
    log.info("持仓台账已保存：%s（%d 行）", p, len(df))
    return p


def upsert_holding(cfg, row: dict[str, Any]) -> pd.DataFrame:
    """新增或更新一行（按「代码 + 买入日期 + 买入价」判定是否为同一笔）。"""
    ensure_holdings_template(cfg)
    p = holdings_device(cfg)
    try:
        cur = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
    except Exception:  # noqa: BLE001
        cur = pd.DataFrame(columns=HOLDING_COLUMNS)
    for c in HOLDING_COLUMNS:
        if c not in cur.columns:
            cur[c] = ""
    cur = cur[HOLDING_COLUMNS]

    code = _norm_code(row.get("代码"))
    if not code:
        raise ValueError("「代码」必须是 6 位股票代码，例如 600519")

    new = {c: row.get(c, "") for c in HOLDING_COLUMNS}
    new["代码"] = code
    key = (code, str(row.get("买入日期") or ""), str(row.get("买入价") or ""))
    hit = cur.index[
        (cur["代码"].map(_norm_code) == key[0])
        & (cur["买入日期"].astype(str) == key[1])
        & (cur["买入价"].astype(str) == key[2])
    ]
    if len(hit):
        for c in HOLDING_COLUMNS:
            cur.loc[hit[0], c] = new[c]
    else:
        cur = pd.concat([cur, pd.DataFrame([new])], ignore_index=True)
    save_holdings(cfg, cur.to_dict("records"), p)
    return cur


def delete_holding(cfg, code: str, buy_date: str = "", buy_price: str = "") -> pd.DataFrame:
    """删除一行；不传日期/价格时删除该代码的全部持仓。"""
    p = holdings_device(cfg)
    try:
        cur = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=HOLDING_COLUMNS)
    for c in HOLDING_COLUMNS:
        if c not in cur.columns:
            cur[c] = ""
    cur = cur[HOLDING_COLUMNS]

    c_norm = _norm_code(code)
    keep = ~(cur["代码"].map(_norm_code) == c_norm)
    if buy_date:
        keep &= ~(cur["买入日期"].astype(str) == str(buy_date))
    if buy_price:
        keep &= ~(cur["买入价"].astype(str) == str(buy_price))
    out = cur[keep]
    save_holdings(cfg, out.to_dict("records"), p)
    return out
