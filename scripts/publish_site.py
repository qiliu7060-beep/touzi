"""把网站导出成一个**纯静态**的 ``dist/``，可以直接丢到 Cloudflare Pages / GitHub Pages。

为什么要有这个东西
------------------
本机版（``scripts/serve.py``）是「一台常驻的 Python 服务」：浏览器连的是
``127.0.0.1``，所以必须先有人在这台电脑上把服务跑起来。用户要的是「连上网、
点开网址就能用」——那就不能有后端。于是把服务端的三件事分开处理：

1. **行情**：由浏览器直接向腾讯 ``qt.gtimg.cn`` 取（已实测支持 HTTPS + CORS ``*``）。
   静态站没有后端可代理，所以这一路必须是浏览器直连。
2. **持仓台账**：改存浏览器 ``localStorage``（``scripts/static/app.js``）。
   公开托管的仓库里**不会**出现任何人的真实持仓。
3. **卖出结论**：公共财务、估值与宏观条件由 ``run_all`` 算好并写入
   ``site.js``；PE/PB 自身历史分位及个人止损/止盈在浏览器里随有效报价重新判断。
   未覆盖的股票或报价缺失时显示无法评估，不沿用选股时的动作。

输入是 ``run_all.run_once()`` 的返回值（和 ``serve.py`` 用的是同一批对象），
所以静态站与本机站看到的是**同一套口径**，不会出现两个版本的结论。

产物（``dist/``）
-----------------
``index.html pick.html my.html report.html macro.html backtest.html data.html
rules.html app.js site.js report.md scores.csv tracking.csv macro_context.json
.nojekyll``

用法
----
    F:\\ana\\python.exe scripts\\publish_site.py                # 重算一轮再导出
    F:\\ana\\python.exe scripts\\publish_site.py --reuse        # 复用已有回测，快一点
    F:\\ana\\python.exe scripts\\publish_site.py --serve 8899   # 导出后起个本地静态服务看效果
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import re
import shutil
import sys
import time
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "vendor", ROOT, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

import run_all  # noqa: E402  （复用 run_once，保证静态站与本机站同一套口径）
import webui  # noqa: E402

from touzi.config import load_config  # noqa: E402
from touzi.portfolio import load_fees  # noqa: E402
from touzi.plans import evaluate_sell  # noqa: E402
from touzi.util import Cache, cached_fetch, is_live, resolve_ttls, setup_logging  # noqa: E402

SITE_JS = ('<script src="site.js?v=__ASSET_VERSION__"></script>'
           '<script src="app.js?v=__ASSET_VERSION__"></script>')
NAV = [(href, key, label) for href, key, label in webui.NAV_ITEMS]

# 页面文件名（不带 .html）：与本机版的 8 条路由一一对应
FILE_OF = {"/": "index", "/pick": "pick", "/my": "my", "/report": "report",
           "/macro": "macro", "/backtest": "backtest", "/data": "data", "/rules": "rules"}
EXTRA_FILES = ["report.md", "scores.csv", "tracking.csv", "macro_context.json"]


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _f(v) -> float | None:
    """转成 JSON 能表示的 float；NaN / 空值一律 None。"""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v != v:
        return ""
    return str(v)


def relink(html: str) -> str:
    """把站内绝对链接改成相对文件名。

    GitHub Pages **不会**把 ``/pick`` 映射到 ``pick.html``（Cloudflare Pages 会），
    所以为了「同一份 dist 丢哪都能用」，这里统一改成 ``pick.html``。
    只替换白名单里的路径，不会碰到 ``/api/...`` 之类。
    """
    pairs = [('href="/"', 'href="index.html"')]
    for route, name in FILE_OF.items():
        if route != "/":
            pairs.append((f'href="{route}"', f'href="{name}.html"'))
    # 老入口 / 别名：本机版是 302，静态站直接指向目标文件
    pairs += [('href="/portfolio"', 'href="my.html"'),
              ('href="/dashboard.html"', 'href="index.html"'),
              ('href="/index.html"', 'href="index.html"')]
    for fn in EXTRA_FILES:
        pairs.append((f'href="/{fn}"', f'href="{fn}"'))
    for a, b in pairs:
        html = html.replace(a, b)
    return html


def valuation_thresholds(cache, codes, plan: dict, ttl_days: float,
                         as_of: str) -> dict[str, dict]:
    """只读本轮评分已使用的百度「近五年」缓存，不新增网络请求。

    与 screener 一样：按 as_of 截断、原序列 >20 项、正有限样本 >=5。
    percentile_of 使用 count(pool < v)/n，score_valuation 再 round(..., 4)。
    找到最小满足实际评分比较的计数 k，导出未舍入的 ordered[k-1]；
    浏览器必须用严格 >。重复样本、非整数百分比及评分舍入均保持一致。
    截面、行业、价格推算值及过期缓存绝不作为个股五年历史。
    """
    import numpy as np

    out = {}
    cutoff = pd.to_datetime(as_of, errors="coerce")
    if pd.isna(cutoff):
        return out
    for code in sorted(set(codes)):
        rec = {"as_of": as_of, "source": "个股5年历史"}
        for metric, indicator in (("pe", "市盈率(TTM)"), ("pb", "市净率")):
            item = {"known": False, "reason": "有效个股五年历史缓存缺失或样本不足"}
            rec[metric] = item
            try:
                raw = cache.get(f"val_baidu_{code}_{indicator}_近五年", ttl_days=ttl_days)
                if raw is None:
                    continue
                dates = pd.to_datetime(raw["date"], errors="coerce")
                series = pd.Series(pd.to_numeric(raw["value"], errors="coerce").to_numpy(),
                                   index=pd.DatetimeIndex(dates))
                series = series[~series.index.isna()].dropna().sort_index()
                series = series[~series.index.duplicated(keep="last")]
                series = series[series.index <= cutoff]
                arr = series.to_numpy(dtype=float)
                arr = np.sort(arr[np.isfinite(arr) & (arr > 0)])
                if len(series) <= 20 or len(arr) < 5:
                    continue
                q = float(plan[f"sell_{metric}_percentile"])
                if not np.isfinite(q) or not 0 <= q <= 1:
                    continue
                n = len(arr)
                k = next(k for k in range(n + 1) if round(k / n, 4) >= q)
                item.update(known=True, boundary=float(arr[k - 1]) if k else 0.0,
                            samples=n, percentile=q, history_end=series.index[-1].strftime("%Y-%m-%d"),
                            reason="")
            except (KeyError, TypeError, ValueError, OSError):
                continue
        out[code] = rec
    return out


BENCHMARK_ETFS = (
    ("510300", "沪深300 ETF", "#60a5fa"),
    ("510050", "上证50 ETF", "#f59e0b"),
    ("513500", "标普500 ETF", "#34d399"),
    ("513100", "纳指 ETF", "#f472b6"),
)


def benchmark_history(cache: Cache, ttl_days: float = 1.0) -> dict:
    """给浏览器端收益对比烘入四只 ETF 的周线。

    用可成交的人民币 ETF，而不是不可直接买入的指数点位。周线足以画长期比较图，
    同时把静态 ``site.js`` 控制在很小的体积内。单只抓取失败不会拖垮整站发布。
    """
    instruments = []
    for code, label, color in BENCHMARK_ETFS:
        try:
            import requests

            def _fetch_weekly(code=code):
                symbol = f"sh{code}"
                resp = requests.get(
                    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                    params={"param": f"{symbol},week,,,1000,qfq"},
                    timeout=30,
                    headers={"User-Agent": "Mozilla/5.0 touzi/0.1"},
                )
                resp.raise_for_status()
                bag = ((resp.json().get("data") or {}).get(symbol) or {})
                rows = bag.get("qfqweek") or bag.get("week") or []
                if not rows:
                    raise RuntimeError("腾讯 ETF 周线为空")
                return pd.DataFrame({"date": [r[0] for r in rows],
                                     "close": [r[2] for r in rows]})

            raw = cached_fetch(
                cache, f"benchmark_etf_qfqweek_sh{code}_v1", _fetch_weekly,
                ttl_days=ttl_days, retries=1, sleep=1.0,
            )
            df = raw.copy()
            df["date"] = pd.to_datetime(df.get("date"), errors="coerce")
            df["close"] = pd.to_numeric(df.get("close"), errors="coerce")
            close = df.dropna(subset=["date", "close"]).set_index("date")["close"]
            close = close[close.index >= pd.Timestamp("2013-01-01")]
            close = close[close > 0]
            weekly = close
            weekly = weekly[~weekly.index.duplicated(keep="last")].sort_index()
            instruments.append({
                "code": code, "label": label, "color": color,
                "prices": [[d.strftime("%Y-%m-%d"), round(float(v), 4)]
                           for d, v in weekly.items()],
            })
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 对比基准 {label}({code}) 历史行情不可用：{exc}")
    last_dates = [x["prices"][-1][0] for x in instruments if x.get("prices")]
    return {
        "as_of": max(last_dates) if last_dates else "",
        "instruments": instruments,
        "lot_size": 100,
        # ETF 在交易所买卖：沿用券商佣金（含最低佣金），不收股票印花税和过户费。
        "fee_note": "ETF 买卖佣金按当前配置计算（含最低佣金）；不计股票印花税和过户费。",
    }


def site_js(res: dict, cfg, thr: dict | None = None,
            benchmarks: dict | None = None) -> str:
    """生成 ``site.js``：静态站唯一的数据来源（结论 + 费率 + 减仓比例）。"""
    fees = load_fees(cfg)
    plan = cfg.get("plan", {}) or {}
    reduce_ratio = _f(plan.get("reduce_ratio"))
    if reduce_ratio is None:
        reduce_ratio = 0.5

    entries: dict[str, dict] = {}

    scores = res.get("scores")
    if scores is not None and len(scores):
        for _, r in scores.iterrows():
            code = re.sub(r"\D", "", _s(r.get("代码"))).zfill(6)
            if not re.fullmatch(r"\d{6}", code):
                continue
            entries[code] = {
                "name": _s(r.get("名称")),
                "action": "候选",
                "total": _f(r.get("总分")),
                "quality": _f(r.get("质量分")),
                "quality_state": _s(r.get("质量状态")),
                "industry": _s(r.get("行业")),
                "pe": _f(r.get("PE(TTM)") if "PE(TTM)" in scores.columns else r.get("PE_TTM")),
                "pb": _f(r.get("PB")),
                "dy": _f(r.get("股息率%")),
                "triggers": "", "failed": "", "warn": "",
            }

    # 公共卖出条件覆盖本轮所有已评分股票，不受跟踪表 top 40 的截断影响。
    # 不把任何浏览器持仓、成本或数量写入公开快照；相对成本止损在浏览器现算。
    macro = res.get("macro")
    macro_score = getattr(macro, "macro_score", float("nan"))
    for score in res.get("stock_results") or []:
        code = re.sub(r"\D", "", _s(getattr(score, "code", ""))).zfill(6)
        if not re.fullmatch(r"\d{6}", code):
            continue
        e = entries.setdefault(code, {"name": _s(getattr(score, "name", "")),
                                      "action": "候选", "triggers": "", "failed": "", "warn": ""})
        checks = evaluate_sell(score, cfg, macro_score=macro_score, holding=None)
        public_checks = [c for c in checks if c.key not in {"sell_stop_loss", "sell_take_profit"}]
        quality = (getattr(score, "parts", {}) or {}).get("quality")
        detail = getattr(quality, "detail", {}) or {}
        e["financial_analysis"] = detail.get("financial_analysis")
        e["financial_capability"] = e["financial_analysis"] is not None
        financial_keys = {"sell_debt_equity", "sell_equity_nonpositive", "sell_capex_to_profit",
                          "sell_annual_loss", "sell_cumulative_loss"}
        metrics = (e["financial_analysis"] or {}).get("metrics", {})
        metric_by_check = {"sell_debt_equity": "debt_equity", "sell_equity_nonpositive": "debt_equity",
                           "sell_capex_to_profit": "capex_to_profit", "sell_annual_loss": "annual_net_profit",
                           "sell_cumulative_loss": "cumulative_net_profit"}
        e["sell_checks"] = [vars(c) for c in public_checks]
        e["sell_snapshot"] = {
            "financial_warn": [c.label for c in public_checks if c.key in financial_keys and c.hit],
            "financial_missing": [c.label for c in public_checks if c.key in financial_keys and not c.known
                                  and metrics.get(metric_by_check[c.key], {}).get("status") != "not_applicable"],
            "percentile_labels": {c.key: c.label for c in public_checks
                                  if c.key in {"sell_pe_percentile", "sell_pb_percentile"}},
            "exit": [c.label for c in public_checks if c.hit and c.level == "清仓"],
            "reduce": [c.label for c in public_checks if c.hit and c.level == "减仓"],
            "warn": [c.label for c in public_checks if c.hit and c.level == "预警"],
            "missing": [c.label for c in public_checks if not c.known and c.level != "预警"],
            "missing_warn": [c.label for c in public_checks if not c.known and c.level == "预警"],
        }

    track = res.get("track")
    if track is not None and len(track):
        for _, r in track.iterrows():
            code = re.sub(r"\D", "", _s(r.get("代码"))).zfill(6)
            if not re.fullmatch(r"\d{6}", code):
                continue
            e = entries.setdefault(code, {"name": "", "action": "候选", "total": None,
                                          "pe": None, "pb": None, "dy": None,
                                          "triggers": "", "failed": "", "warn": ""})
            e.update({
                "name": _s(r.get("名称")) or e.get("name", ""),
                "action": _s(r.get("建议动作")) or e.get("action", "候选"),
                "total": _f(r.get("总分")) if _f(r.get("总分")) is not None else e.get("total"),
                "pe": _f(r.get("PE_TTM")) if _f(r.get("PE_TTM")) is not None else e.get("pe"),
                "pb": _f(r.get("PB")) if _f(r.get("PB")) is not None else e.get("pb"),
                "dy": _f(r.get("股息率%")) if _f(r.get("股息率%")) is not None else e.get("dy"),
                "triggers": _s(r.get("触发的卖出条件")),
                "failed": _s(r.get("未通过的门槛")),
                "warn": _s(r.get("预警")),
            })

    # 公共选股决策覆盖全部已评分股票；个人跟踪top40和持仓动作不得覆盖公共buy结论。
    from touzi.close_snapshot import public_entry
    all_results = list(res.get("stock_results") or [])
    ranked = sorted(all_results, key=lambda item: (not pd.notna(item.total),
                    -float(item.total) if pd.notna(item.total) else 0, item.code))
    ranks = {item.code: (i + 1) / len(ranked) for i, item in enumerate(ranked)}
    for score in all_results:
        quote_at = _s((res.get("flags") or {}).get("quote_at"))
        entries[score.code].update(public_entry(score, cfg, macro_score, quote_at, ranks[score.code]))

    flags = res.get("flags", {}) or {}
    # 前端要用的阈值原样带过去，免得 JS 里再抄一遍数字（抄了就会走样）
    js_plan = {}
    for k in ("sell_stop_loss", "sell_take_profit", "sell_pe_abs", "sell_graham",
              "sell_pe_percentile", "sell_pb_percentile",
              "sell_dividend_yield", "sell_total", "sell_macro_score",
              "sell_reduce_total", "sell_macro_reduce_score"):
        v = _f(plan.get(k))
        if v is not None:
            js_plan[k] = v
    js_plan["reduce_ratio"] = reduce_ratio

    from touzi.market_calendar import CALENDAR
    from touzi.financial_coverage import financial_coverage
    payload = {
        "trading_calendar": CALENDAR,
        "financial_coverage": financial_coverage(entries),
        "built_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M"),
        "as_of": _s(flags.get("as_of")),
        "quote_at": _s(flags.get("quote_at")),
        "quote_coverage": _f(flags.get("quote_coverage")),
        "interval": int(webui.LIVE_INTERVAL),
        "fees": {"commission_rate": fees.commission_rate,
                 "commission_min": fees.commission_min,
                 "stamp_duty_rate": fees.stamp_duty_rate,
                 "transfer_fee_rate": fees.transfer_fee_rate},
        "plan": js_plan,
        "stock_weights": dict(cfg.get("stock_weights", {}) or {}),
        "sell_rules": [{"level": c.level, "label": c.label}
                       for c in evaluate_sell(None, cfg)],
        # thr[code] = {pe_sell: 该股 PE 自身历史分位到 80% 时的 PE 值, ...}
        # 有了它，浏览器就能用实时 PE 精确判「分位 ≥ 80%」这条卖出规则。
        "thr": thr or {},
        "entries": entries,
        "benchmarks": benchmarks or {"as_of": "", "instruments": [], "lot_size": 100},
    }
    return ("/* 由 scripts/publish_site.py 生成，不要手改。\n"
            "   公共快照在发布时计算；历史分位及个人止损/止盈由浏览器按有效报价计算。 */\n"
            "window.__SITE__ = " + json.dumps(payload, ensure_ascii=False,
                                             separators=(",", ":")) + ";\n")


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
def overview_body(res: dict) -> str:
    """静态版总览：KPI 那几格留给浏览器的本地台账，其余照旧。"""
    body = [webui.hero(
        "总览",
        "一屏之内回答两件事：<b>现在是什么环境</b>，<b>我该做什么</b>。"
        "行情每 15 秒自己刷新（浏览器直接取，不经过任何服务器）。",
        [("/pick", "选股 · 勾选建仓"), ("/my", "我的持仓"),
         ("/report", "完整报告"), ("/data", "数据新鲜度")])]
    body.append(
        '<h2>我的持仓（实时）</h2>'
        '<div class="grid grid3" id="__kpi"></div>'
        '<p class="hint">这几格按<b>你自己浏览器里存的持仓</b>实时算——'
        '所以换个浏览器、换台电脑就是空的，这是有意为之：'
        '公开的网站上不该有任何人的真实持仓。'
        '买入筛选与公共风险信号按最近一次发布计算，个人止损/止盈随报价重算；'
        '没有完整数据时会显示「无法评估」。</p>')
    body.append(webui.R.render_overview(
        macro=res.get("macro"), my_port=None,
        track=res.get("track"), freshness=res.get("freshness")))
    return "".join(body)


_FORM = """
<div class="grid grid2">
  <div>
    <div class="kv"><span>股票代码</span><b><input class="ip" id="f_code" type="text" placeholder="600519" style="width:110px"></b></div>
    <div class="kv"><span>名称（可留空）</span><b><input class="ip" id="f_name" type="text" placeholder="自动带出" style="width:150px"></b></div>
    <div class="kv"><span>买入日期</span><b><input class="ip" id="f_date" type="date" style="width:150px"></b></div>
  </div>
  <div>
    <div class="kv"><span>买入价</span><b><input class="ip" id="f_price" type="text" placeholder="1450.00" style="width:110px"></b></div>
    <div class="kv"><span>股数</span><b><input class="ip" id="f_shares" type="text" placeholder="100" style="width:110px"></b></div>
    <div class="kv"><span>备注</span><b><input class="ip" id="f_note" type="text" placeholder="可留空" style="width:150px"></b></div>
  </div>
</div>
<input type="hidden" id="f_idx" value="">
<p class="small muted" id="f_msg" style="min-height:1.2em"></p>
<p>
  <button class="b" type="button" onclick="__holdSave()">保存</button>
  <button class="b b2" type="button" onclick="__holdClear()">清空表单</button>
</p>
"""

_MY_TOOLS = """
<p class="small muted">
  台账只存在<b>你这台浏览器</b>里，不会上传。换浏览器/清缓存就没了，所以：
</p>
<p>
  <button class="b b2" type="button" onclick="__holdExport()">导出成 holdings.csv</button>
  <button class="b b2" type="button" onclick="__holdWipe()">清空全部持仓</button>
</p>
<textarea id="f_import" rows="3" style="width:100%" placeholder="把 holdings.csv 的内容粘到这里（第一行要留表头），然后点下面这个按钮"></textarea>
<p><button class="b b2" type="button" onclick="__holdImport()">从上面的文本导入</button></p>
"""


def my_body(cfg) -> str:
    """静态版的「我的持仓」：表单 + 三张由 app.js 填的表。"""
    fees = load_fees(cfg)
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    body = [webui.hero(
        "我的持仓",
        "录入之后系统持续跟踪：现价、市值、浮动盈亏 <b>每 15 秒自动刷新</b>；"
        "个人止损/止盈随报价重算；财务、估值和宏观卖出条件按网站最近一次发布的公共快照判断。",
        [("/pick", "去选股 · 勾选建仓"), ("/rules", "看卖出规则")])]
    body.append(webui.card(
        "① 录入 / 修改一笔持仓",
        _FORM.replace('id="f_date" type="date"', f'id="f_date" type="date" value="{today}"'),
        "代码必须是 6 位数字。同一只股票分批买入就录多行，系统按加权平均合并成本。"))
    body.append(webui.card(
        "② 持仓台账",
        '<div class="scroll"><table><tbody id="__ledger">'
        '<tr><td colspan="8" class="muted">正在读取本地台账…</td></tr>'
        '</tbody></table></div>'
        '<p class="small muted" id="__ledgernote"></p>',
        "网站读写的就是浏览器的本地存储，没有服务器。"))
    body.append(webui.card(
        "③ 现在该怎么办",
        '<div class="scroll"><table><tbody id="__live">'
        '<tr><td class="muted">正在读取本地台账…</td></tr></tbody></table></div>',
        "任一清仓条件优先于减仓条件；都未触发时显示「一切照旧」。缺少该股评分或报价时显示「无法评估」，不假装安全。"
        "公开条件以页面显示的评分发布时间为准，价格条件以页面显示的报价时间为准。"
        "触发后请在可交易时段核对报价；本页不自动下单。"
        "「到账金额」「扣费后净盈利」按你自己填的买入价和当前报价计算；实际成交以交易所为准。"))
    body.append('<h2>实时盈亏</h2><div class="grid grid3" id="__kpi"></div>')
    body.append(webui.card(
        "④ 与四等分指数 ETF 组合对比",
        '<div id="__compare" class="compare-wrap">'
        '<p class="muted small">正在读取持仓并计算历史对比…</p></div>',
        "按你的每笔实际投入日期与含费金额，同步把资金四等分投入沪深300、上证50、标普500和纳指 ETF；"
        "采用整手交易并保留未成交现金。"))
    body.append(webui.card(
        "⑤ 备份 / 迁移 / 换浏览器",
        _MY_TOOLS,
        "持仓不上传，所以删缓存就没了——想留就点上面的「导出」。"))
    body.append(webui.card("⑥ 手续费口径", webui._fees_table(fees),
                           "和本机版用的是同一份配置（config/settings.toml 的 [fees]）。"))
    return "".join(body)


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def export(res: dict, cfg, dist: Path, benchmarks: dict | None = None,
           cache: Cache | None = None) -> list[Path]:
    dist.mkdir(parents=True, exist_ok=True)
    # 每次发布生成新的查询参数，绕过浏览器和 Pages CDN 对旧 JS 的短时缓存。
    scripts = SITE_JS.replace("__ASSET_VERSION__", str(int(time.time())))
    # 静态资源先摆好：页面里的 <script src> 是相对路径
    shutil.copy2(ROOT / "scripts" / "static" / "app.js", dist / "app.js")
    if cache is None:
        cache = Cache(cfg.resolve(cfg.general.get("cache_dir", "data_cache")))
    codes = [str(score.code).zfill(6) for score in res.get("stock_results") or []]
    thresholds = valuation_thresholds(cache, codes, cfg.get("plan", {}) or {},
                                      resolve_ttls(cfg)["history"],
                                      _s((res.get("flags") or {}).get("as_of")))
    (dist / "site.js").write_text(site_js(res, cfg, thr=thresholds, benchmarks=benchmarks), encoding="utf-8")
    from touzi.live_model import export_model
    (dist / "live-model.json").write_text(
        json.dumps(export_model(res, cfg), ensure_ascii=False, allow_nan=False,
                   separators=(",", ":")), encoding="utf-8")
    (dist / ".nojekyll").write_text("", encoding="utf-8")

    written: list[Path] = [dist / "app.js", dist / "site.js"]

    for href, key, label in NAV:
        name = FILE_OF[href]
        if href == "/":
            body = overview_body(res)
        elif href == "/my":
            body = my_body(cfg)
        elif href == "/pick":
            body = webui.view_pick(res)
        elif href == "/report":
            body = webui.view_report(res)
        elif href == "/macro":
            body = webui.view_macro(res)
        elif href == "/backtest":
            body = webui.view_backtest(res)
        elif href == "/data":
            body = webui.view_data(res)
        else:
            body = webui.view_rules(res, cfg)
        html = webui.page(label, key, body, cfg=cfg, script=scripts)
        html = relink(html)
        p = dist / f"{name}.html"
        p.write_text(html, encoding="utf-8")
        written.append(p)

    out_dir = cfg.resolve(cfg.general.get("output_dir", "output"))
    for fn in EXTRA_FILES:
        src = out_dir / fn
        if src.exists():
            shutil.copy2(src, dist / fn)
            written.append(dist / fn)

    # 静态托管上 / 的兜底：直接用 index.html，并给 404 一条回总览的路
    (dist / "404.html").write_text(
        relink(webui.page("找不到这个页面", "", webui.hero(
            "找不到这个页面",
            "地址可能写错了。回到总览重新点一次导航即可。",
            [("/", "回到总览")]), cfg=cfg, script=scripts)),
        encoding="utf-8")
    written.append(dist / "404.html")
    return written


def verify(dist: Path, limit: int = 60) -> list[str]:
    """导出后的静态自检：不依赖任何服务，直接读文件。"""
    bad: list[str] = []

    def walk(d: Path) -> list[str]:
        out: list[str] = []
        for p in sorted(d.rglob("*")):
            if p.is_file():
                out.append(p.relative_to(dist).as_posix())
        return out

    files = walk(dist)
    need = [f"{name}.html" for name in FILE_OF.values()] + [
        "app.js", "site.js", ".nojekyll", "404.html"]
    for n in need:
        if n not in files:
            bad.append(f"缺少 {n}")

    for name in FILE_OF.values():
        p = dist / f"{name}.html"
        if not p.exists():
            continue
        h = p.read_text(encoding="utf-8")
        if h.count('class="nv') != len(NAV):
            bad.append(f"{name}.html 导航数 {h.count('class=\"nv')} ≠ {len(NAV)}")
        if h.count('class="nv on"') != 1:
            bad.append(f"{name}.html 当前页高亮数 {h.count('class=\"nv on\"')} ≠ 1")
        if "http://" in h or "https://" in h.replace("https://qt.gtimg.cn", ""):
            bad.append(f"{name}.html 里有站外链接")
        if 'href="/' in h:
            bad.append(f"{name}.html 里还有站内绝对链接 href=\"/…（静态托管会 404）")
        if "Traceback" in h or ">None<" in h or ">nan<" in h:
            bad.append(f"{name}.html 里有脏值")
        if "site.js" not in h or "app.js" not in h:
            bad.append(f"{name}.html 没有挂上静态脚本")
        if name in ("index", "my") and 'id="__kpi"' not in h:
            bad.append(f"{name}.html 缺少本地台账挂载点 __kpi")
    my = dist / "my.html"
    if my.exists():
        h = my.read_text(encoding="utf-8")
        for mid in ("__ledger", "__live", "f_code", "f_price", "f_shares", "f_date"):
            if f'id="{mid}"' not in h:
                bad.append(f"my.html 缺少 {mid}")
        if 'id="__compare"' not in h:
            bad.append("my.html 缺少收益对比挂载点 __compare")
    pick = dist / "pick.html"
    if pick.exists():
        h = pick.read_text(encoding="utf-8")
        if h.count("data-pick=") == 0:
            bad.append("pick.html 没有可勾选的候选行")
        if h.count('data-q="') == 0:
            bad.append("pick.html 没有 data-q，价格不会实时刷新")

    # site.js 能不能被 JS 解析、entries 够不够多
    sj = dist / "site.js"
    if sj.exists():
        txt = sj.read_text(encoding="utf-8")
        m = re.search(r"window\.__SITE__ = (\{.*\});", txt, re.S)
        if not m:
            bad.append("site.js 里找不到 window.__SITE__ 赋值")
        else:
            try:
                d = json.loads(m.group(1))
            except json.JSONDecodeError as exc:
                bad.append(f"site.js 不是合法 JSON：{exc}")
                d = {}
            n = len(d.get("entries") or {})
            if n < 10:
                bad.append(f"site.js 的 entries 只有 {n} 只，太少了（应该上百）")
            if not d.get("built_at"):
                bad.append("site.js 没有 built_at")
            bench = d.get("benchmarks") or {}
            if len(bench.get("instruments") or []) != 4:
                bad.append("site.js 的四等分 ETF 基准数据不完整")
    return bad


def serve_dist(dist: Path, port: int) -> None:
    """起一个最小的本地静态服务，用浏览器预览导出结果。"""
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(dist))
    url = f"http://127.0.0.1:{port}/index.html"
    print(f"\n本地预览：{url}　（Ctrl+C 停止）")
    import webbrowser
    import threading
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已停止预览。")


def validate_quote_freshness(flags: dict, min_quote_time: str,
                             now: datetime | None = None) -> list[str]:
    """定时发布前核验行情源时间；不把旧缓存包装成今日开盘/收盘评分。"""
    shanghai_tz = ZoneInfo("Asia/Shanghai")
    current = now or datetime.now(shanghai_tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=shanghai_tz)
    else:
        current = current.astimezone(shanghai_tz)
    quote_text = str(flags.get("quote_at") or "")
    try:
        quote_at = datetime.strptime(quote_text, "%Y-%m-%d %H:%M").replace(
            tzinfo=shanghai_tz)
    except ValueError:
        return ["行情源未返回真实报价时间"]
    errors: list[str] = []
    from touzi.market_calendar import market_clock
    clock = market_clock(current)
    if clock is None or quote_at.date() != clock["quote_day"]:
        errors.append(f"报价时间 {quote_at:%Y-%m-%d %H:%M} 不在已核验交易日历的有效窗口")
    elif clock["phase"] == "closed" and quote_at.strftime("%H:%M") < "15:00":
        errors.append("休市或盘前仅接受最近交易日完整收盘报价")
    if quote_at.strftime("%H:%M") < min_quote_time:
        errors.append(f"报价时间 {quote_at:%H:%M} 早于要求的 {min_quote_time}")
    if quote_at > current + timedelta(minutes=5):
        errors.append("报价时间晚于当前时间，数据不可信")
    coverage = _f(flags.get("quote_coverage")) or 0.0
    if coverage < 0.9:
        errors.append(f"有真实报价的候选股仅占 {coverage:.1%}，低于 90%")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description="把 touzi 导出成纯静态网站（dist/）")
    ap.add_argument("--config", default=None)
    ap.add_argument("--limit", type=int, default=300, help="候选股上限（默认 300）")
    ap.add_argument("--reuse", action="store_true", help="复用已有回测结果，跳过重跑")
    ap.add_argument("--skip-backtest", action="store_true",
                    help="跳过历史回测，只更新宏观、选股、评分和网站（适合定时发布）")
    ap.add_argument("--weekly-macro", action="store_true",
                    help="同一北京时间自然周复用宏观评分，个股仍每次重算")
    ap.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    ap.add_argument("--min-quote-time", choices=("09:30", "15:00"), default=None,
                    help="定时发布门槛：要求今日真实报价达到开盘后或收盘时段")
    ap.add_argument("--out", default="dist", help="导出目录（默认 dist）")
    ap.add_argument("--serve", type=int, default=0, metavar="PORT",
                    help="导出后起本地静态服务预览")
    ns = ap.parse_args()

    setup_logging()
    cfg = load_config(ns.config)
    out_dir = cfg.resolve(cfg.general.get("output_dir", "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    dist = (ROOT / ns.out) if not Path(ns.out).is_absolute() else Path(ns.out)

    print("=" * 68)
    print(f"导出静态站 → {dist}")
    print("=" * 68)

    t0 = time.time()
    args = run_all.build_parser().parse_args([])
    args.limit = ns.limit
    args.reuse_backtest = bool(ns.reuse)
    args.skip_backtest = bool(ns.skip_backtest)
    args.skip_stocks_backtest = bool(ns.skip_backtest)
    args.weekly_macro = bool(ns.weekly_macro)
    args.live = not ns.offline
    cfg["general"]["live"] = bool(args.live)
    cache = Cache(cfg.resolve(cfg.general.get("cache_dir", "data_cache")))
    proxy = cfg.general.get("proxy") or None

    res = run_all.run_once(cfg, args, cache, proxy, out_dir)
    if is_live(cfg):
        minimum = ns.min_quote_time or ("15:00" if datetime.now(ZoneInfo("Asia/Shanghai")).hour >= 15 else "09:30")
        freshness_errors = validate_quote_freshness(res.get("flags") or {}, minimum)
        if freshness_errors:
            print("不发布过期评分：" + "；".join(freshness_errors))
            return 1
    print("\n准备四等分 ETF 对比周线…")
    benchmarks = benchmark_history(cache, ttl_days=1.0)
    written = export(res, cfg, dist, benchmarks=benchmarks, cache=cache)
    bad = verify(dist)

    total = sum(p.stat().st_size for p in dist.rglob("*") if p.is_file())
    print(f"\n写出 {len(written)} 个文件，合计 {total / 1024:.0f} KB，用时 {time.time() - t0:.0f} 秒")
    for p in sorted(dist.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(dist).as_posix():<20} {p.stat().st_size:>8,} B")
    if bad:
        print("\n静态自检失败：")
        for b in bad:
            print(f"  ✗ {b}")
        return 1
    print("\n静态自检通过：8 个页面 + app.js + site.js + 导出文件，站内链接全是相对路径。")
    print("\n下一步：双击工作区根目录的 `更新公网网站.bat`（它会自动打开上传页），"
          "\n把 dist 文件夹里的文件传到 GitHub 仓库并开 Pages。"
          "\n详细步骤见 README 的「公网版」一节。")
    print("\n想先在本机看效果：加 --serve 8899 起一个纯静态预览。")

    if ns.serve:
        serve_dist(dist, ns.serve)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


