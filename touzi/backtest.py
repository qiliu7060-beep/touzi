"""回测：验证用户提出的那些宏观说法在历史上到底成不成立，以及个股打分能不能赚钱。

## 为什么分成两个回测

用户给的规则是**两层**的，必须分开验证，混在一起会得出错误结论：

* **回测A —— 宏观规则验证（择时层）**：把「PMI 大幅超过 50 / CPI 涨得快 /
  PPI 下跌到一半 / PPI 到底部」这些说法，逐条拿来和沪深300 的**前瞻 3/6/12 个月
  收益**对照。目的是回答一句话：*这些说法在历史上是否真的成立？*
  这一层不涉及选股，所以不受幸存者偏差影响，结论最硬。
* **回测B —— 个股组合（选股层）**：按打分排序取前 N 只，季度调仓，对比沪深300。
  目的是回答：*这套打分能不能选出跑赢基准的股票？*

## 回测A 的做法：直接复用生产逻辑

不另写一套「历史状态判断」，而是**对每个历史月末把序列切一刀，
丢进生产用的 ``score_pmi`` / ``score_cpi`` / ``score_ppi`` / ``score_gdp``**。
所以回测验证的就是线上跑的同一套规则，不存在「回测和实盘两套代码」的问题。
再按状态分组，统计前瞻收益的均值与胜率。

## 回测B 的三个必须写在报告里的偏差

1. **幸存者偏差**：候选池是**今天**还在交易的股票。已经退市的公司不在里面，
   所以回测结果天然偏乐观。这是免费数据源下无法根治的（要么买退市股历史库，
   要么付费用 Wind/聚源），只能明说。
2. **估值历史只有 5 年**：个股 PE/PB 序列来自百度股市通「近五年」，
   所以组合回测最早只能回到约 5 年前，样本量有限。
3. **截面分位被禁用**：全市场/同行业截面分位依赖「今天」的快照，
   拿它给历史打分等于偷看未来。所以 ``point_in_time=True`` 时估值模块
   **只用个股自身的历史分位**，只有个股历史不足时才退化。
   代价是历史打分和今天的打分口径不完全一致。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import numpy as np
import pandas as pd

from .data.macro_cn import build_cn_macro_panel, fetch_cn_bond_10y
from .data.macro_us import fetch_fed_panel
from .data.stock import fetch_index_daily, fetch_price_history, parallel_map, plain_code
from .signals.macro import score_cpi, score_gdp, score_pmi, score_ppi
from .signals.fed import score_fed
from .util import Cache, apply_proxy_env, resolve_ttls

__all__ = ["validate_macro_rules", "backtest_portfolio", "backtest_stocks", "backtest_all"]

LOG = logging.getLogger(__name__)


# =============================================================== 工具
def _monthly_last(s: pd.Series) -> pd.Series:
    s = pd.Series(s).dropna()
    if s.empty:
        return s
    if not isinstance(s.index, pd.DatetimeIndex):
        return s
    return s.resample("ME").last().dropna()


def _clip(obj, as_of: pd.Timestamp):
    if obj is None:
        return None
    if isinstance(obj, pd.DataFrame) and len(obj) == 0:
        return obj
    try:
        return obj[obj.index <= as_of]
    except Exception:
        return obj


def _fwd_returns(series: pd.Series, horizons: list[int]) -> pd.DataFrame:
    """月末值序列 -> 前瞻 k 个月收益（小数）。``shift(-k)`` 不构成未来函数：
    只有站在 t 时刻「事后统计」时才用到，打分本身从不读它。"""
    out = pd.DataFrame(index=series.index)
    for k in horizons:
        out[f"fwd{k}"] = series.shift(-k) / series - 1.0
    return out


def _bucket_table(df: pd.DataFrame, rule: str, bucket_col: str, horizons: list[int]) -> list[dict[str, Any]]:
    """按状态分组统计前瞻收益。"""
    rows: list[dict[str, Any]] = []
    if bucket_col not in df.columns:
        return rows
    for bucket, sub in df.groupby(bucket_col, dropna=True):
        rec: dict[str, Any] = {"规则": rule, "状态": str(bucket), "样本数": int(len(sub))}
        for k in horizons:
            col = f"fwd{k}"
            vals = sub[col].dropna()
            rec[f"{k}月均值%"] = None if not len(vals) else round(float(vals.mean()) * 100, 2)
            rec[f"{k}月胜率%"] = None if not len(vals) else round(float((vals > 0).mean()) * 100, 1)
        rows.append(rec)
    # 无条件基准
    base: dict[str, Any] = {"规则": rule, "状态": "【全样本基准】", "样本数": int(len(df))}
    for k in horizons:
        vals = df[f"fwd{k}"].dropna()
        base[f"{k}月均值%"] = None if not len(vals) else round(float(vals.mean()) * 100, 2)
        base[f"{k}月胜率%"] = None if not len(vals) else round(float((vals > 0).mean()) * 100, 1)
    rows.append(base)
    return rows


# =============================================================== 回测A
def validate_macro_rules(
    cfg,
    cache: Cache | None = None,
    proxy: str | None = None,
    start: str | None = None,
    end: str | None = None,
    horizons: list[int] | None = None,
) -> dict[str, Any]:
    """验证用户的宏观说法：逐月判定状态，统计沪深300 前瞻收益。"""
    proxy = proxy if proxy is not None else (cfg.general.get("proxy") or None)
    apply_proxy_env(proxy)
    if cache is None:
        cache = Cache(cfg.resolve(cfg.general.get("cache_dir", "data_cache")))
    horizons = list(horizons or cfg.backtest.get("forward_months", [3, 6, 12]))
    start = pd.Timestamp(start or cfg.general.get("start_date", "2011-01-01"))
    end = pd.Timestamp(end) if end else pd.Timestamp.today()

    LOG.info("回测A：装载宏观与指数数据…")
    ttls = resolve_ttls(cfg)
    panel = build_cn_macro_panel(cache, proxy, ttl_days=ttls["macro"], bond_ttl_days=ttls["history"])
    aligned = panel["aligned"]
    pmi_df, cpi_s, ppi_s, gdp_s = aligned["pmi"], aligned["cpi"], aligned["ppi"], aligned["gdp"]

    idx = fetch_index_daily(cfg.backtest.get("benchmark", "sh000300"), cache,
                            ttl_days=ttls["quote"])
    idx_m = _monthly_last(idx["close"])
    fwd = _fwd_returns(idx_m, horizons)

    fed_panel = None
    try:
        fed_panel = fetch_fed_panel(cache, proxy, ttl_days=ttls["us"])
    except Exception as exc:
        LOG.warning("回测A：美联储面板抓取失败，fed 规则将跳过：%s", exc)
    cn_bond = None
    try:
        cn_bond = fetch_cn_bond_10y(cache, proxy)
    except Exception as exc:
        LOG.warning("回测A：中国10年期国债抓取失败：%s", exc)

    months = [d for d in idx_m.index if start <= d <= end]
    # 前瞻收益需要未来数据，最后 horizon 个月自动落空，属正常
    LOG.info("回测A：逐月判定 %d 个月末状态…", len(months))

    recs: list[dict[str, Any]] = []
    for t in months:
        rec: dict[str, Any] = {"date": t}
        try:
            p = _clip(pmi_df, t)
            if p is not None and len(p):
                r = score_pmi(p["manufacturing"], cfg,
                              p["non_manufacturing"] if "non_manufacturing" in p.columns else None)
                d = r.detail
                rec["PMI水平"] = ("≥51.5 强扩张" if d.get("pmi", 0) >= cfg.pmi.strong_level
                                else "50~51.5 温和扩张" if d.get("pmi", 0) >= cfg.pmi.expansion_level
                                else "<50 收缩")
                rec["PMI动量"] = ("上升阶段" if d.get("momentum_score", 50) >= 55
                               else "走平" if d.get("momentum_score", 50) >= 45 else "下行阶段")
                rec["_pmi"] = d.get("pmi")
        except Exception as exc:
            LOG.debug("%s PMI 失败：%s", t, exc)
        try:
            c = _clip(cpi_s, t)
            if c is not None and len(c):
                d = score_cpi(c, cfg).detail
                d3 = d.get("delta_3m")
                rec["CPI速度"] = ("涨得快" if d3 is not None and d3 > cfg.cpi.fast_rise_3m
                               else "涨得慢" if d3 is not None and d3 < -cfg.cpi.fast_rise_3m else "基本走平")
                rec["_cpi"] = d.get("cpi_yoy")
        except Exception as exc:
            LOG.debug("%s CPI 失败：%s", t, exc)
        try:
            p2 = _clip(ppi_s, t)
            if p2 is not None and len(p2):
                d = score_ppi(p2, cfg).detail
                r_ = d.get("retracement")
                rec["PPI回撤"] = ("底部区(≥85%)" if r_ is not None and r_ >= 0.85
                               else "深跌区(65%~85%)" if r_ is not None and r_ >= 0.65
                               else "半山腰(35%~65%)" if r_ is not None and r_ >= 0.35
                               else "高位区(<35%)" if r_ is not None else "无周期")
                rec["_ppi_retrace"] = r_
                rec["_ppi"] = d.get("ppi_yoy")
        except Exception as exc:
            LOG.debug("%s PPI 失败：%s", t, exc)
        try:
            g = _clip(gdp_s, t)
            if g is not None and len(g):
                d = score_gdp(g, cfg).detail
                dv = d.get("delta_vs_mean")
                rec["GDP动能"] = ("高于近年均值" if dv is not None and dv > 0
                               else "低于近年均值" if dv is not None else "无数据")
                rec["_gdp"] = d.get("gdp_yoy")
        except Exception as exc:
            LOG.debug("%s GDP 失败：%s", t, exc)
        if fed_panel is not None:
            try:
                fp = _clip(fed_panel, t)
                cb = _clip(cn_bond, t) if cn_bond is not None else None
                if fp is not None and len(fp) > 300:
                    r = score_fed(fp, cfg, china_10y=cb)
                    rec["美联储潮汐"] = ("涨潮(≥60)" if r.score >= 60
                                     else "中性(45~60)" if r.score >= 45 else "退潮(<45)")
                    rec["_fed_score"] = round(float(r.score), 1)
            except Exception as exc:
                LOG.debug("%s Fed 失败：%s", t, exc)
        recs.append(rec)

    df = pd.DataFrame(recs).set_index("date").join(fwd, how="left")

    horizons = [h for h in horizons if f"fwd{h}" in df.columns]
    rules = {
        "PMI": "PMI 大幅超过 50 或处于上升阶段，股市大概率上涨",
        "CPI": "CPI 涨得快利好股市和债市，涨得慢不利于股市和债市",
        "PPI": "PPI 下跌到一半股市领先上涨；PPI 到底部时股市已到顶峰",
        "GDP": "GDP 与股市正相关",
        "FED": "美联储潮汐（降息/扩表/美元走弱）利好 A 股",
    }
    bucket_cols = {"PMI水平": "PMI", "PMI动量": "PMI", "CPI速度": "CPI",
                   "PPI回撤": "PPI", "GDP动能": "GDP", "美联储潮汐": "FED"}

    tables: dict[str, list[dict[str, Any]]] = {}
    all_rows: list[dict[str, Any]] = []
    for col, rule in bucket_cols.items():
        if col in df.columns and df[col].notna().any():
            rows = _bucket_table(df, rule, col, horizons)
            tables[col] = rows
            all_rows.extend(rows)

    table = pd.DataFrame(all_rows)
    summary = {
        "行数": len(df),
        "区间": f"{df.index.min():%Y-%m} ~ {df.index.max():%Y-%m}" if len(df) else "-",
        "基准(沪深300)": {
            f"{h}月均值%": None if df[f"fwd{h}"].dropna().empty else round(float(df[f"fwd{h}"].mean()) * 100, 2)
            for h in horizons
        },
    }
    LOG.info("回测A完成：%d 个月，%d 条对照", len(df), len(table))
    return {"detail": df, "table": table, "tables": tables,
            "rules": rules, "summary": summary, "horizons": horizons}


# =============================================================== 回测B
def _portfolio_metrics(returns: pd.Series, freq_per_year: float, rf: float = 0.0) -> dict[str, float]:
    r = returns.dropna()
    if r.empty:
        return {}
    n = len(r)
    cum = float((1.0 + r).prod() - 1.0)
    years = n / freq_per_year
    ann = float((1.0 + cum) ** (1.0 / years) - 1.0) if years > 0 and cum > -1 else float("nan")
    vol = float(r.std(ddof=1) * np.sqrt(freq_per_year)) if n > 1 else float("nan")
    sharpe = float((ann - rf) / vol) if vol and np.isfinite(vol) and vol > 0 else float("nan")
    curve = (1.0 + r).cumprod()
    dd = float((curve / curve.cummax() - 1.0).min())
    return {
        "期数": n,
        "累计收益%": round(cum * 100, 2),
        "年化收益%": None if not np.isfinite(ann) else round(ann * 100, 2),
        "年化波动%": None if not np.isfinite(vol) else round(vol * 100, 2),
        "夏普": None if not np.isfinite(sharpe) else round(sharpe, 2),
        "最大回撤%": round(dd * 100, 2),
        "胜率%": round(float((r > 0).mean()) * 100, 1),
    }


def backtest_portfolio(
    cfg,
    cache: Cache | None = None,
    proxy: str | None = None,
    limit: int | None = None,
    top_n: int | None = None,
    quarters: int | None = None,
    workers: int = 6,
    screener=None,
) -> dict[str, Any]:
    """个股组合回测：季度调仓，取总分前 N 只等权持有，对比沪深300。"""
    from .screener import Screener

    proxy = proxy if proxy is not None else (cfg.general.get("proxy") or None)
    apply_proxy_env(proxy)
    if cache is None:
        cache = Cache(cfg.resolve(cfg.general.get("cache_dir", "data_cache")))
    top_n = int(top_n or cfg.screen.get("top_n", 30))
    cost = float(cfg.backtest.get("cost_rate", 0.0015))
    bcfg = cfg.backtest

    scr = screener or Screener(cfg, cache=cache, proxy=proxy)
    scr._need_price = True  # 触发历史价格抓取

    kept, _ = scr.prefilter()
    cand = scr._select_candidates(kept, limit=limit)
    codes = [str(c) for c in cand["code"]]
    metas = {str(r["code"]): r for _, r in cand.iterrows()}

    LOG.info("回测B：抓取 %d 只股票的历史数据（首次较慢，之后走缓存）…", len(codes))
    raws = parallel_map(scr._fetch_one, codes, workers=workers, label="回测数据抓取")

    # ---- 调仓日：沪深300 的月末，且个股估值历史覆盖得到 ----
    idx = fetch_index_daily(bcfg.get("benchmark", "sh000300"), cache,
                            ttl_days=resolve_ttls(cfg)["history"])
    idx_m = _monthly_last(idx["close"])
    q_ends = idx_m.index[idx_m.index.is_quarter_end]

    # 估值历史（百度）只有 5 年，回测起点不能早于它。
    # 但**绝不能取 max()**：只要有一只次新股（或百度无历史的老股）起始日很晚，
    # 整个回测窗口就被它拖到最近，导致「可用调仓日不足」。改用分位数：
    # 取 60% 分位 = 60% 候选股都已经同时有价格与估值数据的那个月末。
    starts: list[pd.Timestamp] = []
    for raw in raws.values():
        s = None
        ph = raw.get("price_hist")
        if ph is not None and len(ph):
            s = ph.index.min()
        pe = raw.get("pe_hist")
        if pe is not None and len(pe):
            s = pe.index.min() if s is None else max(s, pe.index.min())
        if s is not None:
            starts.append(pd.Timestamp(s))
    if not starts:
        raise RuntimeError("没有拿到任何个股历史数据，无法回测")

    starts_sr = pd.Series(starts)
    cover_start = starts_sr.quantile(0.60)
    covered = int((starts_sr <= cover_start).sum())
    LOG.info(
        "回测B：起始日 %s（%d/%d 只覆盖；最早 %s，最晚 %s）",
        cover_start.date(), covered, len(starts),
        starts_sr.min().date(), starts_sr.max().date(),
    )

    q_ends = [d for d in q_ends if d >= cover_start]
    if quarters:
        q_ends = q_ends[-int(quarters):]
    if len(q_ends) < 3:
        raise RuntimeError(f"可用调仓日不足（{len(q_ends)} 个），无法回测")
    LOG.info("回测B：调仓日 %d 个，%s ~ %s", len(q_ends), q_ends[0].date(), q_ends[-1].date())

    freq = 4.0  # 季度
    periods: list[dict[str, Any]] = []
    prev_hold: set[str] = set()
    equity = 1.0
    bench_equity = 1.0

    for i, t in enumerate(q_ends[:-1]):
        t_next = q_ends[i + 1]
        scored: list[tuple[float, str]] = []
        for code, meta in metas.items():
            raw = raws.get(code) or {}
            try:
                ss = scr.score_one(meta, raw, as_of=t, point_in_time=True)
            except Exception:
                continue
            if np.isfinite(ss.total):
                scored.append((float(ss.total), code))
        scored.sort(reverse=True)
        picked = [c for _, c in scored[:top_n]]
        if len(picked) < int(bcfg.get("min_stocks_per_period", 10)):
            LOG.warning("%s 入选股票不足（%d），跳过该期", t.date(), len(picked))
            continue

        rets: list[float] = []
        for code in picked:
            ph = raws.get(code, {}).get("price_hist")
            if ph is None or len(ph) == 0:
                continue
            px = pd.to_numeric(ph["close"], errors="coerce").dropna()
            a = px[px.index <= t]
            b = px[px.index <= t_next]
            if len(a) and len(b) and float(a.iloc[-1]) > 0:
                rets.append(float(b.iloc[-1]) / float(a.iloc[-1]) - 1.0)
        if not rets:
            continue
        gross = float(np.mean(rets))

        hold = set(picked)
        turnover = len(hold - prev_hold) / max(len(hold), 1)
        net = gross - 2.0 * cost * turnover
        prev_hold = hold

        bi_a = idx_m[idx_m.index <= t]
        bi_b = idx_m[idx_m.index <= t_next]
        bench = float(bi_b.iloc[-1]) / float(bi_a.iloc[-1]) - 1.0 if len(bi_a) and len(bi_b) else float("nan")

        equity *= (1.0 + net)
        bench_equity *= (1.0 + bench) if np.isfinite(bench) else 1.0
        periods.append({
            "调仓日": f"{t:%Y-%m-%d}", "下期": f"{t_next:%Y-%m-%d}",
            "持仓数": len(rets), "换手率%": round(turnover * 100, 1),
            "组合收益%": round(net * 100, 2), "沪深300%": None if not np.isfinite(bench) else round(bench * 100, 2),
            "超额%": None if not np.isfinite(bench) else round((net - bench) * 100, 2),
            "组合净值": round(equity, 4), "基准净值": round(bench_equity, 4),
            "持仓": ",".join(picked),
        })

    if not periods:
        raise RuntimeError("回测没有产生任何有效调仓期")

    pdf = pd.DataFrame(periods)
    port_r = pdf["组合收益%"] / 100.0
    bench_r = pdf["沪深300%"] / 100.0
    rf = float(bcfg.get("risk_free_rate", 0.025))

    metrics = {
        "组合": _portfolio_metrics(port_r, freq, rf),
        "沪深300": _portfolio_metrics(bench_r, freq, rf),
    }
    excess = port_r - bench_r.fillna(0.0)
    metrics["超额"] = {
        "区间": f"{q_ends[0]:%Y-%m-%d} ~ {q_ends[len(pdf)]:%Y-%m-%d}",
        "累计超额%": round(float((1 + port_r).prod() - (1 + bench_r.fillna(0)).prod()) * 100, 2),
        "平均每期超额%": round(float(excess.mean()) * 100, 2),
        "跑赢期数占比%": round(float((excess > 0).mean()) * 100, 1),
        "期末组合净值": round(float((1 + port_r).prod()), 4),
        "期末基准净值": round(float((1 + bench_r.fillna(0)).prod()), 4),
        "单边成本": cost,
        "调仓频率": "季度",
    }
    metrics["_disclaimer"] = [
        "候选池是今天仍在交易的股票，存在幸存者偏差，结果偏乐观。",
        "估值历史来自百度股市通『近五年』，回测区间受此限制。",
        "回测打分禁用了全市场/同行业截面分位（那是今天的快照），只保留个股历史分位。",
    ]

    LOG.info("回测B完成：%d 期，组合累计 %s%%，基准累计 %s%%",
             len(pdf), metrics["组合"]["累计收益%"], metrics["沪深300"]["累计收益%"])
    return {"periods": pdf, "metrics": metrics, "top_n": top_n,
            "cost_rate": cost, "start": str(q_ends[0].date())}


# =============================================================== 汇总
def backtest_all(cfg, cache: Cache | None = None, proxy: str | None = None,
                 limit: int | None = None, quarters: int | None = None,
                 workers: int = 6, skip_macro: bool = False,
                 skip_portfolio: bool = False) -> dict[str, Any]:
    """跑完整的两个回测。"""
    proxy = proxy if proxy is not None else (cfg.general.get("proxy") or None)
    if cache is None:
        cache = Cache(cfg.resolve(cfg.general.get("cache_dir", "data_cache")))
    out: dict[str, Any] = {}
    if not skip_macro:
        out["macro"] = validate_macro_rules(cfg, cache=cache, proxy=proxy)
    if not skip_portfolio:
        out["portfolio"] = backtest_portfolio(cfg, cache=cache, proxy=proxy,
                                              limit=limit, quarters=quarters, workers=workers)
    return out


# ======================================================= 回测C：个股级
def _next_close(ph: pd.DataFrame | None, t: pd.Timestamp, max_wait_days: int = 15):
    """信号日 ``t`` 之后第一个交易日的收盘价（成交价），取不到返回 ``None``。

    用**次日**收盘价成交而不是信号日当天，是为了不偷看未来：买入信号是用
    ``t`` 日收盘价与当日估值算出来的，如果又按 ``t`` 日收盘价成交，就等于
    「先知道结果再下单」。停牌超过 ``max_wait_days`` 则视为无法成交。
    """
    if ph is None or len(ph) == 0:
        return None, None
    s = pd.to_numeric(ph.get("close"), errors="coerce").dropna()
    s = s[s.index > pd.Timestamp(t)]
    if not len(s):
        return None, None
    d = pd.Timestamp(s.index[0])
    if (d - pd.Timestamp(t)).days > max_wait_days:
        return None, None
    return float(s.iloc[0]), d


def _max_drawdown(nav: pd.Series) -> float:
    if nav is None or len(nav) < 2:
        return float("nan")
    peak = nav.cummax()
    return float((nav / peak - 1.0).min())


def backtest_stocks(
    cfg,
    cache: Cache | None = None,
    proxy: str | None = None,
    codes: list[str] | None = None,
    limit: int | None = None,
    months: int | None = None,
    workers: int = 6,
    screener=None,
    monthly_macro: bool = True,
) -> dict[str, Any]:
    """回测C：**逐只股票**按 ``[plan]`` 的买卖规则模拟，输出单笔交易明细。

    与回测B（组合）的区别——这也是用户明确要求的「不要用组合，直接用个股」：

    * 回测B 问的是「按打分排序等权持有前 30 只，组合赚不赚钱」；
    * 回测C 问的是「**具体这一只股票**，按买入门槛进、按卖出条件出，
      每一笔交易赚了多少、持有多久、是被哪一条规则卖掉的」。

    做法：

    1. 评估频率 = 基准指数（沪深300）的**月末**，取最近 ``months`` 个月；
    2. 每个评估日对每只股票调 ``Screener.score_one(point_in_time=True)``，
       只用该日及之前的数据（估值分位、股价、财务、政策事件全部截断）；
    3. 空仓时若 :func:`touzi.plans.decide` 给出「建仓」→ 次日收盘价买入；
       持仓时若给出「清仓」→ 次日收盘价全部卖出；「减仓」→ 卖出一半；
    4. 记录每笔交易的买入/卖出日期与价格、持有天数、收益率、**卖出原因**；
    5. 同时算同期「买入并持有」的收益，用来回答一个关键问题：
       *卖出规则到底有没有用*——如果择时卖出跑不过一直拿着，那它就没价值。

    交易成本按 ``[backtest] cost_rate`` 单边扣除。
    """
    from .plans import decide
    from .regime import build_macro_context
    from .screener import Screener

    proxy = proxy if proxy is not None else (cfg.general.get("proxy") or None)
    apply_proxy_env(proxy)
    if cache is None:
        cache = Cache(cfg.resolve(cfg.general.get("cache_dir", "data_cache")))
    cost = float(cfg.backtest.get("cost_rate", 0.0015))
    bcfg = cfg.backtest

    scr = screener or Screener(cfg, cache=cache, proxy=proxy)
    scr._need_price = True

    if codes:
        plain = [plain_code(str(c)) for c in codes]
        kept, _ = scr.prefilter()
        universe = kept[kept["code"].astype(str).isin(plain)]
        # 指定了代码但预筛把它们剔掉了（例如 ST）——如实说明，不静默返回空
        dropped = sorted(set(plain) - set(universe["code"].astype(str)))
        if dropped:
            LOG.warning("回测C：这些代码被预筛剔除，不会回测：%s", dropped)
    else:
        kept, _ = scr.prefilter()
        universe = scr._select_candidates(kept, limit=limit)

    if universe is None or len(universe) == 0:
        raise RuntimeError("回测C：没有可回测的股票（预筛把候选全部剔除了？）")

    univ_codes = [str(c) for c in universe["code"]]
    metas = {str(r["code"]): r for _, r in universe.iterrows()}

    LOG.info("回测C：抓取 %d 只股票的历史数据 …", len(univ_codes))
    raws = parallel_map(scr._fetch_one, univ_codes, workers=workers, label="回测C数据抓取")
    raws = {plain_code(str(k)): v for k, v in (raws or {}).items() if v}
    metas = {plain_code(str(k)): v for k, v in metas.items()}
    univ_codes = [c for c in univ_codes if c in raws]
    if not univ_codes:
        raise RuntimeError("回测C：没有一只股票抓到历史数据")

    # ---- 评估日：沪深300 的月末 ----
    idx = fetch_index_daily(bcfg.get("benchmark", "sh000300"), cache,
                            ttl_days=resolve_ttls(cfg)["history"])
    idx_m = _monthly_last(idx["close"])
    dates = list(idx_m.index)
    start_dt = pd.Timestamp(cfg.general.get("start_date") or "2011-01-01")
    dates = [d for d in dates if d >= start_dt]
    # 个股估值历史只有 5 年，太早的评估日没有分位可用
    earliest = min(
        (pd.Timestamp(v["pe_hist"].index.min()) for v in raws.values()
         if v.get("pe_hist") is not None and len(v["pe_hist"])),
        default=None,
    )
    if earliest is not None:
        dates = [d for d in dates if d >= earliest]
    if months:
        dates = dates[-int(months):]
    if len(dates) < 6:
        raise RuntimeError(f"回测C：可用评估日只有 {len(dates)} 个，太少（至少需要 6 个）")
    LOG.info("回测C：评估日 %d 个，%s ~ %s；股票 %d 只",
             len(dates), dates[0].date(), dates[-1].date(), len(univ_codes))

    # ---- 宏观背景分（按评估日算一次并记忆化；同一日期不重复计算）----
    macro_cache: dict[pd.Timestamp, float] = {}

    def macro_at(t: pd.Timestamp) -> float:
        if t in macro_cache:
            return macro_cache[t]
        val = float("nan")
        if monthly_macro:
            try:
                mc = build_macro_context(cfg, cache=cache, proxy=proxy, as_of=t)
                val = float(mc.macro_score)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("回测C：%s 的宏观背景分算不出来（%s），该日不启用宏观门槛", t.date(), exc)
        macro_cache[t] = val
        return val

    # ---- 逐只股票模拟 ----
    trades: list[dict[str, Any]] = []
    per_stock: list[dict[str, Any]] = []

    for n, code in enumerate(univ_codes, 1):
        raw = raws.get(code) or {}
        # 注意：meta 是 DataFrame 的一行（Series），不能用 `or {}` 兜底——
        # Series 的真值判断会抛 "The truth value of a Series is ambiguous"
        meta = metas.get(code)
        if meta is None:
            continue
        ph = raw.get("price_hist")
        if ph is None or len(ph) == 0:
            continue
        px_all = pd.to_numeric(ph.get("close"), errors="coerce").dropna()
        if len(px_all) < 40:
            continue
        name = str(meta.get("name") or code)

        units = 0.0          # 当前持有股数（按「买入金额/价格」折算，初始资金 1.0）
        cash = 1.0
        entry_cost = 0.0     # 每股成本
        entry_date: pd.Timestamp | None = None
        entry_units = 0.0
        reduced = False      # 本轮建仓是否已经减过仓（减仓只做一次，见下方注释）
        nav_series: list[tuple[pd.Timestamp, float]] = []
        stock_trades: list[dict[str, Any]] = []

        for t in dates:
            try:
                ss = scr.score_one(meta, raw, as_of=t, point_in_time=True)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("回测C：%s @ %s 打分失败：%s", code, t.date(), exc)
                continue

            px_now = float(ss.price) if np.isfinite(ss.price) else float("nan")
            holding = ({"买入价": entry_cost, "买入日期": str(entry_date.date())}
                       if units > 0 and entry_cost > 0 else None)
            d = decide(ss, cfg, macro_score=macro_at(t), holding=holding, price=px_now)

            # 记录净值（按当日收盘价估值）
            if np.isfinite(px_now):
                nav = cash + units * px_now
                nav_series.append((t, nav))

            if units <= 0:
                if d.action == "建仓":
                    fill, fdate = _next_close(ph, t)
                    if fill and fill > 0:
                        spend = cash
                        units = spend * (1.0 - cost) / fill
                        entry_cost = fill
                        entry_date = fdate
                        entry_units = units
                        cash = 0.0
            else:
                # 加仓：还有现金就补进去，并把「已减过仓」的标记复位
                if d.action == "加仓" and cash > 1e-9:
                    fill, fdate = _next_close(ph, t)
                    if fill and fill > 0:
                        add = cash * (1.0 - cost) / fill
                        units += add
                        entry_units += add
                        # 成本按加权平均更新
                        if units > 0:
                            entry_cost = ((entry_cost * (units - add)) + cash) / units
                        cash = 0.0
                        reduced = False
                    continue

                if d.action == "清仓":
                    sell_ratio = 1.0
                elif d.action == "减仓" and not reduced:
                    sell_ratio = 0.5
                else:
                    # 减仓只做一次：否则「总分≤67 / 宏观≤40」会月月触发，
                    # 每次卖掉剩余仓位的一半，仓位几何衰减，等于被反复割肉
                    continue
                fill, fdate = _next_close(ph, t)
                if not fill or fill <= 0:
                    continue
                sell_units = units * sell_ratio
                proceeds = sell_units * fill * (1.0 - cost)
                cash += proceeds
                units -= sell_units
                if sell_ratio < 1.0:
                    reduced = True
                reason = "；".join(f"[{c.level}] {c.label}" for c in d.triggered) or d.action
                hold_from = entry_date if entry_date is not None else t
                ret = fill / entry_cost - 1.0 if entry_cost > 0 else float("nan")
                full_exit = units <= 1e-9
                stock_trades.append({
                    "代码": code, "名称": name,
                    "买入日": f"{hold_from:%Y-%m-%d}" if hold_from is not None else "",
                    "买入价": round(entry_cost, 3),
                    "卖出日": f"{fdate:%Y-%m-%d}" if fdate is not None else "",
                    "卖出价": round(fill, 3),
                    "持有天数": int((fdate - hold_from).days) if (fdate is not None and hold_from is not None) else None,
                    "单笔收益%": None if not np.isfinite(ret) else round(ret * 100, 2),
                    "卖出比例": "全部" if full_exit else "一半",
                    "卖出原因": reason,
                    "卖出时总分": None if not np.isfinite(d.total) else round(d.total, 2),
                    "卖出时宏观分": None if not np.isfinite(d.macro_score) else round(d.macro_score, 2),
                })
                if full_exit:
                    units = 0.0
                    entry_cost = 0.0
                    entry_date = None
                    entry_units = 0.0
                    reduced = False
                else:
                    # 减仓后剩余仓位的成本不变
                    pass

        # 期末仍持仓 → 按最后一个评估日收盘价平掉，便于统计
        if units > 0 and np.isfinite(px_all.iloc[-1]):
            last_px = float(px_all.iloc[-1])
            last_d = pd.Timestamp(px_all.index[-1])
            ret = last_px / entry_cost - 1.0 if entry_cost > 0 else float("nan")
            stock_trades.append({
                "代码": code, "名称": name,
                "买入日": f"{entry_date:%Y-%m-%d}" if entry_date is not None else "",
                "买入价": round(entry_cost, 3),
                "卖出日": f"{last_d:%Y-%m-%d}", "卖出价": round(last_px, 3),
                "持有天数": int((last_d - entry_date).days) if entry_date is not None else None,
                "单笔收益%": None if not np.isfinite(ret) else round(ret * 100, 2),
                "卖出比例": "期末未卖（按最后收盘价估算）",
                "卖出原因": "回测期末仍持有",
                "卖出时总分": None, "卖出时宏观分": None,
            })
            cash += units * last_px * (1.0 - cost)
            units = 0.0

        final_nav = cash
        nav = pd.Series({d: v for d, v in nav_series}).sort_index() if nav_series else pd.Series(dtype=float)
        years = max(1e-9, (dates[-1] - dates[0]).days / 365.25)
        strategy_ret = final_nav - 1.0

        # 同期买入并持有：第一个评估日的收盘价 → 最后一个评估日的收盘价
        bh = float("nan")
        a = px_all[px_all.index >= dates[0]]
        b = px_all[px_all.index <= dates[-1]]
        if len(a) and len(b):
            bh = float(b.iloc[-1]) / float(a.iloc[0]) - 1.0

        wins = [x for x in stock_trades if x["单笔收益%"] is not None and x["单笔收益%"] > 0]
        per_stock.append({
            "代码": code, "名称": name,
            "交易次数": len(stock_trades),
            "盈利次数": len(wins),
            "胜率%": round(len(wins) / len(stock_trades) * 100, 1) if stock_trades else None,
            "平均持有天数": (round(float(np.mean([x["持有天数"] for x in stock_trades
                                                  if x["持有天数"] is not None])), 0)
                             if any(x["持有天数"] is not None for x in stock_trades) else None),
            "策略收益%": round(strategy_ret * 100, 2),
            "策略年化%": round(((1.0 + strategy_ret) ** (1.0 / years) - 1.0) * 100, 2),
            "买入持有%": None if not np.isfinite(bh) else round(bh * 100, 2),
            "超额%": None if not np.isfinite(bh) else round((strategy_ret - bh) * 100, 2),
            "最大回撤%": None if not len(nav) else round(_max_drawdown(nav) * 100, 2),
            "期末是否空仓": "是" if units <= 0 else "否",
        })
        trades.extend(stock_trades)
        if n % 20 == 0:
            LOG.info("回测C：已模拟 %d/%d 只", n, len(univ_codes))

    if not trades and not per_stock:
        raise RuntimeError("回测C：没有任何股票产生有效模拟结果")

    tdf = pd.DataFrame(trades)
    sdf = pd.DataFrame(per_stock).sort_values("策略收益%", ascending=False) if per_stock else pd.DataFrame()

    # ---- 汇总 ----
    overall: dict[str, Any] = {}
    if len(tdf):
        rets = pd.to_numeric(tdf["单笔收益%"], errors="coerce").dropna()
        overall = {
            "股票数": len(sdf),
            "总交易笔数": int(len(tdf)),
            "盈利笔数": int((rets > 0).sum()),
            "胜率%": round(float((rets > 0).mean()) * 100, 1),
            "平均单笔收益%": round(float(rets.mean()), 2) if len(rets) else None,
            "单笔收益中位数%": round(float(rets.median()), 2) if len(rets) else None,
            "最好一笔%": round(float(rets.max()), 2) if len(rets) else None,
            "最差一笔%": round(float(rets.min()), 2) if len(rets) else None,
            "平均持有天数": (round(float(pd.to_numeric(tdf["持有天数"], errors="coerce").dropna().mean()), 0)
                             if tdf["持有天数"].notna().any() else None),
            "平均策略收益%": round(float(sdf["策略收益%"].mean()), 2) if len(sdf) else None,
            "平均买入持有%": (round(float(pd.to_numeric(sdf["买入持有%"], errors="coerce").dropna().mean()), 2)
                              if len(sdf) and sdf["买入持有%"].notna().any() else None),
            "跑赢买入持有占比%": (round(float((pd.to_numeric(sdf["超额%"], errors="coerce") > 0).mean()) * 100, 1)
                                   if len(sdf) else None),
        }
        # 只有真正交易过的股票才谈得上「策略表现」；把两组口径分开列，
        # 否则「从未买入」（收益记 0）会与「买入持有」混在一起，读数失真。
        traded = sdf[pd.to_numeric(sdf["交易次数"], errors="coerce").fillna(0) > 0]
        bh_all = pd.to_numeric(sdf["买入持有%"], errors="coerce").dropna()
        overall["有交易的股票数"] = int(len(traded))
        overall["有交易的股票平均策略收益%"] = (
            round(float(traded["策略收益%"].mean()), 2) if len(traded) else None)
        if len(traded):
            bh_t = pd.to_numeric(traded["买入持有%"], errors="coerce").dropna()
            overall["有交易的股票平均买入持有%"] = round(float(bh_t.mean()), 2) if len(bh_t) else None
        else:
            overall["有交易的股票平均买入持有%"] = None
        if len(bh_all):
            overall["买入持有中位数%"] = round(float(bh_all.median()), 2)
            overall["买入持有25%分位%"] = round(float(bh_all.quantile(0.25)), 2)
            overall["买入持有75%分位%"] = round(float(bh_all.quantile(0.75)), 2)
        # 按卖出原因归类，看哪条规则最常触发、效果如何
        by_reason = (tdf.assign(_r=tdf["卖出原因"].astype(str).str.split("；").str[0])
                     .groupby("_r")
                     .agg(笔数=("单笔收益%", "size"),
                          平均收益=("单笔收益%", lambda s: round(float(pd.to_numeric(s, errors="coerce").mean()), 2)))
                     .reset_index().rename(columns={"_r": "卖出原因"}))
        overall["按卖出原因"] = by_reason.sort_values("笔数", ascending=False).to_dict("records")

    metrics = {
        "汇总": overall,
        "_disclaimer": [
            "候选池是今天仍在交易的股票，存在幸存者偏差，结果偏乐观。",
            "**候选池按「今天的」流通市值排序选取，这本身带后见之明**：今天的大市值里已经包含"
            "新易盛、寒武纪、中际旭创这类 2023–2026 年涨了十几倍的 AI 行情赢家，"
            "当年无法预知。所以「平均买入持有%」被这些极值拉高，读的时候请同时看"
            "「买入持有中位数%」与「有交易的股票平均买入持有%」。",
            "「平均策略收益%」对从未触发买入的股票记 0（因为一直空仓），与「买入持有」不是同口径；"
            "只有「有交易的股票平均策略收益%」才与同口径的「有交易的股票平均买入持有%」可比。",
            "估值历史来自百度股市通『近五年』，所以个股回测最早只能回到约 5 年前。",
            "信号在评估日（月末）收盘后产生，按**次一交易日收盘价**成交，"
            f"单边成本 {cost:.4f}；停牌超过 15 天视为无法成交。",
            "历史打分禁用了全市场/同行业截面分位（那是今天的快照），只用个股自身历史分位，"
            "因此历史口径与今天的打分口径不完全一致。",
            "「减仓」按卖出一半处理，且**同一轮建仓只减一次**（除非之后触发加仓）；"
            "分红没有再投资，股息率只作为信号，不进入收益。",
        ],
    }
    LOG.info("回测C完成：%d 只股票、%d 笔交易；平均策略收益 %s%%，平均买入持有 %s%%",
             len(sdf), len(tdf), overall.get("平均策略收益%"), overall.get("平均买入持有%"))
    return {"trades": tdf, "stocks": sdf, "metrics": metrics,
            "start": str(dates[0].date()), "end": str(dates[-1].date())}
