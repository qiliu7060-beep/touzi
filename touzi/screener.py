"""个股筛选与打分：把「估值 + 质量 + 股息 + 政策」四项合成一只股票的总分。

## 一、这个模块在整个系统里的位置

    build_macro_context()   →  宏观背景分 / 目标仓位      （买多少）
    Screener.run()          →  个股总分 / 排名            （买什么）

两者互不混合：宏观差不代表某只股票变贵了。用户要的「长期投资判断」是由
个股总分给出的，宏观分只决定这个组合整体用几成仓。

## 二、四项权重与理由

    valuation 0.45 > quality 0.30 > dividend 0.15 > policy 0.10

- **估值 0.45 最高**：用户点名市盈率、市净率、股息率，估值是长期回报最主要的
  解释变量（买得便宜本身就是安全边际）。但估值单独用会掉进价值陷阱，
  所以必须有质量项压制。
- **质量 0.30**：把「便宜但有病」的公司剔除。低 PE 若来自盈利即将崩塌，
  那不是便宜而是陷阱。
- **股息 0.15**：现金分红是长期投资唯一确定的现金流，也是财报造假最难伪造的部分。
- **政策 0.10 最低**：政策是**方向性**信号，无法量化到具体公司，且本模块的
  底层表是人工判断（见 signals/policy.py 的说明）。给低权重是刻意的克制——
  政策用来做「同分优先」，而不是用来把一家基本面平庸的公司推上榜首。

缺失项在可用项之间重新归一化，并在 ``data_flags`` 里记录，便于事后发现
「这只股票其实只有 2 项参与了打分」。**某一项完全缺失时不按 0 分算**，
否则数据不全的股票会被系统性低估。

## 三、先粗筛再精算

全市场 5500 只股票，每只要抓 4 个接口（PE 历史、PB 历史、分红明细、财务指标），
全抓一轮 2 万次请求不现实。所以先用快照里的现成字段做**硬性排除**
（ST、上市不满 1 年、成交额过低、负债率过高、PE 为负），
再按流通市值排序取前 ``max_candidates`` 只做精算。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from .data.stock import (
    fetch_dividend_detail,
    fetch_dividend_summary,
    fetch_financial_indicators,
    fetch_industry_map,
    fetch_market_snapshot,
    fetch_price_history,
    fetch_stock_list,
    fetch_valuation_history,
    parallel_map,
    plain_code,
)
from .signals.dividend import build_dividend_input, score_dividend
from .signals.macro import RuleResult
from .signals.policy import load_industry_policy, load_policy_events, score_policy
from .signals.quality import QualityInput, score_quality, summarize_financials
from .signals.valuation import ValuationInput, percentile_of, score_valuation
from .util import clamp

__all__ = ["StockScore", "Screener"]

LOG = logging.getLogger(__name__)

STOCK_PARTS = ("valuation", "quality", "dividend", "policy")
PART_LABELS = {
    "valuation": "估值",
    "quality": "质量",
    "dividend": "股息",
    "policy": "政策",
}


def _grade(total: float) -> str:
    if not np.isfinite(total):
        return "N/A"
    if total >= 80:
        return "A+"
    if total >= 72:
        return "A"
    if total >= 62:
        return "B"
    if total >= 50:
        return "C"
    return "D"


def _last_value(series: Any, fallback: Any = float("nan")) -> float:
    """取序列最后一个有效值；取不到就返回 fallback。"""
    if series is None or len(series) == 0:
        return fallback
    s = pd.to_numeric(pd.Series(series).squeeze(), errors="coerce").dropna()
    return float(s.iloc[-1]) if len(s) else fallback


def _clip_history(obj: Any, as_of: pd.Timestamp) -> Any:
    """把一份历史数据截断到 as_of —— 回测里防未来函数的关键动作。

    同时兼容两种形态：财务表（带 ``report_date`` 列）与时间序列（DatetimeIndex）。
    """
    if obj is None or len(obj) == 0:
        return obj
    if isinstance(obj, pd.DataFrame) and "report_date" in obj.columns:
        d = pd.to_datetime(obj["report_date"], errors="coerce")
        return obj[d <= as_of]
    idx = getattr(obj, "index", None)
    if isinstance(idx, pd.DatetimeIndex):
        return obj[idx <= as_of]
    return obj


@dataclass
class StockScore:
    """一只股票的打分结果。"""

    code: str
    name: str
    industry: str
    price: float = float("nan")
    pe_ttm: float = float("nan")
    pb: float = float("nan")
    dividend_yield: float = float("nan")
    float_mktcap_yi: float = float("nan")
    parts: dict[str, RuleResult] = field(default_factory=dict)
    total: float = float("nan")
    grade: str = "N/A"
    data_flags: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "代码": self.code,
            "名称": self.name,
            "行业": self.industry,
            "总分": None if not np.isfinite(self.total) else round(self.total, 2),
            "评级": self.grade,
            "股价": None if not np.isfinite(self.price) else round(self.price, 2),
            "PE_TTM": None if not np.isfinite(self.pe_ttm) else round(self.pe_ttm, 2),
            "PB": None if not np.isfinite(self.pb) else round(self.pb, 2),
            "股息率%": None if not np.isfinite(self.dividend_yield) else round(self.dividend_yield, 3),
            "流通市值(亿)": None if not np.isfinite(self.float_mktcap_yi) else round(self.float_mktcap_yi, 1),
        }
        for key in STOCK_PARTS:
            res = self.parts.get(key)
            row[f"{PART_LABELS[key]}分"] = (
                None if res is None or not np.isfinite(res.score) else round(float(res.score), 1)
            )
            row[f"{PART_LABELS[key]}状态"] = None if res is None else res.state
        return row

    def explain_text(self) -> str:
        lines = [
            f"【{self.code} {self.name}】{self.industry}  "
            f"总分 {self.total:.1f}（{self.grade}）"
        ]
        for key in STOCK_PARTS:
            res = self.parts.get(key)
            if res is None:
                lines.append(f"  · {PART_LABELS[key]}：未参与打分")
                continue
            lines.append(f"  · {PART_LABELS[key]} {res.score:.1f}／{res.state}")
            lines.append(f"      {res.explain}")
        return "\n".join(lines)


class Screener:
    """个股筛选器。"""

    def __init__(self, cfg, cache=None, proxy: str | None = None, as_of: pd.Timestamp | str | None = None):
        from .util import Cache, apply_proxy_env, is_live, resolve_ttls

        self.cfg = cfg
        self.proxy = proxy if proxy is not None else cfg.general.get("proxy") or None
        apply_proxy_env(self.proxy)
        if cache is None:
            cache = Cache(cfg.resolve(cfg.general.get("cache_dir", "data_cache")))
        self.cache = cache
        self.as_of = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.today()
        # 实时模式：各类数据的缓存 TTL 压到分钟级，且用实时价/实时 PE-PB 参与分位
        self.live = is_live(cfg)
        self.ttls = resolve_ttls(cfg)
        self.quotes_at: pd.Timestamp | None = None   # 本次快照的抓取时刻
        self._policy_table = None
        self._events = None
        self._universe: pd.DataFrame | None = None

    # ------------------------------------------------------------------ 数据准备
    def load_universe(self) -> pd.DataFrame:
        """取全市场清单 + 快照 + 行业归属，合成候选池底表。"""
        if self._universe is not None:
            return self._universe
        ttl_macro = self.ttls["macro"]
        ttl_price = self.ttls["quote"]

        snap = fetch_market_snapshot(self.cache, self.proxy, ttl_days=ttl_price)
        self.quotes_at = self.cache.saved_at("market_snapshot_tx") or pd.Timestamp.now()
        names = fetch_stock_list(self.cache, self.proxy, ttl_days=ttl_macro)
        uni = snap.merge(names[["code", "name", "list_date", "exchange"]], on="code", how="left",
                         suffixes=("", "_list"))
        if "name_list" in uni.columns:
            uni["name"] = uni["name"].fillna(uni["name_list"])
            uni = uni.drop(columns=["name_list"])
        try:
            ind = fetch_industry_map(self.cache, self.proxy, ttl_days=self.ttls["fund"])
            if len(ind):
                uni = uni.merge(ind[["code", "industry"]], on="code", how="left")
        except Exception as exc:
            LOG.warning("行业归属抓取失败，政策模块将按未知行业处理：%s", exc)
            uni["industry"] = ""
        if "industry" not in uni.columns:
            uni["industry"] = ""
        uni["industry"] = uni["industry"].fillna("")

        # 上市天数：用快照/清单里的上市日期算，缺失则留空（不因此排除）
        uni["list_date"] = pd.to_datetime(uni.get("list_date"), errors="coerce")
        uni["listed_days"] = (self.as_of - uni["list_date"]).dt.days

        # 是否 ST：靠名称判断（沪市清单里没有 ST 标记字段）
        uni["is_st"] = uni["name"].astype(str).str.contains("ST|退", na=False)
        self._universe = uni
        LOG.info("候选池底表：%d 只（含行业归属 %d 只）", len(uni), int((uni["industry"] != "").sum()))
        return uni

    def prefilter(self, uni: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
        """按 [screen] 配置做硬性排除，返回 (通过, 被剔除并附原因)。"""
        uni = self.load_universe() if uni is None else uni
        scfg = self.cfg.screen
        df = uni.copy()
        reasons: list[str] = [""] * len(df)

        def _drop(mask: pd.Series, label: str) -> None:
            m = mask.fillna(False).to_numpy()
            for i in np.flatnonzero(m):
                reasons[i] = f"{reasons[i]};{label}" if reasons[i] else label

        if bool(scfg.get("exclude_st", True)):
            _drop(df["is_st"], "ST/退市风险")
        min_days = int(scfg.get("min_listed_days", 365))
        if min_days > 0:
            _drop(df["listed_days"].notna() & (df["listed_days"] < min_days), f"上市不足{min_days}天")
        min_amt = float(scfg.get("min_avg_amount_wan", 0) or 0)
        if min_amt > 0 and "amount" in df.columns:
            # 快照的 amount 单位是元，配置用万元
            _drop(pd.to_numeric(df["amount"], errors="coerce") < min_amt * 1e4, f"成交额<{min_amt:g}万")
        if bool(scfg.get("exclude_negative_pe", True)):
            pe = pd.to_numeric(df["pe_ttm"], errors="coerce")
            _drop(pe.notna() & (pe <= 0), "PE为负(亏损)")
        _drop(pd.to_numeric(df["price"], errors="coerce").isna(), "无价格")

        df["剔除原因"] = reasons
        kept = df[df["剔除原因"] == ""].copy()
        rejected = df[df["剔除原因"] != ""].copy()
        LOG.info("粗筛：通过 %d 只，剔除 %d 只", len(kept), len(rejected))
        return kept, rejected

    def _select_candidates(self, kept: pd.DataFrame, limit: int | None = None) -> pd.DataFrame:
        """按流通市值降序取前 N 只做精算（精算每只 4 次请求，必须限量）。"""
        cap_col = "float_mktcap_yi" if "float_mktcap_yi" in kept.columns else None
        if cap_col:
            out = kept.sort_values(cap_col, ascending=False, na_position="last")
        else:
            out = kept
        n = int(limit or self.cfg.screen.get("max_candidates", 600) or 600)
        out = out.head(n)
        LOG.info("精算候选：%d 只（按流通市值取前 %d）", len(out), n)
        return out

    # ------------------------------------------------------------------ 单只精算
    def _fetch_one(self, code: str) -> dict[str, Any]:
        """抓一只股票的四份明细数据。任何一份失败都不影响其它。"""
        code = plain_code(code)
        ttl = self.ttls["history"]
        out: dict[str, Any] = {"code": code}
        getters: dict[str, Callable[[], Any]] = {
            "pe_hist": lambda: fetch_valuation_history(code, self.cache, "市盈率(TTM)", "近五年", ttl_days=ttl),
            "pb_hist": lambda: fetch_valuation_history(code, self.cache, "市净率", "近五年", ttl_days=ttl),
            "div": lambda: fetch_dividend_detail(code, self.cache, self.proxy, ttl_days=self.ttls["fund"]),
            "fin": lambda: fetch_financial_indicators(code, self.cache, start_year="2016",
                                                      ttl_days=self.ttls["fund"]),
        }
        # 回测需要历史价格（算股息率与区间收益），实时筛选不需要，故按需抓取
        if getattr(self, "_need_price", False):
            start = str(self.cfg.general.get("start_date", "2011-01-01"))
            adjust = str(self.cfg.general.get("adjust", "qfq"))
            getters["price_hist"] = lambda: fetch_price_history(
                code, self.cache, start=start, adjust=adjust, ttl_days=self.ttls["quote"]
            )
        for key, fn in getters.items():
            try:
                out[key] = fn()
            except Exception as exc:
                LOG.debug("%s %s 抓取失败：%s", code, key, exc)
                out[key] = None
        return out

    def score_one(
        self,
        meta: pd.Series | dict,
        raw: dict[str, Any],
        as_of: pd.Timestamp | str | None = None,
        point_in_time: bool = False,
    ) -> StockScore:
        """把一只股票的明细数据打分。

        参数
        ----
        as_of : 打分时点。回测里传入历史季度末，只使用该时点及之前的数据。
        point_in_time : 为 True 时**不使用全市场/同行业截面分位**——
            截面池是「今天的」快照，拿它给历史打分等于偷看未来。
            此时估值模块只依赖个股自身的历史分位。
        """
        cfg = self.cfg
        as_of_ts = self.as_of if as_of is None else pd.Timestamp(as_of)
        d = dict(meta)
        code = plain_code(str(d.get("code", raw.get("code", ""))))
        name = str(d.get("name") or "")
        industry = str(d.get("industry") or "")
        price = pd.to_numeric(d.get("price"), errors="coerce")
        pe = pd.to_numeric(d.get("pe_ttm"), errors="coerce")
        pb = pd.to_numeric(d.get("pb"), errors="coerce")

        flags: dict[str, Any] = {}

        # ---------------------------------------------------------- 历史分位
        # 注意：只取 as_of 及之前的历史（_clip_history），并用序列自身的最后一个
        # 值作为「当前值」——这样分位是自洽的，不依赖另一路数据源的快照。
        pe_hist = _clip_history(raw.get("pe_hist"), as_of_ts)
        pb_hist = _clip_history(raw.get("pb_hist"), as_of_ts)
        pe_pctl_ts = pb_pctl_ts = float("nan")
        if pe_hist is not None and len(pe_hist) > 20:
            pe_pctl_ts = percentile_of(float(pe_hist.dropna().iloc[-1]), pe_hist)
        if pb_hist is not None and len(pb_hist) > 20:
            pb_pctl_ts = percentile_of(float(pb_hist.dropna().iloc[-1]), pb_hist)

        # point-in-time 时，PE/PB 也要取 as_of 当天的值，而不是今天快照的值
        # （快照会在格雷厄姆数 PE×PB≤22.5 这类绝对水平判断里泄漏未来信息）
        if point_in_time:
            pe = _last_value(pe_hist, fallback=pe)
            pb = _last_value(pb_hist, fallback=pb)

        # 实时模式：用「实时快照的 PE/PB」在自身历史池里的分位，替代历史末值分位。
        # 这样盘中估值一变，分位和得分立刻跟着变，而不是等百度次日更新历史序列。
        if self.live and not point_in_time:
            if pe_hist is not None and len(pe_hist.dropna()) > 20 and np.isfinite(pe) and pe > 0:
                pe_pctl_ts = percentile_of(float(pe), pe_hist)
            if pb_hist is not None and len(pb_hist.dropna()) > 20 and np.isfinite(pb) and pb > 0:
                pb_pctl_ts = percentile_of(float(pb), pb_hist)

        # ---------------------------------------------------------- 股息
        # point-in-time 时用 as_of 当天的收盘价，否则用快照的最新价
        if point_in_time:
            price = float("nan")  # 绝不退回快照价：那是今天的价格，等于偷看未来
            ph = _clip_history(raw.get("price_hist"), as_of_ts)
            if ph is not None and len(ph) and "close" in getattr(ph, "columns", []):
                s_close = pd.to_numeric(ph["close"], errors="coerce").dropna()
                if len(s_close):
                    price = float(s_close.iloc[-1])
        div = _clip_history(raw.get("div"), as_of_ts)
        div_row = self._div_summary_map().get(code) if getattr(self, "_div_summary", None) is not None else None
        din = build_dividend_input(code, name, div, float(price) if np.isfinite(price) else None,
                                   div_row, as_of=as_of_ts)
        dy = din.get("dividend_yield", float("nan"))

        # ---------------------------------------------------------- 质量
        fin = _clip_history(raw.get("fin"), as_of_ts)
        qsum = summarize_financials(fin, stability_years=int(cfg.quality.stability_years)) if fin is not None and len(fin) else {}
        if qsum:
            flags["质量报告期"] = f"{qsum.get('periods_annual', 0)} 个年报 / 共 {qsum.get('periods', 0)} 期"

        # ---------------------------------------------------------- 政策
        pin = {
            "code": code, "name": name, "industry": industry,
            "policy_table": self._industry_policy(), "events": self._policy_events(),
            "as_of": as_of_ts,
        }

        # ---------------------------------------------------- 四项分别打分
        # 截面分位只在「今天」这种非 point-in-time 场景下可用
        use_cs = not point_in_time
        parts: dict[str, RuleResult] = {}
        parts["valuation"] = score_valuation(
            ValuationInput(
                code=code, name=name, industry=industry,
                pe_ttm=float(pe) if np.isfinite(pe) else None,
                pb=float(pb) if np.isfinite(pb) else None,
                dividend_yield=float(dy) if np.isfinite(dy) else None,
                pe_percentile_ts=pe_pctl_ts, pb_percentile_ts=pb_pctl_ts,
                pe_percentile_cs=self._cs_percentile("pe", pe) if use_cs else float("nan"),
                pb_percentile_cs=self._cs_percentile("pb", pb) if use_cs else float("nan"),
                dy_percentile_cs=self._cs_percentile("dy", dy, higher_better=True) if use_cs else float("nan"),
                pe_percentile_industry=self._ind_percentile(industry, "pe", pe) if use_cs else float("nan"),
                pb_percentile_industry=self._ind_percentile(industry, "pb", pb) if use_cs else float("nan"),
            ),
            cfg,
        )
        parts["quality"] = score_quality(QualityInput(code=code, name=name, industry=industry, **qsum), cfg)
        parts["dividend"] = score_dividend({**din, "bond_yield": self._bond_yield()}, cfg)
        parts["policy"] = score_policy(pin, cfg)

        # ---------------------------------------------------- 加权合成
        raw_w = dict(cfg.stock_weights)
        acc = tot_w = 0.0
        used: dict[str, float] = {}
        missing: list[str] = []
        for key in STOCK_PARTS:
            res = parts[key]
            w = float(raw_w.get(key, 0.0))
            if res is None or not np.isfinite(res.score) or w <= 0:
                missing.append(key)
                continue
            acc += w * float(res.score)
            tot_w += w
            used[key] = w
        total = clamp(acc / tot_w) if tot_w > 0 else float("nan")

        flags["参与打分的项"] = list(used.keys())
        flags["缺失项"] = missing
        flags["权重"] = {k: round(v / tot_w, 4) for k, v in used.items()} if tot_w else {}

        return StockScore(
            code=code, name=name, industry=industry,
            price=float(price) if np.isfinite(price) else float("nan"),
            pe_ttm=float(pe) if np.isfinite(pe) else float("nan"),
            pb=float(pb) if np.isfinite(pb) else float("nan"),
            dividend_yield=float(dy) if np.isfinite(dy) else float("nan"),
            float_mktcap_yi=pd.to_numeric(d.get("float_mktcap_yi"), errors="coerce"),
            parts=parts, total=total, grade=_grade(total), data_flags=flags,
        )

    # ------------------------------------------------------------ 懒加载的辅助表
    def _industry_policy(self) -> pd.DataFrame:
        if self._policy_table is None:
            self._policy_table = load_industry_policy(self.cfg)
        return self._policy_table

    def _policy_events(self) -> pd.DataFrame:
        if self._events is None:
            self._events = load_policy_events(self.cfg)
        return self._events

    def _div_summary_map(self) -> dict[str, dict[str, Any]]:
        if getattr(self, "_div_summary", None) is None:
            try:
                s = fetch_dividend_summary(self.cache, self.proxy, ttl_days=self.ttls["fund"])
                self._div_summary = {plain_code(r["code"]): r for _, r in s.iterrows()}
            except Exception as exc:
                LOG.warning("全市场分红概览抓取失败：%s", exc)
                self._div_summary = {}
        return self._div_summary

    def _bond_yield(self) -> float:
        if getattr(self, "_bond", "missing") == "missing":
            try:
                from .data.macro_cn import fetch_cn_bond_10y
                s = fetch_cn_bond_10y(self.cache, self.proxy, ttl_days=self.ttls["history"])
                self._bond = float(s.dropna().iloc[-1]) if len(s.dropna()) else float("nan")
            except Exception:
                self._bond = float("nan")
        return self._bond

    def _cs_percentile(self, field: str, value: Any, higher_better: bool = False) -> float:
        """全市场截面分位。"""
        uni = self.load_universe()
        col = {"pe": "pe_ttm", "pb": "pb", "dy": None}[field]
        if col is None:
            return float("nan")
        try:
            v = float(value)
        except (TypeError, ValueError):
            return float("nan")
        if not np.isfinite(v):
            return float("nan")
        pool = pd.to_numeric(uni[col], errors="coerce")
        p = percentile_of(v, pool)
        if higher_better and np.isfinite(p):
            # 股息率越高越好：把「值越低分位越低」翻成「值越高分位越高」
            p = 1.0 - p
        return p

    def _ind_percentile(self, industry: str, field: str, value: Any) -> float:
        """同行业截面分位（行业归属缺失时返回 NaN，由个股历史分位兜底）。"""
        if not industry:
            return float("nan")
        uni = self.load_universe()
        if "industry" not in uni.columns:
            return float("nan")
        sub = uni[uni["industry"] == industry]
        if len(sub) < 5:
            return float("nan")
        try:
            v = float(value)
        except (TypeError, ValueError):
            return float("nan")
        if not np.isfinite(v):
            return float("nan")
        col = {"pe": "pe_ttm", "pb": "pb"}[field]
        return percentile_of(v, pd.to_numeric(sub[col], errors="coerce"))

    # ------------------------------------------------------------------ 主流程
    def run(
        self,
        limit: int | None = None,
        codes: list[str] | None = None,
        workers: int = 6,
    ) -> pd.DataFrame:
        """跑完整筛选，返回按总分降序的结果表。"""
        if codes:
            uni = self.load_universe()
            cand = uni[uni["code"].astype(str).isin({plain_code(c) for c in codes})].copy()
        else:
            kept, _ = self.prefilter()
            cand = self._select_candidates(kept, limit=limit)

        metas = {str(r["code"]): r for _, r in cand.iterrows()}
        LOG.info("开始抓取 %d 只股票的明细（并发 %d）…", len(metas), workers)
        raws = parallel_map(self._fetch_one, list(metas.keys()), workers=workers, label="明细抓取")

        results: list[StockScore] = []
        for code, meta in metas.items():
            raw = raws.get(code) or {"code": code}
            try:
                results.append(self.score_one(meta, raw))
            except Exception as exc:
                LOG.warning("%s 打分失败：%s", code, exc)

        df = pd.DataFrame([r.as_dict() for r in results])
        if len(df):
            df = df.sort_values("总分", ascending=False, na_position="last").reset_index(drop=True)
            df.insert(0, "排名", range(1, len(df) + 1))
        self.results = results
        return df
