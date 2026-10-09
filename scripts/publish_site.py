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
3. **结论**（建议动作 / 触发的卖出条件 / 预警）：浏览器算不了（要估值分位、
   打分、规则表），所以在这里就由 ``run_all`` 算好、烘进 ``site.js`` 的 ``entries``。
   长期投资的结论本来就是「一天一变」，可以接受；钱是实时的。

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
from datetime import datetime
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
from touzi.data.stock import fetch_valuation_history  # noqa: E402
from touzi.portfolio import load_fees  # noqa: E402
from touzi.util import Cache, is_live, setup_logging  # noqa: E402

SITE_JS = '<script src="site.js"></script><script src="app.js"></script>'
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


def valuation_thresholds(cache, codes, plan: dict, ttl_days: float) -> dict[str, dict]:
    """给每只股票烘一个「PE/PB 涨到这个数就等于自身历史分位到了 X%」的门槛值。

    为什么这样可以做到**精确**：`percentile_of(v, pool)` 的定义是
    ``count(pool < v) / n``，它是 v 的**单调阶梯函数**。所以
    「分位 ≥ 80%」等价于「v > pool 排序后第 ceil(0.8n) 个值」——一个数就能定死。
    于是浏览器只要拿腾讯行情里的实时 PE/PB 跟这个数比一下，就和 Python 端
    `evaluate_sell()` 的判断**完全一致**，不必把 5 年日频序列搬到前端。

    这样「PE/PB 回到自身历史分位 ≥ 80% 就该卖」这条规则从「发布时冻结」
    变成了「打开网页实时判」——而它正是回测里表现最好的那条卖出规则。
    """
    import numpy as np

    def n_of(scale_key: str, default: float) -> int:
        try:
            return int(round(100 * float(plan.get(scale_key, default) or default)))
        except (TypeError, ValueError):
            return int(round(100 * default))

    buy_pct = n_of("buy_max_pe_percentile", 0.40)
    out: dict[str, dict] = {}
    for code in codes:
        rec: dict[str, float] = {}
        for metric, indicator, prefix in (("pe", "市盈率(TTM)", "pe"), ("pb", "市净率", "pb")):
            try:
                s = fetch_valuation_history(code, cache, indicator, "近五年", ttl_days=ttl_days)
            except Exception:  # noqa: BLE001
                continue
            arr = pd.to_numeric(s, errors="coerce").to_numpy(dtype=float)
            arr = arr[np.isfinite(arr) & (arr > 0)]
            if len(arr) < 60:
                continue
            ordered = np.sort(arr)
            n = len(ordered)
            for q, key in ((n_of("sell_%s_percentile" % metric, 0.80), f"{prefix}_sell"),
                           (n_of("buy_max_%s_percentile" % metric, 0.40), f"{prefix}_buy")):
                need = int(np.ceil(q / 100.0 * n))
                if need <= 0:
                    continue
                # percentile(v) >= q/100  ⟺  v > ordered[need-1]
                rec[key] = round(float(ordered[need - 1]), 4)
        if rec:
            out[code] = rec
    return out


def site_js(res: dict, cfg, thr: dict | None = None) -> str:
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
                "pe": _f(r.get("PE(TTM)") if "PE(TTM)" in scores.columns else r.get("PE_TTM")),
                "pb": _f(r.get("PB")),
                "dy": _f(r.get("股息率%")),
                "triggers": "", "failed": "", "warn": "",
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

    flags = res.get("flags", {}) or {}
    # 前端要用的阈值原样带过去，免得 JS 里再抄一遍数字（抄了就会走样）
    js_plan = {}
    for k in ("sell_stop_loss", "sell_take_profit", "sell_pe_abs", "sell_graham",
              "sell_dividend_yield", "sell_total", "sell_macro_score",
              "sell_reduce_total", "sell_macro_reduce_score"):
        v = _f(plan.get(k))
        if v is not None:
            js_plan[k] = v
    js_plan["reduce_ratio"] = reduce_ratio

    payload = {
        "built_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M"),
        "as_of": _s(flags.get("as_of")),
        "interval": int(webui.LIVE_INTERVAL),
        "fees": {"commission_rate": fees.commission_rate,
                 "commission_min": fees.commission_min,
                 "stamp_duty_rate": fees.stamp_duty_rate,
                 "transfer_fee_rate": fees.transfer_fee_rate},
        "plan": js_plan,
        # thr[code] = {pe_sell: 该股 PE 自身历史分位到 80% 时的 PE 值, ...}
        # 有了它，浏览器就能用实时 PE 精确判「分位 ≥ 80%」这条卖出规则。
        "thr": thr or {},
        "entries": entries,
    }
    return ("/* 由 scripts/publish_site.py 生成，不要手改。\n"
            "   结论是发布那一刻的；价格由浏览器直接向腾讯行情取，是实时的。 */\n"
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
        '买入门槛、卖出触发这类结论仍然是发布那一刻算好的。</p>')
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
        "卖出提醒、建议卖出股数、扣掉手续费后的净盈利，来自发布那一刻算好的规则结论。",
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
        "「建议动作」来自发布时的规则结论；「到账金额」「扣费后净盈利」按你自己填的买入价实时算。"))
    body.append('<h2>实时盈亏</h2><div class="grid grid3" id="__kpi"></div>')
    body.append(webui.card(
        "④ 备份 / 迁移 / 换浏览器",
        _MY_TOOLS,
        "持仓不上传，所以删缓存就没了——想留就点上面的「导出」。"))
    body.append(webui.card("⑤ 手续费口径", webui._fees_table(fees),
                           "和本机版用的是同一份配置（config/settings.toml 的 [fees]）。"))
    return "".join(body)


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def export(res: dict, cfg, dist: Path) -> list[Path]:
    dist.mkdir(parents=True, exist_ok=True)
    # 静态资源先摆好：页面里的 <script src> 是相对路径
    shutil.copy2(ROOT / "scripts" / "static" / "app.js", dist / "app.js")
    (dist / "site.js").write_text(site_js(res, cfg), encoding="utf-8")
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
        html = webui.page(label, key, body, cfg=cfg, script=SITE_JS)
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
            [("/", "回到总览")]), cfg=cfg, script=SITE_JS)),
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


def main() -> int:
    ap = argparse.ArgumentParser(description="把 touzi 导出成纯静态网站（dist/）")
    ap.add_argument("--config", default=None)
    ap.add_argument("--limit", type=int, default=300, help="候选股上限（默认 300）")
    ap.add_argument("--reuse", action="store_true", help="复用已有回测结果，跳过重跑")
    ap.add_argument("--skip-backtest", action="store_true",
                    help="跳过历史回测，只更新宏观、选股、评分和网站（适合定时发布）")
    ap.add_argument("--offline", action="store_true", help="只用缓存，不联网")
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
    args.live = not ns.offline
    cfg["general"]["live"] = bool(args.live)
    cache = Cache(cfg.resolve(cfg.general.get("cache_dir", "data_cache")))
    proxy = cfg.general.get("proxy") or None

    res = run_all.run_once(cfg, args, cache, proxy, out_dir)
    written = export(res, cfg, dist)
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
