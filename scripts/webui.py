"""统一网站：**一个站点、一套外观、一份数据**。

这一层存在的理由，就是不要再出现「看板一个样、持仓页另一个样、数据散在一堆 CSV 里」。
所有页面都是同一套顶栏 + 同一套 CSS，正文则来自 ``touzi.report`` 里**同一批 section**
（``dashboard_sections``）或同一批对象（``run_once`` 的返回值），所以改一处就全站跟着改，
不会出现两套口径。

两种「更新」被刻意分开，这是本网站最要紧的设计：

* **整页重算**（几分钟一次，后台线程）：重出结论——打分、宏观背景分、目标仓位、买卖决策。
* **秒级报价**（15 秒一次，浏览器轮询 ``/api/quotes``）：只重算**钱**——现价、市值、
  浮动盈亏、扣费后净盈利。数字原地跳动并闪一下颜色，不等整页重算。

所以页面上的价格类单元格都带 ``data-q="代码.字段"``，由 :func:`live_js` 那段脚本改写。
``touzi.portfolio.Portfolio.requote`` 在服务端用同一套费率公式重算，前端只负责显示。

**不假装宏观实时**：PMI/CPI/PPI/GDP 是国家统计局月度、季度公布，财务是季度、分红是事件，
这几类页面上都明写了「天然滞后」。行情（价格/PE/PB）才是真能秒级刷新的那一块。
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "vendor", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from touzi import report as R  # noqa: E402
from touzi.portfolio import FeeModel, Portfolio, holdings_device, load_fees  # noqa: E402
from touzi.tracker import load_holdings  # noqa: E402


# --------------------------------------------------------------------------- #
# 导航：整个系统就这 8 个地方，全部在这一个站点里
# --------------------------------------------------------------------------- #
# 页面实时报价的轮询间隔（秒）。serve.py 用 --quote-interval 覆盖它。
LIVE_INTERVAL: float = 15.0

NAV_ITEMS: list[tuple[str, str, str]] = [    ("/", "overview", "总览"),
    ("/pick", "pick", "选股 · 勾选建仓"),
    ("/my", "my", "我的持仓"),
    ("/report", "report", "完整报告"),
    ("/macro", "macro", "宏观与政策"),
    ("/backtest", "backtest", "回测"),
    ("/data", "data", "数据新鲜度"),
    ("/rules", "rules", "规则与费率"),
]

# 每个视图由哪些 section 组成（键取自 touzi.report.dashboard_sections）
VIEW_SECTIONS: dict[str, list[str]] = {
    "report": ["macro", "my", "verdicts", "macro_bt", "port_bt", "stock_bt",
               "scores", "track", "policy", "freshness", "method"],
    "macro": ["macro", "verdicts", "macro_bt", "policy"],
    "backtest": ["port_bt", "stock_bt", "macro_bt"],
    "data": ["freshness", "method"],
}


SHELL_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0}
.topbar{position:sticky;top:0;z-index:900;display:flex;align-items:center;gap:4px;
 flex-wrap:wrap;padding:8px 16px;background:#0b1220cc;backdrop-filter:blur(8px);
 border-bottom:1px solid #1f2937;font:14px/1.4 "Microsoft YaHei",system-ui,sans-serif}
.topbar .brand{color:#fff;font-weight:700;text-decoration:none;margin-right:12px;
 white-space:nowrap;letter-spacing:.3px}
.topbar a.nv{color:#9ca3af;text-decoration:none;padding:6px 11px;border-radius:8px;
 white-space:nowrap}
.topbar a.nv:hover{background:#1f2937;color:#e5e7eb}
.topbar a.nv.on{background:#2563eb;color:#fff;font-weight:600}
.topbar .sp{flex:1 1 auto}
.topbar .live{color:#9ca3af;font-size:12px;white-space:nowrap}
.topbar .live b{color:#22c55e}
main.wrap{padding-top:16px}
.hero-links{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
.hero-links a{display:inline-block;padding:8px 14px;border-radius:10px;background:#111827;
 color:#e5e7eb;text-decoration:none;border:1px solid #1f2937;font-size:13px}
.hero-links a:hover{border-color:#2563eb;color:#fff}
.flash-up{animation:__fu 1.2s ease-out}
.flash-dn{animation:__fd 1.2s ease-out}
@keyframes __fu{0%{background:#065f46;color:#ecfdf5}100%{background:transparent}}
@keyframes __fd{0%{background:#7f1d1d;color:#fef2f2}100%{background:transparent}}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#6b7280;
 margin-right:6px;vertical-align:middle}
.dot.ok{background:#22c55e;box-shadow:0 0 6px #22c55e}
.dot.wait{background:#eab308}
.dot.bad{background:#ef4444;box-shadow:0 0 6px #ef4444}
.src{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
 border:1px solid #374151;color:#9ca3af;margin-left:4px}
.src.live{border-color:#166534;color:#86efac}
@media (prefers-reduced-motion:reduce){.flash-up,.flash-dn{animation:none!important}}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
h4{font-size:13.5px;margin:4px 0 8px;color:#9ca3af;font-weight:600}
@media (max-width:900px){.grid3{grid-template-columns:repeat(2,minmax(0,1fr))}}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.px{font-variant-numeric:tabular-nums}
.pickbar{position:sticky;bottom:0;z-index:800;display:flex;gap:10px;align-items:center;
 flex-wrap:wrap;padding:10px 14px;margin-top:12px;background:#0b1220ee;border:1px solid #1f2937;
 border-radius:12px;font:13px/1.5 "Microsoft YaHei",system-ui,sans-serif}
.pickbar input[type=date],.pickbar input[type=text]{background:#111827;color:#e5e7eb;
 border:1px solid #374151;border-radius:7px;padding:6px 9px;font:inherit}
button.b{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:8px 15px;
 cursor:pointer;font:inherit;font-weight:600}
button.b:hover{background:#1d4ed8}
button.b2{background:#374151;font-weight:400}
button.b2:hover{background:#4b5563}
button.b:disabled{opacity:.5;cursor:not-allowed}
tr.picked{background:#0f2a1d}
.hint{color:#9ca3af;font-size:12.5px}
.toast{position:fixed;left:50%;transform:translateX(-50%);bottom:70px;z-index:99998;
 background:#111827;color:#e5e7eb;border:1px solid #374151;border-radius:10px;
 padding:10px 18px;font:13px/1.6 "Microsoft YaHei",system-ui,sans-serif;
 box-shadow:0 8px 24px rgba(0,0,0,.5);display:none}
table input.ip,table input.is{width:78px;background:#111827;color:#e5e7eb;border:1px solid #374151;
 border-radius:6px;padding:4px 6px;font:12.5px/1.2 inherit;text-align:right}
@media print{.topbar,.pickbar,.toast{display:none}}
"""


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _md_inline(text: Any) -> str:
    """把 ``**加粗**`` 转成 <b>，其余转义。与 serve.py 的同名逻辑一致。"""
    s = "" if text is None else str(text)
    parts = s.split("**")
    out = []
    for i, seg in enumerate(parts):
        out.append(f"<b>{_esc(seg)}</b>" if i % 2 else _esc(seg))
    return "".join(out)


def _num(v: Any, nd: int = 2, dash: str = "—") -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dash
    if not pd.notna(f):
        return dash
    return f"{f:,.{nd}f}"


def _pct(v: Any, nd: int = 2, sign: bool = True, dash: str = "—") -> str:
    """百分比单元格。**None 时给破折号而不是「—%」**。

    注意服务端首屏文本必须和 `live_js` 里 `fmt()` 的产出长得一样，否则每 15 秒
    报价回来时数字会「跳一下」（少一个 % 号）。`sign=False` 时与 `fmt` 一致。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return dash
    if not pd.notna(f):
        return dash
    return f"{f:+,.{nd}f}%" if sign else f"{f:,.{nd}f}%"


def _qcell(value: Any, code: str, field: str, nd: int = 2, suffix: str = "",
           cls: str = "num") -> str:
    """一个「会被实时改写」的单元格。``data-q`` 是前端唯一的约定。"""
    return (f'<td class="{cls}" data-q="{_esc(code)}.{_esc(field)}" '
            f'data-nd="{nd}" data-suffix="{_esc(suffix)}">{_num(value, nd)}'
            f'{_esc(suffix)}</td>')


# --------------------------------------------------------------------------- #
# 外壳
# --------------------------------------------------------------------------- #
def topbar(active: str) -> str:
    links = []
    for href, key, label in NAV_ITEMS:
        on = " on" if key == active else ""
        links.append(f'<a class="nv{on}" href="{href}">{label}</a>')
    return (
        '<header class="topbar">'
        '<a class="brand" href="/">📈 A 股长期投资系统</a>'
        + "".join(links)
        + '<span class="sp"></span>'
        '<span class="live" id="__navlive">'
        '<span class="dot" id="__dot"></span>'
        '<span id="__livestate">连接中…</span>'
        "　·　报价 <b id=\"__livets\">—</b>"
        "　·　<span id=\"__buildstate\">—</span>"
        "</span>"
        "</header>"
    )


def live_js(interval: float | None = None) -> str:
    """15 秒一次问 ``/api/quotes``，把带 ``data-q`` 的单元格原地改写。

    数字变了就闪一下颜色（涨绿跌红在这个市场是反的，所以这里只表达「变了」，
    不用它表达涨跌——涨跌由 ``change_pct`` 自己带正负号表达）。
    """
    if interval is None:
        interval = LIVE_INTERVAL
    return """<script>
(function () {
  var IV = %(iv)d * 1000, STATUS_IV = %(siv)d * 1000, BUSY = false, LASTPV = null;
  function fmt(v, nd, suffix) {
    if (v === null || v === undefined || (typeof v === 'number' && isNaN(v))) { return null; }
    var s = Number(v).toLocaleString('zh-CN', {minimumFractionDigits: nd, maximumFractionDigits: nd});
    return s + (suffix || '');
  }
  function flash(el, up) {
    el.classList.remove('flash-up', 'flash-dn');
    void el.offsetWidth;
    el.classList.add(up ? 'flash-up' : 'flash-dn');
    setTimeout(function () { el.classList.remove('flash-up', 'flash-dn'); }, 1200);
  }
  function lookup(d, code, field) {
    var bag = (code === 'summary') ? d.summary
            : (code === 'meta') ? d.meta
            : (d.quotes && d.quotes[code] && (field in d.quotes[code])) ? d.quotes[code]
            : (d.holdings && d.holdings[code]) ? d.holdings[code] : null;
    if (!bag || !(field in bag)) { return undefined; }
    return bag[field];
  }
  function apply(d) {
    var cells = document.querySelectorAll('[data-q]');
    for (var i = 0; i < cells.length; i++) {
      var el = cells[i], spec = el.getAttribute('data-q').split('.');
      var v = lookup(d, spec[0], spec[1]);
      if (v === undefined) { continue; }
      if (el.getAttribute('data-kind') === 'text') {
        var t = (v === null || v === undefined) ? '—' : String(v);
        if (el.textContent !== t) { el.textContent = t; flash(el, true); }
        continue;
      }
      var nd = parseInt(el.getAttribute('data-nd') || '2', 10);
      var txt = fmt(v, nd, el.getAttribute('data-suffix') || '');
      if (txt === null) { continue; }
      var old = parseFloat((el.textContent || '').replace(/[,%%]/g, ''));
      if (el.textContent !== txt) { el.textContent = txt; if (!isNaN(old)) { flash(el, Number(v) >= old); } }
    }
    var ts = document.getElementById('__livets');
    if (ts && d.meta && d.meta.quote_time) { ts.textContent = d.meta.quote_time; }
    var st = document.getElementById('__livestate');
    var dot = document.getElementById('__dot');
    if (st && d.meta) {
      if (d.meta.error) { st.textContent = '报价失败'; }
      else if (!d.meta.count) { st.textContent = '无实时标的'; }
      else { st.textContent = '实时 ' + d.meta.count + ' 只'; }
      if (dot) { dot.className = 'dot ' + (d.meta.error ? 'bad' : (d.meta.count ? 'ok' : 'wait')); }
    }
  }
  var LASTBUILD = null;
  function busyEditing() {
    var a = document.activeElement;
    if (!a) { return false; }
    return a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.tagName === 'SELECT';
  }
  function checkBuild() {
    fetch('/api/status', {cache: 'no-store'}).then(function (r) { return r.json(); })
      .then(function (s) {
        var el = document.getElementById('__buildstate');
        if (el) {
          el.textContent = '第 ' + s.build_no + ' 版'
            + (s.building ? '（正在重算…）' : (s.elapsed ? '（' + s.elapsed + 's）' : ''))
            + (s.last_error ? '（上轮失败）' : '');
        }
        var dot = document.getElementById('__dot');
        if (dot && s.building) { dot.className = 'dot wait'; }
        if (LASTBUILD === null) { LASTBUILD = s.build_no; return; }
        /* 结论重算完成 → 整页刷新。正在输入时不打断，等下一次。 */
        if (s.build_no > LASTBUILD) {
          if (busyEditing()) { if (el) { el.textContent += '　← 已更新，填完即刷新'; } return; }
          LASTBUILD = s.build_no;
          location.reload();
        }
      }).catch(function () {})
      .then(function () { setTimeout(checkBuild, STATUS_IV); });
  }
  function tick() {
    if (BUSY || document.hidden) { setTimeout(tick, IV); return; }
    BUSY = true;
    fetch('/api/quotes', {cache: 'no-store'})
      .then(function (r) { return r.json(); })
      .then(apply)
      .catch(function () {
        var dot = document.getElementById('__dot');
        if (dot) { dot.className = 'dot bad'; }
      })
      .then(function () { BUSY = false; setTimeout(tick, IV); });
  }
  window.__toast = function (msg) {
    var t = document.getElementById('__toast');
    if (!t) { return; }
    t.textContent = msg; t.style.display = 'block';
    clearTimeout(t.__t); t.__t = setTimeout(function () { t.style.display = 'none'; }, 4000);
  };
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { tick(); } });
  tick();
  checkBuild();
})();
</script>
<div class="toast" id="__toast"></div>""" % {"iv": max(3, int(interval)), "siv": max(10, int(interval) * 4)}


def page(title: str, active: str, body: str, *, bar: str = "",
         interval: float | None = None, cfg=None, script: str | None = None) -> str:
    """把一段正文包进统一外壳。所有页面都走这里，才不会走样。

    ``script`` 用来换掉服务端版的实时脚本（``live_js``）：
      * ``None``（默认）＝照旧用 ``live_js``，本机网站的行为完全不变；
      * 一段 JS ＝ 换成它（静态站的导出器用这个接上 ``app.js``）；
      * ``""`` ＝ 不注入任何脚本。
    """
    if interval is None:
        interval = LIVE_INTERVAL
    tail = live_js(interval) if script is None else script
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)} · A 股长期投资系统</title>"
        f"<style>{R.CSS}{SHELL_CSS}</style></head><body>"
        f"{topbar(active)}"
        f'<main class="wrap">{body}</main>'
        f"{bar}"
        f"{tail}"
        "</body></html>"
    )


def hero(title: str, sub: str, links: list[tuple[str, str]] | None = None) -> str:
    b = [f"<h1>{_esc(title)}</h1>", f'<p class="sub">{sub}</p>']
    if links:
        b.append('<div class="hero-links">'
                 + "".join(f'<a href="{h}">{_esc(t)}</a>' for h, t in links)
                 + "</div>")
    return "".join(b)


def card(title: str, inner: str, note: str = "") -> str:
    n = f'<p class="small muted" style="margin:0 0 10px">{note}</p>' if note else ""
    return f'<div class="card"><h3>{_esc(title)}</h3>{n}{inner}</div>'


# --------------------------------------------------------------------------- #
# 各视图
# --------------------------------------------------------------------------- #
def sections_of(res: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从一次构建的结果里生成全部 section（网站各页从这里挑）。

    结果缓存在 ``res["_sections"]`` 里：一次构建只拼一次 HTML，同一批对象给所有页面用，
    既省 CPU 也保证 8 个页面看到的数字完全一致（不会出现两套口径）。
    """
    res = res or {}
    if res.get("_sections"):
        return res["_sections"]
    secs = R.dashboard_sections(
        res.get("cfg"),
        macro=res.get("macro"),
        scores=res.get("scores"),
        macro_bt=res.get("macro_bt"),
        port_bt=res.get("port_bt"),
        policy_events=res.get("policy_events"),
        industry_policy=res.get("industry_policy"),
        explain_map=res.get("explain_map"),
        freshness=res.get("freshness"),
        track=res.get("track"),
        stock_bt=res.get("stock_bt"),
        my_port=res.get("my_port"),
        flags=res.get("flags"),
    )
    try:
        res["_sections"] = secs
    except TypeError:
        pass
    return secs


def _pick_rows(res: dict[str, Any]) -> list[dict[str, Any]]:
    """勾选建仓的候选行：优先用跟踪表，没有跟踪表就退回打分表。"""
    rows: list[dict[str, Any]] = []
    track = res.get("track")
    if track is not None and len(track):
        for _, r in track.iterrows():
            act = str(r.get("建议动作") or "")
            rows.append({
                "code": str(r.get("代码") or ""),
                "name": str(r.get("名称") or ""),
                "action": act,
                "total": r.get("总分"),
                "price": r.get("现价"),
                "pe": r.get("PE_TTM"),
                "pb": r.get("PB"),
                "dy": r.get("股息率%"),
                "miss": str(r.get("未通过的门槛") or ""),
                "warn": str(r.get("预警") or ""),
            })
        return rows
    scores = res.get("scores")
    if scores is not None and len(scores):
        for _, r in scores.iterrows():
            rows.append({
                "code": str(r.get("代码") or ""),
                "name": str(r.get("名称") or ""),
                "action": "候选",
                "total": r.get("总分"),
                "price": r.get("现价"),
                "pe": r.get("PE(TTM)"),
                "pb": r.get("PB"),
                "dy": r.get("股息率%"),
                "miss": "",
                "warn": "",
            })
    return rows


_ACTION_RANK = {"建仓": 0, "接近建仓": 1, "加仓": 0, "持有": 2, "减仓": 3, "清仓": 4,
                "观望": 5, "回避": 6, "候选": 7}


def view_pick(res: dict[str, Any]) -> str:
    """选股 · 勾选建仓——用户要的那一页：系统把建议摆出来，你打勾、填股数，加进持仓。"""
    rows = _pick_rows(res)
    rows.sort(key=lambda x: (_ACTION_RANK.get(x["action"], 9), -(float(x["total"]) if pd.notna(x["total"]) else -1)))

    ready = [r for r in rows if r["action"] == "建仓"]
    near = [r for r in rows if r["action"] == "接近建仓"]
    watch = [r for r in rows if r["action"] == "观望"]

    body = [hero(
        "选股 · 勾选建仓",
        "下面每一行都是打分与买卖规则算出来的结果，<b>不是推荐你买</b>——"
        "「建仓」= 9 条买入门槛全通过；「接近建仓」= 只差一条。"
        "打勾、填股数（默认 100 股），点底部按钮就直接进你的持仓台账。",
        [("/my", "看我的持仓"), ("/report", "看完整报告"), ("/rules", "看规则与阈值")],
    )]

    def tbl(title: str, items: list[dict[str, Any]], tone: str, note: str) -> str:
        if not items:
            return card(title, '<p class="muted small">本次没有这一类标的。</p>', note)
        head = ("<tr><th>选</th><th>代码</th><th>名称</th><th>动作</th><th class='num'>总分</th>"
                "<th class='num'>现价</th><th class='num'>PE</th><th class='num'>PB</th>"
                "<th class='num'>股息率%</th><th>还差什么</th><th>买入价</th><th>股数</th></tr>")
        trs = []
        for r in items:
            pe_txt = _num(r["pe"], 2)
            pb_txt = _num(r["pb"], 2)
            extra = r["miss"] or ""
            if r["warn"]:
                extra = (extra + "　" if extra else "") + f'<span class="muted">{_esc(r["warn"])}</span>'
            price = r["price"]
            price_v = _num(price, 3)
            trs.append(
                f'<tr data-pick="{_esc(r["code"])}" data-name="{_esc(r["name"])}">'
                f'<td><input type="checkbox" onchange="__pickOne(this)"></td>'
                f'<td><code>{_esc(r["code"])}</code></td>'
                f'<td>{_esc(r["name"])}</td>'
                f'<td><span class="pill {tone}">{_esc(r["action"])}</span></td>'
                f'<td class="num">{_num(r["total"], 1)}</td>'
                f'<td class="px" data-q="{_esc(r["code"])}.price" data-nd="3">{price_v}</td>'
                f'<td class="num">{pe_txt}</td>'
                f'<td class="num">{pb_txt}</td>'
                f'<td class="num">{_num(r["dy"], 2)}</td>'
                f'<td class="small">{extra or "—"}</td>'
                f'<td><input class="ip" type="text" value="{_esc(price_v if price_v != "—" else "")}"></td>'
                f'<td><input class="is" type="text" value="100"></td>'
                "</tr>"
            )
        return card(title,
                    f'<div class="scroll"><table>{head}{"".join(trs)}</table></div>',
                    note)

    body.append(tbl("✅ 建仓：买入门槛全通过", ready, "great",
                    f"{len(ready)} 只。这些是规则说「可以买」的，最终买不买、买多少由你决定。"))
    body.append(tbl("🟡 接近建仓：只差一条门槛", near, "mid",
                    f"{len(near)} 只。差的那一条写在「还差什么」列——多数是估值还贵一点。"))
    body.append(tbl("⚪ 观望：目前不满足买入条件", watch, "weak",
                    f"{len(watch)} 只。列在这里是为了让你能一起勾选建仓（比如你已经持有、想补仓）。"))

    body.append("""
<div class="pickbar">
  <button class="b b2" type="button" onclick="__pickAll('ready')">全选「建仓」</button>
  <button class="b b2" type="button" onclick="__pickAll('near')">全选「接近建仓」</button>
  <button class="b b2" type="button" onclick="__pickAll('none')">清空勾选</button>
  <span class="sp" style="flex:1"></span>
  <label class="hint">买入日期 <input type="date" id="__pdate" value="__TODAY__"></label>
  <label class="hint">备注 <input type="text" id="__pnote" placeholder="可留空" size="12"></label>
  <button class="b" type="button" id="__padd" onclick="__pickAdd()">加入我的持仓</button>
</div>
<script>
function __pickAll(which) {
  var trs = document.querySelectorAll('tr[data-pick]');
  for (var i = 0; i < trs.length; i++) {
    var cb = trs[i].querySelector('input[type=checkbox]');
    var pill = trs[i].querySelector('.pill');
    var act = pill ? pill.textContent : '';
    var want = which === 'ready' ? (act === '建仓')
             : which === 'near' ? (act === '接近建仓') : false;
    cb.checked = want;
    trs[i].className = want ? 'picked' : '';
  }
}
function __pickOne(cb) {
  var tr = cb.closest('tr');
  tr.className = cb.checked ? 'picked' : '';
}
function __pickAdd() {
  var trs = document.querySelectorAll('tr[data-pick]');
  var date = document.getElementById('__pdate').value;
  var note = document.getElementById('__pnote').value;
  var jobs = [];
  for (var i = 0; i < trs.length; i++) {
    var cb = trs[i].querySelector('input[type=checkbox]');
    if (!cb || !cb.checked) { continue; }
    jobs.push({
      code: trs[i].getAttribute('data-pick'),
      name: trs[i].getAttribute('data-name'),
      price: (trs[i].querySelector('input.ip').value || '').trim(),
      shares: (trs[i].querySelector('input.is').value || '').trim()
    });
  }
  if (!jobs.length) { window.__toast('先勾选至少一只股票'); return; }
  var btn = document.getElementById('__padd');
  btn.disabled = true; btn.textContent = '正在写入 0/' + jobs.length + ' …';
  var done = 0, failed = [];
  function next() {
    if (!jobs.length) {
      btn.textContent = '已写入 ' + done + ' 笔，正在重算…';
      fetch('/api/refresh', {method: 'POST'}).catch(function () {});
      window.__toast('已写入 ' + done + ' 笔' + (failed.length ? ('，失败 ' + failed.length + ' 笔：' + failed.join('、')) : '') + '，正在重算，稍后自动刷新');
      setTimeout(function () { location.href = '/my'; }, 2500);
      return;
    }
    var j = jobs.shift();
    var fd = new URLSearchParams();
    fd.set('action', 'add'); fd.set('code', j.code); fd.set('name', j.name);
    fd.set('buy_date', date); fd.set('buy_price', j.price);
    fd.set('shares', j.shares); fd.set('note', note);
    fetch('/api/holdings', {method: 'POST', body: fd})
      .then(function (r) { return r.json().then(function (o) { return {ok: r.ok, o: o}; }); })
      .then(function (res) {
        if (res.ok) { done++; } else { failed.push(j.code); }
      })
      .catch(function () { failed.push(j.code); })
      .then(function () { btn.textContent = '正在写入 ' + done + '/' + (done + jobs.length + failed.length) + ' …'; next(); });
  }
  next();
}
</script>""".replace("__TODAY__", pd.Timestamp.now().strftime("%Y-%m-%d")))
    return "".join(body)


def view_my(res: dict[str, Any], cfg, state=None) -> str:
    """我的持仓：录入 + 台账 + **实时**盈亏（价格 15 秒自己刷新）。"""
    my_port = res.get("my_port")
    holdings = load_holdings(cfg)
    fees = load_fees(cfg)
    s = (my_port.attrs.get("summary", {}) if my_port is not None else {}) or {}

    body = [hero(
        "我的持仓",
        "录入之后系统持续跟踪：现价、市值、浮动盈亏 <b>每 15 秒自动刷新</b>（不等整页重算）；"
        "卖出提醒、建议卖出股数、扣掉手续费后的净盈利，由后台每轮重算按规则给出。",
        [("/pick", "去选股 · 勾选建仓"), ("/rules", "看卖出规则")],
    )]

    n_rows = int(len(my_port)) if my_port is not None else 0
    # 台账为空时 /api/quotes 根本不返回 summary.mv / summary.pnl（没有持仓就没有合计），
    # 这几格若还带 data-q 就成了**永远不会刷新的死格子**。check_report.py 的
    # data-q 交叉检查就是抓这个的——它当初正是靠这里两个死 data-q 暴露出来的。
    live = n_rows > 0
    n_px = int(s.get("有现价只数") or 0)
    wait = None if n_px else "待报价"   # 有台账但还没报价：说「待报价」，不说 ¥0.00

    def _money(key: str, nd: int = 2) -> str:
        if not live:
            return "—"
        return _num(s.get(key), nd) if n_px else (wait or "—")

    pnl_v = s.get("总浮动盈亏")
    pnl_cls = ""
    if live and pnl_v is not None and pd.notna(pnl_v):
        pnl_cls = " v-good" if float(pnl_v) >= 0 else " v-bad"
    kpi = [
        ("持仓只数", _num(s.get("持仓只数"), 0) if live else "0", "", ""),
        ("总成本（含费）", _money("总成本(含费)"), "", ""),
        ("总市值", _money("总市值"), "实时" if live else "", "summary.mv" if live else ""),
        ("浮动盈亏", _money("总浮动盈亏"), "", "summary.pnl" if live else ""),
        ("浮动盈亏%", _pct(s.get("总浮动盈亏%"), 2, sign=False) if (live and n_px) else "—", "",
         "summary.pnl_pct" if live else ""),
        ("需要动手", _num(s.get("需要动手"), 0) if live else "—",
         f'留意 {_num(s.get("留意"), 0)} 只' if live else "", ""),
    ]
    cards = []
    for label, value, note, qkey in kpi:
        attr = f' data-q="{qkey}" data-nd="2"' if qkey else ""
        if qkey and qkey.endswith("_pct"):
            attr += ' data-suffix="%"'
        cls = pnl_cls if qkey in ("summary.pnl", "summary.pnl_pct") else ""
        cards.append(f'<div class="card"><div class="kv"><b>{_esc(label)}</b>'
                     f'<span class="muted small">{_esc(note)}</span></div>'
                     f'<div class="big{cls}"{attr}>{value}</div></div>')
    body.append(f'<div class="grid grid3">{"".join(cards)}</div>')

    # ---- 录入表单 ----
    body.append(card(
        "① 录入 / 修改一笔持仓",
        """
<div class="grid grid2">
  <div>
    <p class="hint">同一只股票<b>分批买入就写多行</b>，系统按加权平均成本合并。
       「代码 + 买入日期 + 买入价」三者完全相同才算同一笔（会覆盖原行）。</p>
    <p class="hint">股数可以留空——那样只给买卖建议、不算金额。</p>
  </div>
  <div>
    <input type="hidden" id="f_idx" value="">
    <div class="kv"><span>代码</span><input id="f_code" placeholder="600519" size="10"></div>
    <div class="kv"><span>名称</span><input id="f_name" placeholder="贵州茅台" size="14"></div>
    <div class="kv"><span>买入日期</span><input id="f_date" type="date"></div>
    <div class="kv"><span>买入价</span><input id="f_price" placeholder="1450.00" size="10"></div>
    <div class="kv"><span>股数</span><input id="f_shares" placeholder="100" size="10"></div>
    <div class="kv"><span>备注</span><input id="f_note" placeholder="可留空" size="14"></div>
    <p style="margin-top:10px">
      <button class="b" type="button" onclick="__holdSave()">保存并重算</button>
      <button class="b b2" type="button" onclick="__holdClear()">清空</button>
      <button class="b b2" type="button" onclick="__refreshNow()">只重算，不改持仓</button>
    </p>
    <p class="hint" id="f_msg"></p>
  </div>
</div>""",
        "现价与盈亏是实时的；「建议动作」要靠后台重算，所以保存后台账会立刻显示、结论要等下一轮。",
    ))

    # ---- 台账 ----
    cols, rows = _holdings_table(cfg)
    trs = []
    for i, r in enumerate(rows):
        code = str(r.get("代码") or "")
        ok = bool(re.fullmatch(r"\d{6}", code))
        tds = "".join(f"<td>{_esc(r.get(c))}</td>" for c in cols)
        badge = "" if ok else ' <span class="pill bad">代码非法，会被跳过</span>'
        trs.append(
            f'<tr data-idx="{i}"><td>{i}</td><td><code>{_esc(code)}</code>{badge}</td>'
            + "".join(f"<td>{_esc(r.get(c))}</td>" for c in cols[1:])
            + "<td>"
            f'<button class="b b2" type="button" onclick="__holdEdit({i})">编辑</button> '
            f'<button class="b b2" type="button" onclick="__holdDel({i},\'{_esc(code)}\')">删除</button>'
            "</td></tr>"
        )
    head = ("<tr><th>#</th><th>代码</th>"
            + "".join(f"<th>{_esc(c)}</th>" for c in cols[1:])
            + "<th>操作</th></tr>")
    # 文件里的行数 ≠ 真正会被跟踪的持仓数：代码不是 6 位数字的行会被读取端跳过
    # （模板里那行「示例-这行会被跳过」就是干这个用的）。两个数都告诉用户，
    # 否则「共 1 行」会让人以为系统已经在跟踪一只股票了。
    n_ok = sum(1 for r in rows if re.fullmatch(r"\d{6}", str(r.get("代码") or "")))
    note = f"共 {len(rows)} 行，其中 {n_ok} 条会被跟踪。"
    if n_ok < len(rows):
        note += "代码不是 6 位数字的行会被跳过（模板里那行示例就是干这个用的）。"
    note += "网站读写的就是这个文件，你也可以直接用 Excel 改它。"
    body.append(card(
        "② 持仓台账（这就是 `config/holdings.csv`）",
        f'<div class="scroll"><table id="ledger">{head}'
        f'{"".join(trs) or "<tr><td colspan=8 class=muted>还没有任何持仓。</td></tr>"}</table></div>',
        note,
    ))

    # ---- 实时盈亏 ----
    body.append(_holdings_live_table(my_port))

    # ---- 手续费口径 ----
    body.append(card("④ 手续费口径（改 `config/settings.toml` 的 `[fees]`）", _fees_table(fees)))
    body.append(_HOLD_JS.replace("__TODAY__", pd.Timestamp.now().strftime("%Y-%m-%d")))
    return "".join(body)


_HOLD_JS = """
<script>
function __el(id) { return document.getElementById(id); }
function __holdClear() {
  ['f_idx','f_code','f_name','f_price','f_shares','f_note'].forEach(function (k) { __el(k).value = ''; });
  __el('f_date').value = '__TODAY__';
  __el('f_msg').textContent = '';
}
function __holdEdit(i) {
  var tr = document.querySelector('#ledger tr[data-idx="' + i + '"]');
  if (!tr) { return; }
  var tds = tr.querySelectorAll('td');
  __el('f_idx').value = String(i);
  __el('f_code').value = (tds[1].textContent || '').trim();
  var names = ['', '', 'f_name', 'f_date', 'f_price', 'f_shares', 'f_note'];
  for (var k = 2; k < 7 && k < tds.length; k++) {
    var el = __el(names[k]);
    if (el) { el.value = (tds[k].textContent || '').trim(); }
  }
  __el('f_msg').textContent = '已把第 ' + i + ' 行填进表单：改完点「保存并重算」。'
    + '键是「代码+买入日期+买入价」，三者都没改就覆盖原行；改了价格或日期则是新增一笔（分批买入）。';
  window.scrollTo({top: 0, behavior: 'smooth'});
}
function __holdSave() {
  var code = (__el('f_code').value || '').trim();
  if (!/^\\d{6}$/.test(code)) { __el('f_msg').textContent = '代码必须是 6 位数字。'; return; }
  var fd = new URLSearchParams();
  fd.set('action', 'add'); fd.set('code', code);
  fd.set('name', __el('f_name').value); fd.set('buy_date', __el('f_date').value);
  fd.set('buy_price', __el('f_price').value); fd.set('shares', __el('f_shares').value);
  fd.set('note', __el('f_note').value);
  __el('f_msg').textContent = '正在保存…';
  fetch('/api/holdings', {method: 'POST', body: fd})
    .then(function (r) { return r.json().then(function (o) { return {ok: r.ok, o: o}; }); })
    .then(function (res) {
      if (!res.ok) { __el('f_msg').textContent = '保存失败：' + (res.o.error || '未知错误'); return; }
      __el('f_msg').textContent = '已保存，共 ' + res.o.count + ' 行。正在重算…';
      fetch('/api/refresh', {method: 'POST'}).catch(function () {});
      window.__toast('已保存，正在重算（价格 15 秒内就会刷新，买卖结论要等整页重算）');
      setTimeout(function () { location.reload(); }, 2500);
    })
    .catch(function () { __el('f_msg').textContent = '保存失败：网络错误'; });
}
function __holdDel(i, code) {
  if (!confirm('确认删除 ' + code + ' 这一行？')) { return; }
  var fd = new URLSearchParams();
  fd.set('action', 'delete'); fd.set('index', String(i));
  fetch('/api/holdings', {method: 'POST', body: fd})
    .then(function (r) { return r.json(); })
    .then(function (o) {
      if (o.error) { window.__toast('删除失败：' + o.error); return; }
      fetch('/api/refresh', {method: 'POST'}).catch(function () {});
      location.reload();
    })
    .catch(function () { window.__toast('删除失败：网络错误'); });
}
function __refreshNow() {
  fetch('/api/refresh', {method: 'POST'})
    .then(function () { window.__toast('已触发重算，完成后页面自动刷新'); })
    .catch(function () { window.__toast('触发失败'); });
}
</script>
"""


def _holdings_table(cfg) -> tuple[list[str], list[dict[str, Any]]]:
    p = holdings_device(cfg)
    if not p.exists():
        return ["代码", "名称", "买入日期", "买入价", "股数", "备注"], []
    for enc in ("utf-8-sig", "gbk"):
        try:
            df = pd.read_csv(p, dtype=str, encoding=enc).fillna("")
            return list(df.columns), df.to_dict("records")
        except Exception:  # noqa: BLE001
            continue
    return ["代码", "名称", "买入日期", "买入价", "股数", "备注"], []


def _fees_table(fees: FeeModel) -> str:
    d = fees.as_dict()
    rows = "".join(f'<div class="kv"><span>{_esc(k)}</span><b>{_esc(v)}</b></div>'
                   for k, v in d.items())
    return ("<p class=\"small muted\">买入成本 = 成交金额 + 佣金（最低 5 元）+ 过户费；"
            "卖出到账 = 成交金额 − 佣金 − 印花税 − 过户费。</p>"
            "<p class=\"small muted\"><b>扣费后净盈利 = 卖出到账金额 − 该部分对应的「含费成本」</b>。"
            "用含费成本而不是成交金额，是因为买入时付的佣金也是你的成本；"
            "小额买入被最低 5 元佣金拖累是真实存在的情况，这里不抹掉。</p>"
            f"{rows}")


def _holdings_live_table(my_port: pd.DataFrame | None) -> str:
    if my_port is None or not len(my_port):
        return card("③ 现在该怎么办", '<p class="muted small">台账为空，所以没什么可跟踪的。'
                                       '先在上面录入，或去「选股 · 勾选建仓」打勾加入。</p>')
    head = ("<tr><th>代码</th><th>名称</th><th class='num'>买入价</th><th class='num'>股数</th>"
            "<th class='num'>含费成本</th><th class='num'>现价</th><th class='num'>市值</th>"
            "<th class='num'>浮动盈亏</th><th class='num'>浮动盈亏%</th><th>建议动作</th>"
            "<th class='num'>建议卖出%</th><th class='num'>建议卖出股数</th>"
            "<th class='num'>到账金额</th><th class='num'>扣费后净盈利</th><th>提醒</th></tr>")
    trs = []
    for _, r in my_port.iterrows():
        code = _esc(r.get("代码"))
        act = str(r.get("建议动作") or "")
        tone = {"清仓": "bad", "减仓": "weak", "持有": "good", "加仓": "great"}.get(act, "mid")
        pnl = r.get("浮动盈亏")
        pnl_cls = "v-good" if (pnl is not None and pd.notna(pnl) and float(pnl) >= 0) else "v-bad"
        net = r.get("扣费后净盈利")
        net_cls = "v-good" if (net is not None and pd.notna(net) and float(net) >= 0) else "v-bad"
        trs.append(
            f"<tr><td><code>{code}</code></td><td>{_esc(r.get('名称'))}</td>"
            f'<td class="num">{_num(r.get("买入价"), 3)}</td>'
            f'<td class="num">{_esc(r.get("股数") if r.get("股数") is not None else "未填")}</td>'
            f'<td class="num">{_num(r.get("含费成本"), 2)}</td>'
            + _qcell(r.get("现价"), str(r.get("代码")), "price", 3)
            + _qcell(r.get("市值"), str(r.get("代码")), "mv", 2)
            + _qcell(r.get("浮动盈亏"), str(r.get("代码")), "pnl", 2, cls=f"num {pnl_cls}")
            + _qcell(r.get("浮动盈亏%"), str(r.get("代码")), "pnl_pct", 2, suffix="%", cls=f"num {pnl_cls}")
            + f'<td><span class="pill {tone}">{_esc(act)}</span></td>'
            f'<td class="num">{_num(r.get("建议卖出%"), 1)}%</td>'
            f'<td class="num">{_num(r.get("建议卖出股数"), 0)}</td>'
            f'<td class="num">{_num(r.get("到账金额"), 2)}</td>'
            + _qcell(r.get("扣费后净盈利"), str(r.get("代码")), "net_profit", 2, cls=f"num {net_cls}")
            + f'<td class="small">{_md_inline(r.get("提醒"))}</td></tr>'
        )
    s = my_port.attrs.get("summary", {}) or {}
    foot = (f'<tfoot><tr><th colspan="6">合计（{_num(s.get("有现价只数"), 0)} 只有现价'
            f'{"，另有 " + _num(s.get("无现价只数"), 0) + " 只没现价已排除在合计外" if s.get("无现价只数") else ""}）</th>'
            f'<th class="num" data-q="summary.mv" data-nd="2">{_num(s.get("总市值"), 2)}</th>'
            f'<th class="num" data-q="summary.pnl" data-nd="2">{_num(s.get("总浮动盈亏"), 2)}</th>'
            f'<th class="num" data-q="summary.pnl_pct" data-nd="2" data-suffix="%">'
            f'{_pct(s.get("总浮动盈亏%"), 2, sign=False)}</th>'
            f'<th colspan="6"></th></tr></tfoot>')
    return card("③ 现在该怎么办（价格每 15 秒自己刷新）",
                f'<div class="scroll"><table>{head}{"".join(trs)}{foot}</table></div>',
                "「建议卖出股数」= 建议卖出比例 × 持仓股数，向下取整到 100 股（A 股卖出允许零股，"
                "不足一手的按实际股数）。")


def view_report(res: dict[str, Any]) -> str:
    secs = {s["key"]: s for s in sections_of(res)}
    keys = VIEW_SECTIONS["report"]
    toc = "".join(f'<a href="#sec-{k}">{_esc(secs[k]["title"])}</a>'
                  for k in keys if k in secs)
    body = [hero("完整报告", "下面就是那份单文件看板的全部内容，只是套进了统一站点里。",
                 [("/", "回到总览"), ("/data", "数据新鲜度")]),
            f'<div class="hero-links">{toc}</div>']
    for k in keys:
        s = secs.get(k)
        if s:
            body.append(f'<div id="sec-{k}">{s["html"]}</div>')
    return "".join(body)


def view_sections(res: dict[str, Any], which: str, title: str, sub: str,
                  links: list[tuple[str, str]] | None = None) -> str:
    secs = {s["key"]: s for s in sections_of(res)}
    body = [hero(title, sub, links)]
    for k in VIEW_SECTIONS[which]:
        s = secs.get(k)
        if s:
            body.append(f'<div id="sec-{k}">{s["html"]}</div>')
    if len(body) == 1:
        body.append(card("还没有数据", '<p class="muted small">先跑一次重算，或等后台自动完成。</p>'))
    return "".join(body)


def view_rules(res: dict[str, Any], cfg) -> str:
    plan = (cfg.get("plan", {}) or {}) if cfg is not None else {}
    fees = load_fees(cfg) if cfg is not None else FeeModel()
    body = [hero("规则与费率", "所有阈值都在 `config/settings.toml` 里，改完重跑，"
                              "全站的「建仓 / 减仓 / 清仓」立刻跟着变。",
                 [("/pick", "去勾选建仓"), ("/my", "我的持仓")])]
    body.append(R.render_plan_rules(plan))
    body.append(card("手续费口径（`[fees]`）", _fees_table(fees)))
    body.append(card("打分权重（`[stock_weights]` / `[macro_weights]`）", _weights_table(cfg)))
    return "".join(body)


def _weights_table(cfg) -> str:
    if cfg is None:
        return '<p class="muted small">—</p>'
    sw = cfg.get("stock_weights", {}) or {}
    mw = cfg.get("macro_weights", {}) or {}
    label = {"valuation": "估值（PE/PB/股息率分位）", "quality": "质量（ROE/现金流/负债）",
             "dividend": "分红（股息率/连续性）", "policy": "政策红利（行业底分+事件）",
             "pmi": "PMI", "ppi": "PPI", "fed": "美联储潮汐", "cpi": "CPI", "gdp": "GDP"}
    def rows(d: dict) -> str:
        return "".join(f'<div class="kv"><span>{_esc(label.get(k, k))}</span>'
                       f'<b>{float(v):.0%}</b></div>' for k, v in d.items())
    return (f'<div class="grid grid2"><div><h4>个股分权重</h4>{rows(sw)}</div>'
            f'<div><h4>宏观背景分权重</h4>{rows(mw)}</div></div>')


def _overview_live(my_port: pd.DataFrame | None) -> str:
    """总览顶部那条**实时**持仓 KPI：只有这一条会随 15 秒报价自己变。"""
    if my_port is None or not len(my_port):
        return card("我的持仓",
                    '<p class="muted small">台账为空——去「选股 · 勾选建仓」打勾，'
                    '或到「我的持仓」手工录入，之后这里会实时显示你的盈亏。</p>')
    s = my_port.attrs.get("summary", {}) or {}
    pnl = s.get("总浮动盈亏")
    known = pnl is not None and pd.notna(pnl)
    cls = ("v-good" if float(pnl) >= 0 else "v-bad") if known else ""
    n_px = int(s.get("有现价只数") or 0)
    # 刚在 /pick 勾选建仓、还没轮到报价时，_summarize 只对「有现价」的行求和，
    # 成本/市值都会是 0。写「¥0.00」会被读成「不赚不亏」——那是假话。
    wait = None if n_px else "待报价"
    cells = [
        ("持仓只数", _num(s.get("持仓只数"), 0), "", ""),
        ("总成本（含费）", _num(s.get("总成本(含费)"), 2) if n_px else "—", "", ""),
        ("总市值", wait or _num(s.get("总市值"), 2), "实时", "summary.mv"),
        ("浮动盈亏", wait or _num(s.get("总浮动盈亏"), 2), "实时", "summary.pnl"),
        ("浮动盈亏%", wait or _pct(s.get("总浮动盈亏%"), 2, sign=False),
         "实时", "summary.pnl_pct"),
        ("需要动手", _num(s.get("需要动手"), 0),
         f'留意 {_num(s.get("留意"), 0)} 只', ""),
    ]
    cards = []
    for label, value, note, qkey in cells:
        if qkey:
            attr = f' data-q="{qkey}" data-nd="2"'
            if qkey.endswith("_pct"):
                attr += ' data-suffix="%"'
        else:
            attr = ""
        c = cls if qkey in ("summary.pnl", "summary.pnl_pct") else ""
        cards.append(f'<div class="card"><div class="kv"><b>{_esc(label)}</b>'
                     f'<span class="muted small">{_esc(note)}</span></div>'
                     f'<div class="big{c}"{attr}>{value}</div></div>')
    return ('<h2>我的持仓（实时）</h2>'
            f'<div class="grid grid3">{"".join(cards)}</div>'
            '<p class="hint">这几格每 15 秒自己刷新（用的是腾讯秒级行情，不读缓存）；'
            '下面的「需要动手」要等后台整页重算，因为那取决于估值分位与打分。</p>')


def view_overview(res: dict[str, Any], state=None) -> str:
    macro = res.get("macro")
    my_port = res.get("my_port")
    track = res.get("track")
    body = [hero("总览", "一屏之内回答两件事：<b>现在是什么环境</b>，<b>我该做什么</b>。"
                         "顶部实时报价每 15 秒自己刷新。",
                 [("/pick", "选股 · 勾选建仓"), ("/my", "我的持仓"),
                  ("/report", "完整报告"), ("/data", "数据新鲜度")])]
    body.append(_overview_live(my_port))
    body.append(R.render_overview(macro=macro, my_port=my_port, track=track,
                                  freshness=res.get("freshness")))
    if state is not None:
        body.append(card("系统状态",
                         f'<div class="kv"><span>决策时点</span><b>{_esc(res.get("flags", {}).get("as_of") or "—")}</b></div>'
                         f'<div class="kv"><span>已打分股票</span><b>{_num(res.get("flags", {}).get("scored"), 0)}</b></div>'
                         f'<div class="kv"><span>实时报价</span><b id="__livestate">—</b></div>'
                         f'<div class="kv"><span>报价时间</span><b id="__livets">—</b></div>'
                         f'<div class="kv"><span>本轮重算用时</span><b>{_esc(state_elapsed(state))} 秒</b></div>'
                         f'<div class="kv"><span>自动重算间隔</span><b>{_esc(state_refresh(state))} 秒</b></div>',
                         "整页重算只负责结论；价格由秒级接口单独刷新，两者互不等待。"))
    return "".join(body)


def state_elapsed(state) -> str:
    e = getattr(state, "elapsed", None)
    return "—" if e is None else f"{e:.1f}"


def state_refresh(state) -> str:
    return str(int(getattr(state, "refresh", 0) or 0))


def view_data(res: dict[str, Any]) -> str:
    return view_sections(res, "data", "数据新鲜度",
                         "哪一块是实时的、哪一块**天然不可能**实时，都写在这里，不假装。",
                         [("/report", "完整报告"), ("/", "回到总览")])


def view_macro(res: dict[str, Any]) -> str:
    return view_sections(res, "macro", "宏观与政策",
                         "宏观背景分决定目标仓位；7 条判断条件逐条做了 2011-2026 的历史验证，"
                         "**证伪的也照原样列出**。",
                         [("/backtest", "回测"), ("/report", "完整报告")])


def view_backtest(res: dict[str, Any]) -> str:
    return view_sections(res, "backtest", "回测",
                         "两个回测：组合（回测B）与逐笔个股（回测C）。"
                         "**跑输买入持有的结论也照原样列出**。",
                         [("/macro", "宏观与政策"), ("/report", "完整报告")])


def error_page(msg: str, active: str = "") -> str:
    return page("出错", active,
                hero("出了点问题", _esc(msg),
                     [("/", "回到总览")])
                + card("怎么办", '<p class="small muted">'
                                '如果页面刚启动，可能第一轮重算还没跑完；状态条会显示进度。'
                                '持续失败请看 `output/serve.log` / `output/serve.err`。</p>'))
