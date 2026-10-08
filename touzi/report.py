"""报告与可视化看板：把宏观背景、个股打分、两个回测渲染成 Markdown 与单文件 HTML。

设计约束：
- 看板是**单文件、零外部依赖**（不引 CDN、不引 JS 库），离线可开——本机代理不稳，
  外链资源经常加载失败，所以全部 CSS 内联、图用内联 SVG 手绘。
- 报告里必须**原样保留三条「与用户直觉相反」的回测结论**，不许美化。用户提的 7 条
  判断条件里有 3 条在这份 2011-2026 的样本里站不住，藏着不说等于骗人。
"""

from __future__ import annotations

import html
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .regime import MACRO_LABELS

CN_NUMS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
           "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十"]


class _Counter:
    """章节号计数器——跳过某个回测时章节号不能断档。"""

    def __init__(self) -> None:
        self.i = 0

    def next(self) -> str:
        self.i += 1
        return CN_NUMS[self.i - 1] if self.i <= len(CN_NUMS) else str(self.i)


# --------------------------------------------------------------------------- #
# 用户 7 条判断条件 → 回测验证结论（数字从回测表里现取，保证与表格一致）
# --------------------------------------------------------------------------- #
USER_RULES: list[dict[str, Any]] = [
    {
        "no": 1,
        "claim": "看市盈率、市净率、股息率判断长期投资价值",
        "module": "估值 + 股息模块",
        "kind": "stock",
        "verdict": "已落地",
        "note": "PE/PB 用个股近 5 年历史分位＋行业/全市场截面分位；股息率用近 12 个月"
                "实际分红除以股价。三者在个股分里合计占 45%（估值）+ 15%（股息）。",
    },
    {
        "no": 2,
        "claim": "国家政策的红利（行业政策倾斜）",
        "module": "政策模块",
        "kind": "stock",
        "verdict": "已落地（含主观成分）",
        "note": "行业底分（31 个申万一级行业，人工打分）＋政策事件衰减加成。"
                "行业表是人工判断而非接口数据，是本系统唯一的主观输入，已在看板原样公开。",
    },
    {
        "no": 3,
        "claim": "美联储潮汐（全球流动性松紧）",
        "module": "美联储模块",
        "kind": "macro",
        "verdict": "短端成立",
        "pick": ("fed", "涨潮"),
        "counter": ("fed", "退潮"),
        "note": "退潮期 3 个月前瞻收益显著低于涨潮期，短端方向与说法一致；"
                "12 个月维度差异消失，说明它更像短周期择时信号而非长期信号。",
    },
    {
        "no": 4,
        "claim": "GDP、PMI 与股市正相关；PMI 大幅超 50 或处于上升阶段，股市大概率上涨",
        "module": "PMI + GDP 模块",
        "kind": "macro",
        "verdict": "部分成立、部分证伪",
        "pick": ("PMI", "≥51.5"),
        "counter": ("PMI", "<50"),
        "note": "PMI≥51.5 的 12 个月前瞻收益略高于全样本，方向对但幅度有限；"
                "反倒是 PMI<50（收缩）时 3 个月胜率高达 75.9%，比扩张期更好——"
                "这是「坏消息→政策宽松→估值修复」的逆势效应，与「PMI 高才涨」相反。"
                "GDP 高于近年均值时 12 个月收益反而低于均值以下，正相关在 A 股不成立。",
    },
    {
        "no": 5,
        "claim": "CPI 涨得快利好股市和债市，涨得慢不利于股市和债市",
        "module": "CPI 模块",
        "kind": "macro",
        "verdict": "被证伪（方向相反）",
        "pick": ("CPI", "涨得快"),
        "counter": ("CPI", "涨得慢"),
        "note": "样本内「涨得快」的 3 个月前瞻收益为负、胜率不足 40%；"
                "「涨得慢」反而是收益最好的一档。A 股对通胀的定价是「通胀上行→货币收紧→杀估值」，"
                "所以 CPI 加速是利空。代码仍按原规则保留该模块，但报告与看板都标注了证伪结论。",
    },
    {
        "no": 6,
        "claim": "PPI 下跌到一半，股市会领先上涨",
        "module": "PPI 模块",
        "kind": "macro",
        "verdict": "强烈成立",
        "pick": ("PPI", "半山腰"),
        "counter": None,
        "note": "PPI 回撤到周期一半（35%~65% 区间）时，12 个月前瞻平均收益远超全样本，"
                "胜率接近 70%。这是全部规则里区分度最强的一条，因此 PPI 在宏观权重里给到 0.25。",
    },
    {
        "no": 7,
        "claim": "PPI 到底部时，股市已经到顶峰",
        "module": "PPI 模块",
        "kind": "macro",
        "verdict": "成立",
        "pick": ("PPI", "底部区"),
        "counter": ("PPI", "高位区"),
        "note": "回撤≥85%（逼近周期底部）时前瞻收益转负；而 PPI 处于高位（回撤<35%）时"
                "12 个月收益为负、胜率仅 30%。两条合起来支持「PPI 见底≈股市见顶」的钟形判断，"
                "也是本系统给 PPI 用钟形曲线而非单调打分的原因。",
    },
]

FAILED_SOURCES = [
    "FRED（fred.stlouisfed.org）—— 本机网络不可达，美联储数据改用 Yahoo Finance",
    "东方财富 push2 系（push2.eastmoney.com 及各分片）—— 不可达，所有 *_em 接口弃用",
    "申万官网（www.swsresearch.com）—— SSL 失败，个股申万行业归属改用新浪行业板块",
    "百度估值接口只有市盈率/市净率/市现率，**没有股息率**，股息率由分红明细自行计算",
]

# 数据源自身的已知偏差（不是本系统能修的，必须原样告知）
DATA_CAVEATS = [
    "行业归属来自新浪板块，对更名/借壳重组的公司会停留在旧行业："
    "如 002555 三七互娱（原顺荣股份，做汽车部件）仍被归入『汽车制造』，"
    "002558 巨人网络（原世纪游轮）仍被归入『酒店旅游』。这会连带影响政策分与行业中性比较。",
    "新浪『经营现金净流量与净利润的比率(%)』这一列的取值本身就是倍数，不是百分数，"
    "代码直接当倍数使用（已用 5 只股票逐年手算核对过）。",
    "新浪财务表的 ROE / 净利率 / 增速是年内累计口径，跨期求均值无意义，"
    "本系统只对年报行（12 月）取均值。",
]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _f(v: Any, nd: int = 2, suffix: str = "") -> str:
    if v is None:
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(x):
        return "—"
    return f"{x:,.{nd}f}{suffix}"


def _score_class(s: Any) -> str:
    try:
        x = float(s)
    except (TypeError, ValueError):
        return "na"
    if not math.isfinite(x):
        return "na"
    if x >= 75:
        return "great"
    if x >= 60:
        return "good"
    if x >= 45:
        return "mid"
    if x >= 30:
        return "weak"
    return "bad"


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _as_frame(v: Any) -> pd.DataFrame:
    """回测 tables 的值既可能是 DataFrame，也可能是 JSON 往返后的 list[dict]。"""
    if isinstance(v, pd.DataFrame):
        return v
    if v is None:
        return pd.DataFrame()
    return pd.DataFrame(list(v))


def _pick(tables: dict[str, Any], rule: str, keyword: str) -> dict[str, Any] | None:
    """从回测表里按关键字取一行。

    `tables` 是按**分桶列**组织的（PMI水平 / PMI动量 / CPI速度 / PPI回撤 /
    GDP动能 / 美联储潮汐），不是按规则名，所以不能直接 ``tables[rule]``：
    要先扫出「规则列 == rule」的所有子表（同一规则可能拆成水平/动量两张），
    再在它们的『状态』列里找关键字。规则名大小写不敏感（"fed" vs "FED"）。
    """
    key = str(rule).upper()
    cands: list[pd.DataFrame] = []
    for k, v in (tables or {}).items():
        df = _as_frame(v)
        if len(df) == 0:
            continue
        if "规则" in df.columns:
            sub = df[df["规则"].astype(str).str.upper() == key]
            if len(sub):
                cands.append(sub)
        elif str(k).upper() == key:
            cands.append(df)
    for df in cands:
        col = "状态" if "状态" in df.columns else df.columns[0]
        hit = df[df[col].astype(str).str.contains(keyword, regex=False, na=False)]
        if len(hit):
            return hit.iloc[0].to_dict()
    return None


def _baseline(tables: dict[str, pd.DataFrame]) -> dict[str, Any] | None:
    """抽出「全样本基准」行，统一成 n / r3 / r6 / r12 / w3 / w6 / w12 的键。"""
    for v in tables.values():
        df = _as_frame(v)
        col = "状态" if "状态" in df.columns else None
        if col is None:
            continue
        hit = df[df[col].astype(str).str.contains("全样本基准", regex=False, na=False)]
        if len(hit):
            row = hit.iloc[0]
            return {
                "n": row.get("样本数"),
                "r3": row.get("3月均值%"), "r6": row.get("6月均值%"), "r12": row.get("12月均值%"),
                "w3": row.get("3月胜率%"), "w6": row.get("6月胜率%"), "w12": row.get("12月胜率%"),
            }
    return None


def user_rule_verdicts(macro_bt: dict[str, Any] | None) -> list[dict[str, Any]]:
    """给用户 7 条判断条件补上实测数字。"""
    out: list[dict[str, Any]] = []
    tables = (macro_bt or {}).get("tables") or {}
    base = _baseline(tables)
    for rule in USER_RULES:
        row = dict(rule)
        row["evidence"] = None
        row["counter_evidence"] = None
        if base is not None:
            row["baseline"] = base
        if rule.get("pick") and tables:
            row["evidence"] = _pick(tables, *rule["pick"])
        if rule.get("counter") and tables:
            row["counter_evidence"] = _pick(tables, *rule["counter"])
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# SVG 手绘净值曲线
# --------------------------------------------------------------------------- #
def equity_svg(periods: pd.DataFrame, width: int = 880, height: int = 300) -> str:
    """组合 vs 基准 净值曲线（内联 SVG）。"""
    if periods is None or len(periods) == 0:
        return '<p class="muted">无组合回测数据</p>'
    d = periods.copy()
    xcol = "调仓日" if "调仓日" in d.columns else d.columns[0]
    d[xcol] = d[xcol].astype(str)
    series = []
    if "组合净值" in d.columns:
        series.append(("组合", "组合净值", "#2f6fed"))
    if "基准净值" in d.columns:
        series.append(("沪深300", "基准净值", "#9aa4b2"))
    series = [s for s in series if d[s[1]].notna().any()]
    if not series:
        return '<p class="muted">无可绘制的净值序列</p>'

    vals = pd.concat([d[c] for _, c, _ in series]).dropna()
    lo, hi = float(vals.min()), float(vals.max())
    if hi - lo < 1e-9:
        hi, lo = hi + 0.5, lo - 0.5
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad

    ml, mr, mt, mb = 58, 18, 16, 34
    pw, ph = width - ml - mr, height - mt - mb
    n = max(len(d) - 1, 1)

    def X(i: int) -> float:
        return ml + pw * i / n

    def Y(v: float) -> float:
        return mt + ph * (1 - (v - lo) / (hi - lo))

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    # 网格与 y 轴刻度
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = Y(v)
        parts.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{width - mr}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{ml - 8}" y="{y + 4:.1f}" class="axt" text-anchor="end">{v:.2f}</text>')
    # 1.0 参考线
    if lo <= 1.0 <= hi:
        y = Y(1.0)
        parts.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{width - mr}" y2="{y:.1f}" class="base"/>')
    # 曲线
    for label, col, color in series:
        pts = " ".join(f"{X(i):.1f},{Y(float(v)):.1f}" for i, v in enumerate(d[col]) if pd.notna(v))
        if pts:
            parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.2"/>')
    # x 轴标签（最多 8 个）
    step = max(len(d) // 8, 1)
    for i in range(0, len(d), step):
        parts.append(f'<text x="{X(i):.1f}" y="{height - 12}" class="axt" text-anchor="middle">'
                     f'{_esc(d[xcol].iloc[i])}</text>')
    # 图例
    lx = ml + 6
    for label, col, color in series:
        last = d[col].dropna()
        txt = f"{label} {float(last.iloc[-1]):.3f}" if len(last) else label
        parts.append(f'<rect x="{lx}" y="{mt + 2}" width="11" height="11" fill="{color}" rx="2"/>')
        parts.append(f'<text x="{lx + 17}" y="{mt + 12}" class="lg">{_esc(txt)}</text>')
        lx += 24 + 9 * len(txt)
    parts.append("</svg>")
    return "".join(parts)


def bar_svg(rows: list[tuple[str, float, float]], width: int = 880, height: int = 240,
            unit: str = "%") -> str:
    """分组柱状图。rows = [(标签, 值1, 值2), ...]"""
    if not rows:
        return '<p class="muted">无数据</p>'
    vals = [v for _, a, b in rows for v in (a, b) if v is not None and math.isfinite(v)]
    if not vals:
        return '<p class="muted">无数据</p>'
    lo = min(0.0, min(vals))
    hi = max(0.0, max(vals))
    if hi - lo < 1e-9:
        hi = lo + 1
    ml, mr, mt, mb = 52, 16, 26, 52
    pw, ph = width - ml - mr, height - mt - mb
    n = len(rows)
    slot = pw / n
    bw = min(slot * 0.30, 34)
    zero = mt + ph * (hi / (hi - lo))

    def Y(v: float) -> float:
        return mt + ph * ((hi - v) / (hi - lo))

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    parts.append(f'<line x1="{ml}" y1="{zero:.1f}" x2="{width - mr}" y2="{zero:.1f}" class="base"/>')
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = Y(v)
        parts.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{width - mr}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{ml - 8}" y="{y + 4:.1f}" class="axt" text-anchor="end">{v:.1f}</text>')
    for i, (label, a, b) in enumerate(rows):
        cx = ml + slot * (i + 0.5)
        for j, (v, color) in enumerate(((a, "#2f6fed"), (b, "#9aa4b2"))):
            if v is None or not math.isfinite(v):
                continue
            x = cx - bw + j * (bw + 2)
            y = min(Y(v), zero)
            h = abs(Y(v) - zero)
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(h, 0.6):.1f}" '
                         f'fill="{color}" rx="2"/>')
            ty = y - 4 if v >= 0 else y + h + 12
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{ty:.1f}" class="axt" text-anchor="middle">'
                         f'{v:.1f}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{height - 30}" class="axt" text-anchor="middle">'
                     f'{_esc(label)}</text>')
    lx = ml + 4
    for label, color in (("组合", "#2f6fed"), ("沪深300", "#9aa4b2")):
        parts.append(f'<rect x="{lx}" y="{mt - 20}" width="11" height="11" fill="{color}" rx="2"/>')
        parts.append(f'<text x="{lx + 16}" y="{mt - 10}" class="lg">{label}</text>')
        lx += 84
    parts.append(f'<text x="{width - mr}" y="{mt - 10}" class="lg" text-anchor="end">单位：{unit}</text>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 样式
# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#f5f7fa;--card:#fff;--ink:#15202b;--muted:#6b7683;--line:#e3e8ef;
--blue:#2f6fed;--green:#12a150;--amber:#d99100;--red:#e5484d;--purple:#7c5cff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,"Segoe UI","Microsoft YaHei",Roboto,Helvetica,Arial,sans-serif;
font-size:14px;line-height:1.6}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:26px;margin:0 0 6px}
h2{font-size:19px;margin:38px 0 14px;padding-left:10px;border-left:4px solid var(--blue)}
h3{font-size:15px;margin:22px 0 10px;color:#2b3a4a}
.sub{color:var(--muted);margin:0 0 22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;
margin:14px 0;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.grid5{display:grid;grid-template-columns:repeat(auto-fit,minmax(196px,1fr));gap:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.kv{display:flex;justify-content:space-between;gap:10px;padding:3px 0;border-bottom:1px dashed var(--line)}
.kv:last-child{border-bottom:0}
.kv b{font-weight:600}
.muted{color:var(--muted)}
.small{font-size:12.5px}
table{width:100%;border-collapse:collapse;margin:10px 0;font-size:13px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child,th.l,td.l{text-align:left;white-space:normal}
th{background:#eef2f7;font-weight:600;color:#33414f;position:sticky;top:0}
tbody tr:hover{background:#f8fafc}
.scroll{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.pill{display:inline-block;min-width:46px;padding:2px 9px;border-radius:999px;font-weight:700;
font-size:12.5px;text-align:center;color:#fff}
.pill.great{background:var(--green)}.pill.good{background:#3f9e6f}
.pill.mid{background:var(--amber)}.pill.weak{background:#e07b39}
.pill.bad{background:var(--red)}.pill.na{background:#b6bfca}
.meter{height:8px;background:#e9eef5;border-radius:5px;overflow:hidden;margin-top:8px}
.meter>i{display:block;height:100%;border-radius:5px}
.v-great{background:var(--green)}.v-good{background:#3f9e6f}.v-mid{background:var(--amber)}
.v-weak{background:#e07b39}.v-bad{background:var(--red)}.v-na{background:#c3cbd5}
.big{font-size:30px;font-weight:700;letter-spacing:-.5px}
.tag{display:inline-block;padding:1px 8px;border-radius:6px;font-size:12px;font-weight:600}
.tag.ok{background:#e5f6ec;color:#0d7a3c}
.tag.warn{background:#fff4e0;color:#9a6600}
.tag.bad{background:#fdeaea;color:#b32227}
.tag.info{background:#e8f0fe;color:#1a56c4}
.chart{width:100%;height:auto;display:block}
.grid{stroke:#e9eef5;stroke-width:1}
.base{stroke:#c3cbd5;stroke-width:1.2;stroke-dasharray:4 4}
.chart polyline{stroke-linejoin:round;stroke-linecap:round}
.axt{font-size:10.5px;fill:#7b8794}
.lg{font-size:11.5px;fill:#4a5768;font-weight:600}
.svc{display:inline-block;padding:3px 10px;border:1px solid var(--line);border-radius:999px;
margin:3px 4px 3px 0;font-size:12.5px;background:#fff}
blockquote{margin:10px 0;padding:10px 14px;background:#fffaf0;border-left:4px solid var(--amber);
border-radius:0 8px 8px 0}
code{background:#eef2f7;padding:1px 5px;border-radius:4px;font-size:12.5px}
footer{color:var(--muted);font-size:12.5px;margin-top:40px;border-top:1px solid var(--line);
padding-top:14px}
"""


def _pill(score: Any) -> str:
    cls = _score_class(score)
    return f'<span class="pill {cls}">{_esc(_f(score, 1))}</span>'


def _meter(score: Any) -> str:
    cls = _score_class(score)
    try:
        w = max(0.0, min(100.0, float(score)))
    except (TypeError, ValueError):
        w = 0.0
    return f'<div class="meter"><i class="v-{cls}" style="width:{w:.0f}%"></i></div>'


# --------------------------------------------------------------------------- #
# 各区块渲染
# --------------------------------------------------------------------------- #
def render_macro(macro, no: str = "一") -> str:
    d = macro.as_dict()
    ladder_note = " → ".join(f"{p['score']:.0f}:{p['position'] * 100:.0f}%"
                            for p in d.get("ladder", [])) or "—"
    cards = []
    for key in ("pmi", "ppi", "fed", "cpi", "gdp"):
        res = d["rules"].get(key)
        if res is None:
            continue
        miss = key in (d.get("unavailable") or [])
        cards.append(
            '<div class="card">'
            f'<div class="kv"><b>{_esc(MACRO_LABELS.get(key, key))}</b>'
            f'<span class="muted small">权重 {d["weights"].get(key, 0):.2f}</span></div>'
            f'<div class="big">{_f(res.get("score"), 1)}'
            f'<span class="muted" style="font-size:14px;font-weight:400"> /100</span></div>'
            f'{_meter(res.get("score"))}'
            f'<div style="margin-top:8px"><span class="tag '
            f'{"warn" if miss else "info"}">{_esc(res.get("state"))}</span></div>'
            f'<p class="small muted" style="margin:8px 0 0">{_esc(res.get("explain"))}</p>'
            "</div>"
        )
    pos = d.get("position")
    score_txt = _f(d.get("macro_score"), 1)
    pos_txt = _f((pos or 0) * 100, 0, "%")
    stale_rows = "".join(
        f'<div class="kv"><span>{_esc(k)}</span><b>{_esc(v)}</b></div>'
        for k, v in (d.get("staleness") or {}).items()
    )
    unavail = "".join(f'<span class="svc">{_esc(u)}</span>' for u in (d.get("unavailable") or []))
    return f"""
<h2>{no}、宏观背景分与目标仓位</h2>
<div class="card">
  <div class="grid2">
    <div>
      <div class="muted small">宏观背景分（0~100）</div>
      <div class="big" style="color:{'var(--green)' if (d.get('macro_score') or 0) >= 60 else 'var(--ink)'}">{score_txt}</div>
      {_meter(d.get('macro_score'))}
      <div style="margin-top:10px"><span class="tag info">{_esc(d.get('regime'))}</span></div>
    </div>
    <div>
      <div class="muted small">对应目标仓位（该分数下的建议股票仓位）</div>
      <div class="big" style="color:var(--blue)">{pos_txt}</div>
      {_meter((pos or 0) * 100)}
      <p class="small muted" style="margin:8px 0 0">仓位阶梯：{_esc(ladder_note)}</p>
    </div>
  </div>
  <p class="small muted" style="margin:14px 0 4px">
    数据截止：<b>{_esc(d.get('as_of'))}</b>　口径：宏观数据已按公布滞后对齐（PMI 当月 0 个月、
    CPI/PPI/GDP 各滞后 1 个月），T 时刻的分数只使用 T 时刻已经能看到的数字。
  </p>
  <div class="grid2" style="margin-top:8px">
    <div>{stale_rows}</div>
    <div>{('<div class="small muted">缺失模块（已按可用项重新归一化权重，未按 0 分计）：</div>' + unavail) if unavail else '<div class="small muted">五个模块数据齐备。</div>'}</div>
  </div>
</div>
<div class="grid5">{''.join(cards)}</div>
"""


def render_rule_verdicts(verdicts: list[dict[str, Any]], base: dict[str, Any] | None,
                         no: str = "二") -> str:
    base_txt = ""
    if base:
        base_txt = (f'<p class="small muted">全样本基准（{_f(base.get("n"), 0)} 个月）：'
                    f'3 月 {_f(base.get("r3"))}%／6 月 {_f(base.get("r6"))}%／'
                    f'12 月 {_f(base.get("r12"))}%；胜率 {_f(base.get("w3"))}%／'
                    f'{_f(base.get("w6"))}%／{_f(base.get("w12"))}%</p>')
    rows = []
    for r in verdicts:
        ev = r.get("evidence")
        ce = r.get("counter_evidence")

        def cell(row: dict[str, Any] | None) -> str:
            if not row:
                return '<span class="muted">—</span>'
            return (f'{_esc(row.get("状态"))}<br><span class="muted small">n={_f(row.get("样本数"), 0)}'
                    f'　前瞻均值 3/6/12 月：{_f(row.get("3月均值%"))}% / {_f(row.get("6月均值%"))}% / '
                    f'{_f(row.get("12月均值%"))}%'
                    f'　胜率 {_f(row.get("3月胜率%"), 1)}% / {_f(row.get("6月胜率%"), 1)}% / '
                    f'{_f(row.get("12月胜率%"), 1)}%</span>')

        kind = "本题（长期投资选股）" if r.get("kind") == "stock" else "宏观择时规则"
        cls = ("ok" if "成立" in str(r.get("verdict")) and "部分" not in str(r.get("verdict"))
               and "已落地" not in str(r.get("verdict"))
               else "warn" if ("部分" in str(r.get("verdict")) or "已落地" in str(r.get("verdict")))
               else "bad")
        rows.append(f"""<tr>
<td class="l"><b>{r['no']}. {_esc(r['claim'])}</b><br>
  <span class="muted small">{_esc(r['module'])}　·　{_esc(kind)}</span></td>
<td class="l"><span class="tag {cls}">{_esc(r['verdict'])}</span></td>
<td class="l">{cell(ev)}</td>
<td class="l">{cell(ce) if r.get('counter') else '<span class="muted">—</span>'}</td>
<td class="l small">{_esc(r['note'])}</td>
</tr>""")
    return f"""
<h2>{no}、你提的 7 条判断条件：逐条落地 + 历史验证</h2>
<div class="card">
{base_txt}
<div class="scroll"><table>
<thead><tr><th class="l">你的说法</th><th class="l">结论</th><th class="l">对应状态实测</th>
<th class="l">对照状态实测</th><th class="l">说明</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p class="small muted">表格里的实测数字来自 2011-01 至 2026-09、共 189 个月度样本的
前瞻收益统计（回测A）。<b>「证伪」不是不实现</b>——代码仍按你的规则打分，但结论如实标注，
免得把一条在 A 股历史上方向相反的说法当成买入理由。</p>
</div>
"""


def render_macro_tables(macro_bt: dict[str, Any] | None, no: str = "三") -> str:
    if not macro_bt:
        return (f'<h2>{no}、宏观规则明细</h2>'
                '<div class="card"><p class="muted">未运行回测A。</p></div>')
    blocks = []
    for rule, df in (macro_bt.get("tables") or {}).items():
        cols = [c for c in ["状态", "样本数", "3月均值%", "3月胜率%", "6月均值%", "6月胜率%",
                            "12月均值%", "12月胜率%"] if c in df.columns]
        head = "".join(f'<th class="{"l" if c == "状态" else ""}">{_esc(c)}</th>' for c in cols)
        body = []
        for _, row in df.iterrows():
            is_base = "全样本基准" in str(row.get("状态", ""))
            tds = []
            for c in cols:
                v = row.get(c)
                if c == "状态":
                    tds.append(f'<td class="l">{"<b>" + _esc(v) + "</b>" if is_base else _esc(v)}</td>')
                else:
                    tds.append(f"<td>{_f(v, 2 if '均值' in c else 1)}</td>")
            style = ' style="background:#f7faff"' if is_base else ""
            body.append(f"<tr{style}>{''.join(tds)}</tr>")
        blocks.append(f'<h3>{_esc(rule)}</h3><div class="scroll" style="max-height:none"><table>'
                      f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>")
    summary = macro_bt.get("summary") or ""
    return f"""
<h2>{no}、宏观规则明细（回测A：这些说法在历史上成立吗）</h2>
<div class="card">
<p class="small muted">{_esc(summary)}</p>
<p class="small">做法：把 2011-01 起的每个自然月末当作一个决策点，只喂给它当时能看到的宏观数据，
用<b>与实盘完全相同</b>的打分函数判定状态，再看沪深300 之后 3／6／12 个月的实际涨跌。
这样「回测」和「实盘」不可能出现两套逻辑。</p>
{''.join(blocks)}
</div>
"""


def render_portfolio(port_bt: dict[str, Any] | None, no: str = "四") -> str:
    if not port_bt:
        return (f'<h2>{no}、个股组合回测</h2><div class="card"><p class="muted">'
                "未运行回测B。</p></div>")
    m = port_bt.get("metrics") or {}
    periods = port_bt.get("periods")
    rows = []
    if periods is not None and len(periods):
        cols = ["调仓日", "下期", "持仓数", "换手率%", "组合收益%", "沪深300%", "超额%",
                "组合净值", "基准净值"]
        cols = [c for c in cols if c in periods.columns]
        head = "".join(f'<th class="{"l" if c == "调仓日" else ""}">{_esc(c)}</th>' for c in cols)
        for _, r in periods.iterrows():
            tds = []
            for c in cols:
                v = r.get(c)
                if c in ("调仓日", "下期", "持仓数"):
                    tds.append(f'<td class="l">{_esc(v)}</td>' if c == "调仓日" else f"<td>{_esc(v)}</td>")
                else:
                    color = ""
                    if c in ("组合收益%", "沪深300%", "超额%") and v is not None and pd.notna(v):
                        color = ' style="color:var(--green)"' if float(v) > 0 else ' style="color:var(--red)"'
                    tds.append(f"<td{color}>{_f(v, 2 if c not in ('组合净值', '基准净值') else 3)}</td>")
            rows.append(f"<tr>{''.join(tds)}</tr>")
        table = (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table></div>')
        bars = bar_svg([
            (str(r.get("下期", ""))[-6:], r.get("组合收益%"), r.get("沪深300%"))
            for _, r in periods.iterrows()
        ])
        curve = equity_svg(periods)
    else:
        table, bars, curve = '<p class="muted">无逐期记录</p>', "", ""

    metrics = ""
    if m:
        port = m.get("组合") or {}
        bench = m.get("沪深300") or {}
        exc = m.get("超额") or {}
        items = [
            ("期数", _f(port.get("期数"), 0)),
            ("累计收益", _f(port.get("累计收益%"), 2, "%")),
            ("年化收益", _f(port.get("年化收益%"), 2, "%")),
            ("年化波动", _f(port.get("年化波动%"), 2, "%")),
            ("夏普", _f(port.get("夏普"))),
            ("最大回撤", _f(port.get("最大回撤%"), 2, "%")),
            ("胜率（正收益期数占比）", _f(port.get("胜率%"), 1, "%")),
            ("沪深300 累计收益", _f(bench.get("累计收益%"), 2, "%")),
            ("沪深300 年化收益", _f(bench.get("年化收益%"), 2, "%")),
            ("沪深300 最大回撤", _f(bench.get("最大回撤%"), 2, "%")),
            ("累计超额（终值差）", _f(exc.get("累计超额%"), 2, "%")),
            ("平均每期超额", _f(exc.get("平均每期超额%"), 2, "%")),
            ("跑赢期数占比", _f(exc.get("跑赢期数占比%"), 1, "%")),
        ]
        metrics = "".join(f'<div class="kv"><span class="muted">{k}</span><b>{v}</b></div>'
                          for k, v in items)
    warn = ""
    disc = (m.get("_disclaimer") if isinstance(m, dict) else None) or port_bt.get("disclaimer") or []
    if disc:
        warn = ('<p class="small muted" style="margin-top:10px">'
                + "　".join(f"· {_esc(x)}" for x in disc) + "</p>")
    return f"""
<h2>{no}、个股组合回测（回测B）</h2>
<div class="card">
  <div class="grid2">
    <div>{metrics or '<p class="muted">无指标</p>'}</div>
    <div>
      <p class="small muted">每期按总分取前 {_f(port_bt.get('top_n'), 0)} 名等权买入，
      单边成本 {_f((port_bt.get('cost_rate') or 0) * 100, 2)}%，对比沪深300。
      回测起点 {_esc(port_bt.get('start'))}。</p>
      {curve}
      {bars}
      {warn}
    </div>
  </div>
  {table}
</div>
"""


def render_stocks(scores: pd.DataFrame, top: int = 50,
                  explain_map: dict[str, str] | None = None, no: str = "五") -> str:
    if scores is None or len(scores) == 0:
        return (f'<h2>{no}、当前个股打分</h2><div class="card"><p class="muted">'
                "尚未运行打分。</p></div>")
    df = scores.head(top)
    cols = list(df.columns)
    head = "".join(f'<th class="{"l" if c in ("代码", "名称", "行业") else ""}">{_esc(c)}</th>'
                   for c in cols)
    rows = []
    for _, r in df.iterrows():
        tds = []
        for c in cols:
            v = r.get(c)
            if c == "总分":
                tds.append(f"<td>{_pill(v)}</td>")
            elif c == "评级":
                cls = {"A+": "great", "A": "good", "B": "mid", "C": "weak", "D": "bad"}.get(str(v), "na")
                tds.append(f'<td><span class="pill {cls}">{_esc(v)}</span></td>')
            elif c in ("代码", "名称", "行业"):
                tds.append(f'<td class="l">{_esc(v)}</td>')
            elif c.endswith("状态"):
                tds.append(f'<td><span class="muted small">{_esc(v)}</span></td>')
            elif "分" in c and c != "总分":
                tds.append(f"<td>{_f(v, 1)}</td>")
            elif isinstance(v, float):
                tds.append(f"<td>{_f(v, 3 if '股息率' in c else 2)}</td>")
            else:
                tds.append(f"<td>{_esc(v)}</td>")
        code = str(r.get("代码", ""))
        detail = (explain_map or {}).get(code, "")
        if detail:
            tds.append(f'<td class="l small"><details><summary>展开</summary>'
                       f'<pre style="white-space:pre-wrap;margin:6px 0">{_esc(detail)}</pre>'
                       "</details></td>")
        rows.append(f"<tr>{''.join(tds)}</tr>")
    extra = '<th class="l">逐项理由</th>' if explain_map else ""
    return f"""
<h2>{no}、当前个股打分（前 {min(top, len(df))} 名）</h2>
<div class="card">
<p class="small muted">总分 = 估值 45% + 质量 30% + 股息 15% + 政策 10%（缺失项在可用项间重新归一化）。
评级：A+ ≥80、A ≥72、B ≥62、C ≥50、其余 D。</p>
<div class="scroll"><table><thead><tr>{head}{extra}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</div>
"""


def render_policy(events: pd.DataFrame | None, ind: pd.DataFrame | None, no: str = "六") -> str:
    parts = [f'<h2>{no}、政策红利打分依据（主观成分，全量公开）</h2>', '<div class="card">',
             '<p class="small">政策模块 = 行业底分（上限 80）+ 政策事件加成（上限 20）。'
             '事件按半衰期 180 天指数衰减，且<b>只使用 as_of 之前的事件</b>。'
             '下表是人工判断，不是接口数据——本系统唯一的主观输入，请自行复核。</p>']
    if ind is not None and len(ind):
        rows = "".join(
            f'<tr><td class="l">{_esc(r.get("industry"))}</td><td>{_f(r.get("score"), 0)}</td>'
            f'<td class="l small">{_esc(r.get("note"))}</td></tr>'
            for _, r in ind.sort_values("score", ascending=False).iterrows()
        )
        parts.append('<h3>行业政策底分</h3><div class="scroll"><table><thead><tr>'
                     '<th class="l">行业</th><th>分数</th><th class="l">依据</th>'
                     f"</tr></thead><tbody>{rows}</tbody></table></div>")
    if events is not None and len(events):
        ev = events.copy()
        if "date" in ev.columns:
            ev = ev.sort_values("date", ascending=False)
        rows = "".join(
            f'<tr><td class="l">{_esc(r.get("date"))}</td><td class="l">{_esc(r.get("title"))}</td>'
            f'<td class="l">{_esc(r.get("industry"))}</td><td>{_f(r.get("impact"), 2)}</td>'
            f'<td class="l small">{_esc(r.get("note"))}</td></tr>'
            for _, r in ev.iterrows()
        )
        parts.append(f'<h3>政策事件（{len(ev)} 条）</h3><div class="scroll"><table><thead><tr>'
                     '<th class="l">日期</th><th class="l">事件</th><th class="l">行业</th>'
                     '<th>强度</th><th class="l">备注</th>'
                     f"</tr></thead><tbody>{rows}</tbody></table></div>")
    parts.append("</div>")
    return "".join(parts)


# 动作 → 徽章配色（跟踪表与动作统计共用）
_ACTION_CLASS = {
    "清仓": "bad", "减仓": "weak", "回避": "weak", "观望": "mid",
    "接近建仓": "good", "建仓": "great", "加仓": "great", "持有": "mid",
}
_ACTION_ORDER = ["清仓", "减仓", "加仓", "建仓", "接近建仓", "持有", "观望", "回避", "无法评估"]


def _plan_table(df: pd.DataFrame, cols: list[str], max_rows: int = 60) -> str:
    """跟踪表/逐笔表的 HTML 渲染（自动挑列、按动作上色）。"""
    cols = [c for c in cols if c in df.columns]
    if not cols:
        return '<p class="muted">无数据。</p>'
    head = "".join(
        f'<th class="{"l" if c in ("代码", "名称", "建议动作", "未通过的门槛", "触发的卖出条件", "卖出原因") else ""}">'
        f"{_esc(c)}</th>" for c in cols)
    rows = []
    for _, r in df.head(max_rows).iterrows():
        tds = []
        for c in cols:
            v = r.get(c)
            if c == "建议动作":
                cls = _ACTION_CLASS.get(str(v), "na")
                tds.append(f'<td class="l"><span class="pill {cls}">{_esc(v)}</span></td>')
            elif c == "总分":
                tds.append(f"<td>{_pill(v)}</td>")
            elif c in ("代码", "名称"):
                tds.append(f'<td class="l">{_esc(v)}</td>')
            elif c in ("未通过的门槛", "触发的卖出条件", "预警", "理由"):
                tds.append(f'<td class="l small">{_esc(v)}</td>')
            elif isinstance(v, float):
                tds.append(f"<td>{_f(v, 3 if '股息率' in c else 2)}</td>")
            else:
                tds.append(f"<td>{_esc(v)}</td>")
        rows.append(f"<tr>{''.join(tds)}</tr>")
    more = (f'<p class="small muted">共 {len(df)} 行，表中显示前 {max_rows} 行；'
            "完整清单见 <code>output/tracking.csv</code>。</p>") if len(df) > max_rows else ""
    return ('<div class="scroll"><table><thead><tr>' + head + "</tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>" + more)


# 持仓提醒级别 → 徽章配色
_LEVEL_CLASS = {"act": "bad", "watch": "mid", "hold": "good", "na": "na"}
_LEVEL_LABEL = {"act": "要动手", "watch": "留意", "hold": "持有", "na": "无法评估"}


def render_my_holdings(port: pd.DataFrame | None, no: str = "五") -> str:
    """「我的持仓」章节：录入的成本、现在的盈亏、该卖多少股、扣费后净赚多少。

    用户的原话是「输入对应的股数和单价，你在这个里面持续跟踪，到卖出时提醒，
    例如卖出持仓的百分之多少，最后扣除手续费盈利多少」——所以这张表的核心不是打分，
    而是**可直接照做的动作 + 一分不差的钱**。
    """
    if port is None or len(port) == 0:
        return (f"<h2>{_esc(no)}、我的持仓：录入 → 跟踪 → 卖出提醒</h2>"
                '<p class="muted">还没有录入持仓，所以这一节暂时是空的。'
                "<b>录入后这里会出现</b>：现价、市值、浮动盈亏、建议动作、"
                "<b>建议卖出多少股</b>，以及<b>扣掉手续费后净赚多少</b>。</p>"
                '<h3>怎么录入</h3>'
                '<ul class="small">'
                "<li>最简单的办法：打开网站后点左下角的 "
                "<a href=\"/portfolio\">✎ 我的持仓 / 录入与提醒</a>，"
                "填 <b>代码 / 买入日期 / 买入价 / 股数</b> 后点「保存并重算」；</li>"
                "<li>也可以直接编辑 <code>config/holdings.csv</code>，六列是 "
                "<code>代码,名称,买入日期,买入价,股数,备注</code>；</li>"
                "<li>同一只股票<b>分批买入就写多行</b>，系统按<b>加权平均成本</b>合并"
                "（键是「代码 + 买入日期 + 买入价」）；股数可以留空，那样只给买卖建议、不算金额。</li>"
                "</ul>"
                '<h3>手续费口径（「扣费后净盈利」的算法）</h3>'
                '<ul class="small">'
                "<li>佣金：买卖双向收取，<b>单笔最低 ¥5</b>；</li>"
                "<li>印花税：<b>仅卖出</b> 0.05%（2023-08-28 起由 0.1% 减半）；</li>"
                "<li>过户费：买卖双向 0.001%；</li>"
                "<li><b>扣费后净盈利</b> = 卖出到账金额 − 该部分对应的<b>含费成本</b>"
                "（买入佣金也在成本里，所以小额买入会被最低 5 元明显拖累——这是真实情况）。</li>"
                "</ul>"
                '<p class="small muted">费率写在 <code>config/settings.toml</code> 的 '
                "<code>[fees]</code> 段，请按你的券商对账单核对后改。</p>")

    s = port.attrs.get("summary", {}) or {}
    fees = s.get("fees", {}) or {}

    badges = [
        f'<span class="tag info">持仓 {s.get("持仓只数", 0)} 只</span>',
        f'<span class="tag">总成本(含费) ¥{_n(s.get("总成本(含费)"))}</span>',
        f'<span class="tag">总市值 ¥{_n(s.get("总市值"))}</span>',
        f'<span class="tag {"bad" if (s.get("总浮动盈亏") or 0) < 0 else "ok"}">'
        f'浮动盈亏 ¥{_n(s.get("总浮动盈亏"))}（{_n(s.get("总浮动盈亏%"))}%）</span>',
        f'<span class="tag {"bad" if s.get("需要动手") else "ok"}">需要动手 {s.get("需要动手", 0)} 只</span>',
        f'<span class="tag info">留意 {s.get("留意", 0)} 只</span>',
    ]
    if s.get("无现价只数"):
        badges.append(f'<span class="tag info">无现价 {s.get("无现价只数")} 只（不计入合计）</span>')

    # ---- 行动清单 ----
    act = port[port["_level"].isin(["act", "watch"])] if "_level" in port.columns else port.iloc[0:0]
    if len(act):
        act_cols = ["代码", "名称", "现价", "买入价", "股数", "浮动盈亏", "浮动盈亏%",
                    "建议动作", "建议卖出%", "建议卖出股数", "卖出金额", "卖出费用",
                    "到账金额", "扣费后净盈利", "扣费后净盈利%"]
        alert_block = ('<h3>📣 需要你处理的（按提醒级别排序）</h3>'
                       + _plan_table(act, act_cols)
                       + '<div class="scroll"><table><thead><tr><th class="l">代码</th>'
                         '<th class="l">提醒</th></tr></thead><tbody>'
                       + "".join(
                           f'<tr><td class="l">{_esc(r.get("代码"))}</td>'
                           f'<td class="l">{_md_inline(str(r.get("提醒") or ""))}</td></tr>'
                           for _, r in act.iterrows())
                       + "</tbody></table></div>")
    else:
        alert_block = '<h3>📣 需要你处理的</h3><p class="muted">当前没有任何持仓触发卖出条件。</p>'

    # ---- 全部持仓 ----
    all_cols = ["代码", "名称", "买入日期", "买入价", "股数", "含费成本", "含费成本价",
                "现价", "市值", "浮动盈亏", "浮动盈亏%", "建议动作",
                "建议卖出%", "建议卖出股数", "扣费后净盈利", "备注"]

    fee_note = (
        '<h3>手续费口径（这就是「扣费后净盈利」的算法）</h3>'
        '<ul class="small">'
        f'<li>佣金：<b>{fees.get("佣金费率", 0):.5f}</b>（万 {fees.get("佣金费率", 0) * 10000:.2f}），'
        f'<b>买卖双向</b>收取，单笔最低 <b>¥{fees.get("佣金最低", 5):g}</b>；</li>'
        f'<li>印花税：<b>{fees.get("印花税率", 0):.5f}</b>，<b>仅卖出</b>收取'
        "（2023-08-28 起由 0.1% 减半为 0.05%）；</li>"
        f'<li>过户费：<b>{fees.get("过户费率", 0):.5f}</b>，买卖双向收取；</li>'
        f'<li>「减仓」默认卖出 <b>{float(s.get("reduce_ratio", 0.5)):.0%}</b>'
        "（改 <code>[plan] reduce_ratio</code> 即可），建议股数按 100 股向下取整；</li>"
        "<li><b>扣费后净盈利</b> = 卖出到账金额 − 该部分对应的<b>含费成本</b>。"
        "买入时的佣金也被算进成本，所以小额买入（佣金被最低 5 元兜住）会明显拉低净盈利——"
        "这是真实情况，不是算错。</li>"
        "</ul>"
        '<p class="small muted">费率写在 <code>config/settings.toml</code> 的 <code>[fees]</code> 段。'
        "每家券商不同，请按你的对账单核对后改。</p>"
    )

    return (
        f"<h2>{_esc(no)}、我的持仓：录入 → 跟踪 → 卖出提醒</h2>"
        '<p class="small muted">这张表针对<b>你自己录入的持仓</b>，'
        "和下面按规则算出来的候选清单是两回事。改 <code>config/holdings.csv</code> "
        "（或在网站首页点「我的持仓」录入）后重跑，这里立刻跟着变。</p>"
        '<p class="small">' + " ".join(badges) + "</p>"
        + alert_block
        + '<h3>全部持仓</h3>' + _plan_table(port, all_cols)
        + fee_note
    )


def _n(v: Any) -> str:
    """金额/百分比格式化（千分位，None → —）。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return _esc(v)


def _md_inline(text: str) -> str:
    """把提醒里的 ``**加粗**`` 渲染成 HTML（其余内容照原样转义）。"""
    parts = _esc(text).split("**")
    out = []
    for i, p in enumerate(parts):
        out.append(f"<b>{p}</b>" if i % 2 else p)
    return "".join(out)


def render_plan_rules(plan: dict[str, Any] | None) -> str:
    """把 [plan] 段的买卖门槛原样列出来 —— 用户要求「根据投资策略列出来」。"""
    if not plan:
        return ""
    p = plan

    def g(k: str, d: Any = None) -> Any:
        v = p.get(k, d)
        return d if v is None else v

    def fnum(k: str, d: float = 0.0) -> str:
        try:
            return f"{float(g(k, d)):g}"
        except Exception:  # noqa: BLE001
            return str(g(k, d))

    buy_rows = [
        ("① 总分", f"≥ {fnum('buy_min_total')}", "综合评分够高"),
        ("② 质量分", f"≥ {fnum('buy_min_quality')}", "ROE / 现金流 / 负债不能太差"),
        ("③ PE 自身历史分位", f"≤ {fnum('buy_max_pe_percentile'):}",
         "比它自己过去 5 年 60% 的时间都便宜"),
        ("④ PB 自身历史分位", f"≤ {fnum('buy_max_pb_percentile')}", "净资产口径同样要便宜"),
        ("⑤ 股息率", f"≥ {fnum('buy_min_dividend_yield')}%", "有真金白银的分红"),
        ("⑥ PE 绝对值", f"≤ {fnum('buy_max_pe_abs')}", "再便宜也不碰畸高估值"),
        ("⑦ PE × PB（格雷厄姆数）", f"≤ {fnum('buy_max_graham')}",
         "A 股实测中位数约 62，故定为 50；教科书 22.5 只作「估值优秀」标记"),
        ("⑧ 宏观背景分", f"≥ {fnum('buy_min_macro_score')}", "大盘不能处于防御/保守"),
    ]
    rank_on = False
    try:
        rank_on = float(g("buy_max_rank_pct", 0.0) or 0.0) > 0.0
    except Exception:  # noqa: BLE001
        rank_on = False
    buy_rows.append(("⑨ 候选池内排名",
                     f"≤ {float(g('buy_max_rank_pct', 0.0)):.0%}" if rank_on else "默认关闭",
                     "该分位随候选池大小漂移，故默认不做硬门槛"))

    sell_rows = [
        ("清仓", "PE 自身历史分位", f"≥ {fnum('sell_pe_percentile')}"),
        ("清仓", "PB 自身历史分位", f"≥ {fnum('sell_pb_percentile')}"),
        ("清仓", "PE × PB", f"≥ {fnum('sell_graham')}"),
        ("清仓", "PE 绝对值", f"≥ {fnum('sell_pe_abs')}"),
        ("清仓", "股息率", f"≤ {fnum('sell_dividend_yield')}%"),
        ("清仓", "总分", f"≤ {fnum('sell_total')}"),
        ("清仓", "宏观背景分", f"≤ {fnum('sell_macro_score')}"),
        ("清仓", "现金流 / 净利润", f"≤ {fnum('sell_cashflow_floor')}"),
        ("清仓", f"净利润连降 ≥ {fnum('sell_negative_growth_years')} 个年报", "——"),
        ("清仓", "止损（相对成本）", f"≤ {float(g('sell_stop_loss', -0.25)):.0%}"),
        ("清仓", "止盈（相对成本）", f"≥ +{float(g('sell_take_profit', 1.5)):.0%}"),
        ("减仓", "总分", f"≤ {fnum('sell_reduce_total')}"),
        ("减仓", "宏观背景分", f"≤ {fnum('sell_macro_reduce_score')}"),
    ]

    buy_html = "".join(
        f'<tr><td class="l">{_esc(a)}</td><td class="l"><b>{_esc(b)}</b></td>'
        f'<td class="l small">{_esc(c)}</td></tr>' for a, b, c in buy_rows)
    sell_html = "".join(
        f'<tr><td>{_badge(lv)}</td><td class="l">{_esc(a)}</td>'
        f'<td class="l"><b>{_esc(b)}</b></td></tr>' for lv, a, b in sell_rows)

    return (
        '<h3>规则总表（只在 config/settings.toml 的 [plan] 段，可改）</h3>'
        '<h4>买入门槛：8 条 AND（另 1 条可选，默认关闭）—— 全通过才建仓</h4>'
        '<div class="scroll"><table><thead><tr><th class="l">条件</th>'
        '<th class="l">阈值</th><th class="l">为什么这么定</th></tr></thead>'
        f"<tbody>{buy_html}</tbody></table></div>"
        '<h4>卖出门槛：13 条 OR —— 任一触发就动手</h4>'
        '<div class="scroll"><table><thead><tr><th>级别</th><th class="l">条件</th>'
        '<th class="l">阈值</th></tr></thead>'
        f"<tbody>{sell_html}</tbody></table></div>"
        '<p class="small"><b>「减仓」只做一次</b>：同一轮建仓里「总分 ≤ 67 / 宏观 ≤ 40」'
        '只触发一次减半，之后要等「加仓」复位。原因写在回测里——原先它会月月触发，'
        '每次卖掉剩余的一半，仓位几何衰减到几乎归零，等于被反复割肉。'
        '差 1 条以内记为<b>「接近建仓」</b>，一并列出。</p>')


def _badge(level: str) -> str:
    cls = {"清仓": "bad", "减仓": "weak", "预警": "mid"}.get(level, "na")
    return f'<span class="pill {cls}">{_esc(level)}</span>'


def _plan_rules_md(plan: dict[str, Any] | None) -> str:
    """Markdown 版的买卖规则总表（与看板同源）。"""
    if not plan:
        return ""

    def g(k: str, d: Any = None) -> Any:
        v = plan.get(k, d)
        return d if v is None else v

    def fnum(k: str, d: float = 0.0) -> str:
        try:
            return f"{float(g(k, d)):g}"
        except Exception:  # noqa: BLE001
            return str(g(k, d))

    try:
        rank_on = float(g("buy_max_rank_pct", 0.0) or 0.0) > 0.0
    except Exception:  # noqa: BLE001
        rank_on = False

    L = ["**买入门槛（8 条 AND，全通过才建仓；差 ≤ "
         f"{int(g('buy_near_miss', 1))} 条记为「接近建仓」）**", "",
         "| 条件 | 阈值 |", "|---|---|",
         f"| 总分 | ≥ {fnum('buy_min_total')} |",
         f"| 质量分 | ≥ {fnum('buy_min_quality')} |",
         f"| PE 自身历史分位 | ≤ {fnum('buy_max_pe_percentile')} |",
         f"| PB 自身历史分位 | ≤ {fnum('buy_max_pb_percentile')} |",
         f"| 股息率 | ≥ {fnum('buy_min_dividend_yield')}% |",
         f"| PE 绝对值 | ≤ {fnum('buy_max_pe_abs')} |",
         f"| PE × PB（格雷厄姆数） | ≤ {fnum('buy_max_graham')}"
         "（A 股实测中位数约 62，故定为 50；教科书 22.5 仅作「估值优秀」标记） |",
         f"| 宏观背景分 | ≥ {fnum('buy_min_macro_score')} |",
         ("| 候选池内排名 | ≤ " + f"{float(g('buy_max_rank_pct', 0.0)):.0%}"
          + " |") if rank_on else "| 候选池内排名 | 默认关闭（该分位随候选池大小漂移） |",
         "",
         "**卖出门槛（13 条 OR，任一触发就动手）**", "",
         "| 级别 | 条件 | 阈值 |", "|---|---|---|",
         f"| 清仓 | PE 自身历史分位 | ≥ {fnum('sell_pe_percentile')} |",
         f"| 清仓 | PB 自身历史分位 | ≥ {fnum('sell_pb_percentile')} |",
         f"| 清仓 | PE × PB | ≥ {fnum('sell_graham')} |",
         f"| 清仓 | PE 绝对值 | ≥ {fnum('sell_pe_abs')} |",
         f"| 清仓 | 股息率 | ≤ {fnum('sell_dividend_yield')}% |",
         f"| 清仓 | 总分 | ≤ {fnum('sell_total')} |",
         f"| 清仓 | 宏观背景分 | ≤ {fnum('sell_macro_score')} |",
         f"| 清仓 | 现金流 / 净利润 | ≤ {fnum('sell_cashflow_floor')} |",
         f"| 清仓 | 净利润连降 | ≥ {fnum('sell_negative_growth_years')} 个年报 |",
         f"| 清仓 | 止损（相对成本） | ≤ {float(g('sell_stop_loss', -0.25)):.0%} |",
         f"| 清仓 | 止盈（相对成本） | ≥ +{float(g('sell_take_profit', 1.5)):.0%} |",
         f"| 减仓 | 总分 | ≤ {fnum('sell_reduce_total')} |",
         f"| 减仓 | 宏观背景分 | ≤ {fnum('sell_macro_reduce_score')} |",
         "",
         "**「减仓」只做一次**：同一轮建仓里「总分 ≤ 67 / 宏观 ≤ 40」只触发一次减半，"
         "之后要等「加仓」复位。原因写在回测里——原先它会月月触发，每次卖掉剩余的一半，"
         "仓位几何衰减到几乎归零，等于被反复割肉。"]
    return "\n".join(L)


def render_track(track: pd.DataFrame | None, no: str = "六",
                 plan: dict[str, Any] | None = None) -> str:
    """个股买卖决策与跟踪：直接回答「现在买什么、卖什么」。"""
    if track is None or len(track) == 0:
        return ""
    COLS = ["代码", "名称", "建议动作", "现价", "持仓成本", "浮动盈亏%",
            "PE_TTM", "PB", "股息率%", "PE历史分位", "PB历史分位", "PE×PB",
            "总分", "宏观背景分", "未通过的门槛", "触发的卖出条件", "预警"]
    counts = track["建议动作"].value_counts().to_dict()
    brief = "　".join(
        '<span class="pill {}">{} {}</span>'.format(
            _ACTION_CLASS.get(k, "na"), _esc(k), counts[k])
        for k in _ACTION_ORDER if k in counts)

    act = track[track["建议动作"].isin(["清仓", "减仓", "加仓", "建仓", "接近建仓"])]
    hold = track[track["建议动作"] == "持有"]

    parts = [
        f'<h2>{no}、个股买卖决策与跟踪</h2>',
        '<div class="card">',
        '<p class="small">这一节直接回答「<b>现在该买哪只、该卖哪只</b>」。'
        '打分是连续的，但决策是离散的——下面每只股票都被逐条核对了买入门槛与卖出条件，'
        '动作由 <code>touzi/plans.py</code> 按 <code>config/settings.toml</code> 的 '
        '<code>[plan]</code> 段算出。<b>所有阈值都在配置里，改完重跑，结论立刻跟着变。</b></p>',
        f'<p>{brief}</p>',
        render_plan_rules(plan),
    ]
    if len(act):
        parts.append("<h3>需要动手的（买入候选 / 减仓 / 清仓）</h3>")
        parts.append(_plan_table(act, COLS))
    else:
        parts.append('<p class="muted">本次没有「建仓 / 接近建仓 / 减仓 / 清仓」级别的标的——'
                     "门槛较严时这是正常结果，说明当前估值下没有明显便宜的好公司。</p>")
    if len(hold):
        parts.append(f"<h3>继续持有（{len(hold)} 只）</h3>")
        parts.append(_plan_table(hold, COLS, max_rows=30))
    parts.append("</div>")
    return "".join(parts)


def render_stock_trades(stock_bt: dict[str, Any] | None, no: str = "五") -> str:
    """回测C：个股逐笔买卖（对照「买入并持有」，检验卖出规则是否真的有用）。"""
    if not stock_bt:
        return ""
    trades = stock_bt.get("trades")
    metrics = stock_bt.get("metrics") or {}
    summary = metrics.get("汇总") or {}
    parts = [f"<h2>{no}、个股逐笔买卖回测（不组组合，一只一只算）</h2>", '<div class="card">',
             '<p class="small">按同一套买卖规则，在每个评估日对每只股票独立判断：'
             '买入门槛全通过就按<b>次日收盘价</b>建仓，触发卖出条件就卖。'
             '每笔都与「同期买入并持有」对比，用来判断<b>卖出规则到底有没有创造价值</b>。</p>',
             '<p class="small"><b>怎么读这张表（重要）</b>：候选池是按<b>今天的流通市值</b>排出来的，'
             '今天的大市值里已经包含新易盛、寒武纪、中际旭创这类 2023–2026 年涨了十几倍的 AI 行情赢家，'
             '当年无法预知 —— 所以「平均买入持有%」被这些极值拉高，是<b>带后见之明的基准</b>，'
             '不能当成「本该赚到的钱」。请结合三个数一起读：'
             '①「买入持有中位数%」看这池子的<b>中位</b>表现；'
             '②「有交易的股票数 / 有交易的股票平均策略收益% / 有交易的股票平均买入持有%」'
             '是<b>同口径</b>对比（只在真正买过的股票上比）；'
             '③「平均策略收益%」对从未触发买入的股票记 0，与买入持有<b>不同口径</b>，'
             '它回答的是「这套门槛三年里到底敢不敢下手」。</p>',
             '<p class="small"><b>卖出规则的实测线索</b>：看下面「按卖出原因分布」——'
             '按<b>个股估值</b>卖的（PE/PB 分位 ≥ 80% 清仓）平均收益最高，'
             '而按<b>宏观背景分</b>减仓的这笔数最多、平均收益最低。'
             '这说明卖出条件应该以个股自身估值为主要依据，'
             '少用大盘背景分去动个股仓位 —— 否则容易在上涨途中被反复削峰。</p>']
    if summary:
        kv = "".join(f'<div class="kv"><span>{_esc(k)}</span><b>{_esc(v)}</b></div>'
                     for k, v in summary.items() if not isinstance(v, (dict, list)))
        parts.append(f'<h3>汇总</h3><div class="grid2"><div>{kv}</div><div></div></div>')
        by_reason = summary.get("按卖出原因")
        if isinstance(by_reason, dict) and by_reason:
            rows = "".join(
                f'<tr><td class="l">{_esc(k)}</td><td>{_esc(v)}</td></tr>'
                for k, v in by_reason.items())
            parts.append('<h3>按卖出原因分布</h3><div class="scroll"><table><thead><tr>'
                         '<th class="l">卖出原因</th><th>笔数 / 统计</th>'
                         f"</tr></thead><tbody>{rows}</tbody></table></div>")
    if trades is not None and len(trades):
        parts.append(f"<h3>逐笔明细（共 {len(trades)} 笔，显示前 60 笔）</h3>")
        parts.append(_plan_table(trades, list(trades.columns), max_rows=60))
    discs = metrics.get("_disclaimer") or []
    if discs:
        parts.append('<ul class="small muted">'
                     + "".join(f"<li>{_esc(d)}</li>" for d in discs) + "</ul>")
    parts.append("</div>")
    return "".join(parts)


def _auto_refresh(cfg) -> str:
    """[realtime] dashboard_refresh_seconds > 0 时让浏览器自动重载看板。

    配合 `run_all.py --live --watch N` 使用：脚本原地覆盖 dashboard.html，
    浏览器每隔 N 秒自动拉一次，就得到一个不用手动刷新的盘中看板。
    """
    try:
        secs = int((cfg.get_path("realtime", {}) or {}).get("dashboard_refresh_seconds", 0) or 0)
    except Exception:  # noqa: BLE001
        return ""
    return f'<meta http-equiv="refresh" content="{secs}">' if secs > 0 else ""


def render_freshness(rows: list[dict[str, Any]] | None, no: str = "七") -> str:
    """数据新鲜度面板：讲清楚哪一块是实时的、哪一块天然滞后。"""
    if not rows:
        return ""
    trs = []
    for r in rows:
        latest = _esc(r.get("latest", "-"))
        trs.append(
            f'<tr><td class="l">{_esc(r.get("item", ""))}</td>'
            f'<td class="l"><b>{latest}</b></td>'
            f'<td class="l">{_esc(r.get("fetched", "-"))}</td>'
            f'<td class="l">{_esc(r.get("cycle", "-"))}</td>'
            f'<td class="l">{_esc(r.get("note", ""))}</td></tr>'
        )
    return (
        f'<h2>{no}、数据新鲜度（哪一块实时、哪一块天然滞后）</h2>'
        '<div class="card"><p class="muted">下表逐项说明每类数据的<strong>最新时点</strong>、'
        '<strong>本地缓存的抓取时刻</strong>与<strong>刷新周期</strong>。'
        '行情类数据在实时模式下随每次运行刷新；宏观类数据受官方公布节奏限制，'
        '任何系统都无法让它变得更“实时”——这不是本系统的缺陷，而是数据的固有属性。</p>'
        '<div class="scroll"><table><thead><tr>'
        '<th class="l">数据项</th><th class="l">数据最新到</th><th class="l">缓存抓取时刻</th>'
        '<th class="l">刷新周期</th><th class="l">说明</th>'
        f"</tr></thead><tbody>{''.join(trs)}</tbody></table></div></div>"
    )


def render_method(cfg, unavailable: list[str], flags: dict[str, Any] | None = None,
                  no: str = "七") -> str:
    w = cfg.get_path("macro_weights", {}) or {}
    sw = cfg.get_path("stock_weights", {}) or {}
    srcs = "".join(f'<span class="svc">{_esc(s)}</span>' for s in FAILED_SOURCES)
    caveats = "".join(f"<li>{_esc(c)}</li>" for c in DATA_CAVEATS)
    return f"""
<h2>{no}、方法与口径</h2>
<div class="card">
  <div class="grid2">
    <div>
      <h3 style="margin-top:0">宏观背景分权重</h3>
      <div class="kv"><span>PMI 制造业景气</span><b>{w.get('pmi', 0):.2f}</b></div>
      <div class="kv"><span>PPI 工业品价格周期</span><b>{w.get('ppi', 0):.2f}</b></div>
      <div class="kv"><span>美联储潮汐</span><b>{w.get('fed', 0):.2f}</b></div>
      <div class="kv"><span>CPI 通胀速度</span><b>{w.get('cpi', 0):.2f}</b></div>
      <div class="kv"><span>GDP 增长</span><b>{w.get('gdp', 0):.2f}</b></div>
    </div>
    <div>
      <h3 style="margin-top:0">个股分权重</h3>
      <div class="kv"><span>估值（PE/PB/股息率）</span><b>{sw.get('valuation', 0):.2f}</b></div>
      <div class="kv"><span>质量（ROE/现金流/负债/稳定性）</span><b>{sw.get('quality', 0):.2f}</b></div>
      <div class="kv"><span>股息（水平/连续性/利差）</span><b>{sw.get('dividend', 0):.2f}</b></div>
      <div class="kv"><span>政策红利</span><b>{sw.get('policy', 0):.2f}</b></div>
    </div>
  </div>
  <h3>防未来函数</h3>
  <ul class="small">
    <li>所有分数只使用决策时点<b>已经公布</b>的数据；宏观序列按公布滞后整体后移（PMI 0 月、CPI/PPI/GDP 各 1 月）。</li>
    <li>个股历史分位只用决策时点之前的历史；组合回测按 <code>point_in_time=True</code> 取当日 PE/PB/股价，并<b>禁用截面分位</b>。</li>
    <li>政策事件按日期过滤，未来事件不参与打分。</li>
  </ul>
  <h3>必须知道的三个偏差</h3>
  <ul class="small">
    <li><b>幸存者偏差</b>：以当前在市股票回溯历史，已退市/被并购的股票不在样本里，会高估收益。</li>
    <li><b>估值历史深度</b>：百度估值接口只提供近 5 年数据，更早的季度无法算个股历史分位，早年样本偏少。</li>
    <li><b>截面分位缺失</b>：point-in-time 模式下没有历史全市场快照，因此关闭了「行业内/全市场估值分位」，只用个股自身历史分位。</li>
  </ul>
  <h3>数据源与本机网络限制</h3>
  <p class="small muted">本机（2026-10）实测不可达、已绕开的源：</p>
  <p>{srcs}</p>
  <h3>数据源自身的已知偏差</h3>
  <ul class="small">{caveats}</ul>
  {f'<h3>本次运行的数据标记</h3><pre class="small" style="white-space:pre-wrap">{_esc(json.dumps(flags, ensure_ascii=False, indent=2))}</pre>' if flags else ''}
</div>
"""


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def _split_title(html: str) -> str:
    """从 ``<h2>三、xxx</h2>`` 里取出「xxx」（去掉章节编号）。"""
    m = re.match(r"\s*<h2>(.*?)</h2>", html or "", re.S)
    if not m:
        return ""
    raw = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return re.sub(r"^[一二三四五六七八九十]+、\s*", "", raw)


def dashboard_sections(
    cfg,
    macro=None,
    scores: pd.DataFrame | None = None,
    macro_bt: dict[str, Any] | None = None,
    port_bt: dict[str, Any] | None = None,
    policy_events: pd.DataFrame | None = None,
    industry_policy: pd.DataFrame | None = None,
    explain_map: dict[str, str] | None = None,
    freshness: list[dict[str, Any]] | None = None,
    track: pd.DataFrame | None = None,
    stock_bt: dict[str, Any] | None = None,
    my_port: pd.DataFrame | None = None,
    flags: dict[str, Any] | None = None,
    top_display: int | None = None,
) -> list[dict[str, Any]]:
    """把看板拆成 ``[{"key","title","html"}]``。

    存在的意义只有一个：**网站各个页面不是另写一套文案，而是同一批 section 的重新排列**。
    这样「总览 / 我的持仓 / 回测 / 宏观 / 数据 / 规则」看见的数字、口径、措辞与那份
    完整报告逐字一致，改一处就全站跟着改。章节编号在这里用同一个 ``_Counter`` 一次性
    发完，所以不管网站挑哪几节展示，标题里的编号都和完整报告对得上。
    """
    if top_display is None:
        top_display = int((cfg.get_path("report", {}) or {}).get("top_display", 50) or 50)
    unavailable = list(getattr(macro, "unavailable", []) or [])
    c = _Counter()
    out: list[dict[str, Any]] = []

    def add(key: str, html: str) -> None:
        out.append({"key": key, "title": _split_title(html), "html": html})

    if macro is not None:
        add("macro", render_macro(macro, c.next()))
    else:
        add("macro", f'<h2>{c.next()}、宏观背景</h2>'
                     '<div class="card"><p class="muted">未提供宏观结果。</p></div>')
    # 「我的持仓」紧跟宏观——用户最想看的就是「我手上这笔现在该做什么」。
    # 即使台账为空也要渲染（给空态提示 + 告诉用户去哪录入），
    # 否则章节号会随「有没有持仓」漂移，用户也发现不了这个功能。
    if my_port is not None:
        add("my", render_my_holdings(my_port, c.next()))
    add("verdicts", render_rule_verdicts(user_rule_verdicts(macro_bt),
                                         _baseline((macro_bt or {}).get("tables") or {}),
                                         c.next()))
    add("macro_bt", render_macro_tables(macro_bt, c.next()))
    add("port_bt", render_portfolio(port_bt, c.next()))
    # 只有真的渲染出内容才消耗章节号，否则会出现「四、… 六、…」这种断号
    if stock_bt:
        add("stock_bt", render_stock_trades(stock_bt, c.next()))
    add("scores", render_stocks(scores if scores is not None else pd.DataFrame(),
                                top_display, explain_map, c.next()))
    if track is not None and len(track):
        add("track", render_track(track, c.next(),
                                  cfg.get("plan", {}) if cfg is not None else None))
    add("policy", render_policy(policy_events, industry_policy, c.next()))
    if freshness:
        add("freshness", render_freshness(freshness, c.next()))
    add("method", render_method(cfg, unavailable, flags, c.next()))
    return out


def render_overview(
    macro=None,
    my_port: pd.DataFrame | None = None,
    track: pd.DataFrame | None = None,
    freshness: list[dict[str, Any]] | None = None,
) -> str:
    """首页「总览」：一屏之内回答「现在是什么环境、我该做什么」。

    这一节**不参与报告章节编号**（它只存在于网站上），所有数字都来自别处已经算好的
    结果，不重新计算、不引入第二套口径。
    """
    d = macro.as_dict() if macro is not None else {}
    act_txt = watch_txt = "—"
    ready_n = near_n = watch_n = 0
    top_rows: list[tuple[str, str, float, str]] = []
    if track is not None and len(track):
        for _, r in track.iterrows():
            a = str(r.get("建议动作") or "")
            if a == "建仓":
                ready_n += 1
            elif a == "接近建仓":
                near_n += 1
            elif a == "观望":
                watch_n += 1
            if a in ("建仓", "接近建仓"):
                try:
                    top_rows.append((str(r.get("代码")), str(r.get("名称")),
                                     float(r.get("总分")), a))
                except Exception:  # noqa: BLE001
                    pass
        top_rows.sort(key=lambda x: -x[2])

    pnl_txt = cost_txt = mv_txt = "还没录入持仓"
    pnl_cls = "muted"
    act_list: list[str] = []
    if my_port is not None and len(my_port):
        s = my_port.attrs.get("summary", {}) or {}
        act_txt = str(s.get("需要动手", 0))
        watch_txt = str(s.get("留意", 0))
        n_all = int(s.get("持仓只数") or 0)
        n_px = int(s.get("有现价只数") or 0)
        if n_px:
            cost_txt = _n(s.get("总成本(含费)"))
            mv_txt = _n(s.get("总市值"))
        else:
            # 一行都没有现价时，_summarize 只对「有现价」的行求和，成本/市值都是 0。
            # 显示「¥0.00 / 亏 0.00」会被读成「不赚不亏」，那是假话——它只是还没报价。
            cost_txt = mv_txt = "—"
        pnl = s.get("总浮动盈亏")
        if not n_px:
            pnl_txt = f"待报价（{n_all} 只尚未拿到现价）"
            pnl_cls = "muted"
        elif pnl is not None and pd.notna(pnl):
            # 注意：不能写 s.get("总浮动盈亏%", 0)——「键存在但值为 None」时
            # 默认值不会生效，f"{None:+.2f}" 会抛
            # TypeError: unsupported format string passed to NoneType.__format__，
            # 整页 500。台账里只有「股数/买入价没填全」或全部无现价的持仓时，
            # _summarize 就会把这个键设成 None（tot_cost <= 0 的分支）。
            _pct = s.get("总浮动盈亏%")
            try:
                _pctf = float(_pct)
            except (TypeError, ValueError):
                _pctf = float("nan")
            pnl_txt = (f"{_n(pnl)}（{_pctf:+.2f}%）" if math.isfinite(_pctf)
                       else _n(pnl))
            pnl_cls = "v-good" if pnl >= 0 else "v-bad"
        for _, r in my_port.iterrows():
            if str(r.get("_level")) == "act":
                act_list.append(
                    f'<div class="kv"><span><b>{_esc(r.get("代码"))} '
                    f'{_esc(r.get("名称"))}</b>'
                    f'<span class="muted small">　买入 {_esc(r.get("买入价"))} × '
                    f'{_esc(r.get("股数") or "未填")}　现价 {_esc(r.get("现价"))}</span></span>'
                    f'<span class="pill bad">{_esc(r.get("建议动作"))}</span></div>'
                    f'<p class="small" style="margin:2px 0 10px">'
                    f'{_md_inline(r.get("提醒"))}</p>'
                )

    def kpi(label: str, value: str, note: str = "", cls: str = "") -> str:
        return (f'<div class="card"><div class="kv"><b>{_esc(label)}</b>'
                f'<span class="muted small">{note}</span></div>'
                f'<div class="big {cls}">{value}</div></div>')

    cards = "".join([
        kpi("宏观背景分", _f(d.get("macro_score"), 1),
            _esc(str(d.get("regime") or "")), "v-mid"),
        kpi("目标仓位", _f((d.get("position") or 0) * 100, 0, "%"),
            "由宏观背景分分段映射"),
        kpi("决策时点", _esc(str(d.get("as_of") or "—")), "数据截止到这一天"),
        kpi("我的持仓浮动盈亏", pnl_txt,
            f"成本 {cost_txt} / 市值 {mv_txt}", pnl_cls),
        kpi("需要动手", act_txt, f"留意 {watch_txt} 只", "v-bad" if act_txt not in ("—", "0") else ""),
    ])

    picks = "".join(
        f'<div class="kv"><span><b>{_esc(code)}</b> {_esc(name)}</span>'
        f'<span><span class="pill {"great" if act == "建仓" else "mid"}">{_esc(act)}</span>'
        f'　<b>{score:.1f}</b></span></div>'
        for code, name, score, act in top_rows[:8]
    ) or '<p class="muted small">候选池里目前没有建仓/接近建仓的标的。</p>'

    fresh_txt = "—"
    if freshness:
        # data_freshness 的行的键是 item/latest/fetched/cycle/note；取不到数据时 latest 为 "-"
        missing = [f for f in freshness if str(f.get("latest") or "-").strip() == "-"]
        fresh_txt = (f"{len(freshness)} 类数据都拿到了"
                     if not missing else
                     f"{len(freshness)} 类数据，其中 {len(missing)} 类没取到："
                     + "、".join(str(f.get("item")) for f in missing))

    return (
        "<h2>总览：现在该做什么</h2>"
        f'<div class="grid grid5">{cards}</div>'
        '<div class="card"><h3>📣 今天要你处理的</h3>'
        + ("".join(act_list) if act_list
           else '<p class="muted small">没有触发卖出条件的持仓。'
                '（台账为空时这里也自然是空的——先去「我的持仓」录入。）</p>')
        + "</div>"
        '<div class="grid grid2">'
        '<div class="card"><h3>候选建仓（按总分）</h3>'
        f'<p class="small muted">建仓 {ready_n} 只　·　接近建仓 {near_n} 只　·　观望 {watch_n} 只　'
        f'→ <a href="/pick">去「选股 · 勾选建仓」把它们加进持仓</a></p>'
        f"{picks}</div>"
        '<div class="card"><h3>数据新鲜度</h3>'
        f'<p class="small muted">{_esc(fresh_txt)}</p>'
        '<p class="small muted">行情/估值是秒级实时的（本页顶部数字每 15 秒自己刷新）；'
        '宏观（PMI/CPI/PPI/GDP）是月度、季度公布，<b>本质上不可能实时</b>，'
        '不要指望它变快。详情见 <a href="/data">数据新鲜度</a>。</p></div>'
        "</div>"
    )


def build_dashboard(
    cfg,
    out_path: Path,
    macro=None,
    scores: pd.DataFrame | None = None,
    macro_bt: dict[str, Any] | None = None,
    port_bt: dict[str, Any] | None = None,
    policy_events: pd.DataFrame | None = None,
    industry_policy: pd.DataFrame | None = None,
    explain_map: dict[str, str] | None = None,
    flags: dict[str, Any] | None = None,
    freshness: list[dict[str, Any]] | None = None,
    track: pd.DataFrame | None = None,
    stock_bt: dict[str, Any] | None = None,
    my_port: pd.DataFrame | None = None,
) -> Path:
    as_of = getattr(macro, "as_of", None)
    as_of_txt = str(pd.Timestamp(as_of).date()) if as_of is not None else datetime.now().strftime("%Y-%m-%d")
    top_display = int((cfg.get_path("report", {}) or {}).get("top_display", 50) or 50)

    sections = dashboard_sections(
        cfg, macro=macro, scores=scores, macro_bt=macro_bt, port_bt=port_bt,
        policy_events=policy_events, industry_policy=industry_policy,
        explain_map=explain_map, freshness=freshness, track=track,
        stock_bt=stock_bt, my_port=my_port, flags=flags, top_display=top_display,
    )

    body = [
        '<div class="wrap">',
        "<h1>A 股长期投资趋势判断系统</h1>",
        f'<p class="sub">决策时点 <b>{_esc(as_of_txt)}</b>　·　'
        f'生成于 {datetime.now().strftime("%Y-%m-%d %H:%M")}　·　'
        "判断对象：个股长期投资价值（A 股）"
        + ('　·　<span class="tag ok">实时模式</span>' if (flags or {}).get("live")
           else '　·　<span class="tag info">离线模式（复用缓存）</span>')
        + "</p>",
    ]
    body.extend(s["html"] for s in sections)
    body.append('<footer>本报告由本地脚本自动生成，仅用于研究与复盘，<b>不构成投资建议</b>。'
                "所有打分口径与权重都写在 <code>config/settings.toml</code>，政策表在 "
                "<code>config/industry_policy.csv</code> 与 <code>config/policy_events.csv</code>，"
                "可自行修改后重跑。</footer>")
    body.append("</div>")

    doc = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
           '<meta name="viewport" content="width=device-width,initial-scale=1">'
           "<title>A 股长期投资趋势判断系统</title>"
           f"{_auto_refresh(cfg)}"
           f"<style>{CSS}</style></head><body>{''.join(body)}</body></html>")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path


def build_markdown(
    cfg,
    out_path: Path,
    macro=None,
    scores: pd.DataFrame | None = None,
    macro_bt: dict[str, Any] | None = None,
    port_bt: dict[str, Any] | None = None,
    flags: dict[str, Any] | None = None,
    top: int = 50,
    freshness: list[dict[str, Any]] | None = None,
    track: pd.DataFrame | None = None,
    stock_bt: dict[str, Any] | None = None,
    my_port: pd.DataFrame | None = None,
) -> Path:
    """纯文本/Markdown 版报告（便于 diff 与粘贴）。"""
    L: list[str] = []
    as_of = getattr(macro, "as_of", None)
    L.append("# A 股长期投资趋势判断系统 — 报告")
    L.append("")
    L.append(f"- 决策时点：**{pd.Timestamp(as_of).date() if as_of is not None else '—'}**")
    L.append(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    L.append("- 判断对象：个股长期投资价值（A 股）")
    L.append("")

    cnt = _Counter()
    if macro is not None:
        d = macro.as_dict()
        L.append(f"## {cnt.next()}、宏观背景分与目标仓位")
        L.append("")
        L.append(f"- 宏观背景分：**{_f(d.get('macro_score'), 1)} / 100**")
        L.append(f"- 目标股票仓位：**{_f((d.get('position') or 0) * 100, 0)}%**")
        L.append(f"- 状态：**{d.get('regime')}**")
        if d.get("unavailable"):
            L.append(f"- 缺失模块（权重已重新归一化）：{', '.join(d['unavailable'])}")
        L.append("")
        L.append("| 模块 | 权重 | 得分 | 状态 | 说明 |")
        L.append("|---|---|---|---|---|")
        for key in ("pmi", "ppi", "fed", "cpi", "gdp"):
            res = d["rules"].get(key)
            if res is None:
                continue
            L.append(f"| {MACRO_LABELS.get(key, key)} | {d['weights'].get(key, 0):.2f} | "
                     f"{_f(res.get('score'), 1)} | {res.get('state')} | {res.get('explain')} |")
        L.append("")

    if my_port is not None and len(my_port):
        s = my_port.attrs.get("summary", {}) or {}
        fees = s.get("fees", {}) or {}
        L.append(f"## {cnt.next()}、我的持仓：录入 → 跟踪 → 卖出提醒")
        L.append("")
        L.append(f"- 持仓 **{s.get('持仓只数', 0)}** 只"
                 f"（有现价 {s.get('有现价只数', 0)} 只，无现价 {s.get('无现价只数', 0)} 只不计入合计）")
        L.append(f"- 总成本(含费) **¥{_n(s.get('总成本(含费)'))}**　·　"
                 f"总市值 **¥{_n(s.get('总市值'))}**")
        L.append(f"- 总浮动盈亏 **¥{_n(s.get('总浮动盈亏'))}（{_n(s.get('总浮动盈亏%'))}%）**")
        L.append(f"- 需要动手 **{s.get('需要动手', 0)}** 只　·　留意 {s.get('留意', 0)} 只")
        L.append("")
        _act = (my_port[my_port["_level"].isin(["act", "watch"])]
                if "_level" in my_port.columns else my_port.iloc[0:0])
        if len(_act):
            L.append("### 需要你处理的")
            L.append("")
            for _, _r in _act.iterrows():
                L.append(f"- `{_r.get('代码')}` {_r.get('名称')}：{_r.get('提醒')}")
            L.append("")
        _cols = [c for c in ["代码", "名称", "买入日期", "买入价", "股数", "含费成本", "含费成本价",
                             "现价", "市值", "浮动盈亏", "浮动盈亏%", "建议动作",
                             "建议卖出%", "建议卖出股数", "到账金额", "扣费后净盈利", "备注"]
                 if c in my_port.columns]
        L.append(my_port[_cols].to_markdown(index=False))
        L.append("")
        L.append(f"手续费口径：佣金 {fees.get('佣金费率', 0):.5f}（买卖双向，单笔最低 "
                 f"¥{fees.get('佣金最低', 5):g}）、印花税 {fees.get('印花税率', 0):.5f}（仅卖出）、"
                 f"过户费 {fees.get('过户费率', 0):.5f}（双向）。"
                 f"「减仓」默认卖出 {float(s.get('reduce_ratio', 0.5)):.0%}。"
                 "「扣费后净盈利」= 卖出到账金额 − 该部分对应的含费成本（买入佣金也计入成本）。")
        L.append("")
        L.append("完整清单见 `output/portfolio.csv`；台账在 `config/holdings.csv`。")
        L.append("")
    else:
        L.append(f"## {cnt.next()}、我的持仓：录入 → 跟踪 → 卖出提醒")
        L.append("")
        L.append("还没有录入持仓。编辑 `config/holdings.csv`（六列："
                 "`代码,名称,买入日期,买入价,股数,备注`），或在网站首页点「我的持仓」录入，"
                 "重跑后这里会给出**卖多少股、扣费后净赚多少**。")
        L.append("")

    L.append(f"## {cnt.next()}、你提的 7 条判断条件：逐条验证")
    L.append("")
    for r in user_rule_verdicts(macro_bt):
        L.append(f"### {r['no']}. {r['claim']}")
        L.append("")
        L.append(f"- **结论：{r['verdict']}**（对应模块：{r['module']}）")
        if r.get("evidence"):
            e = r["evidence"]
            L.append(f"- 对应状态实测：{e.get('状态')}（n={_f(e.get('样本数'), 0)}）　"
                     f"前瞻均值 3/6/12 月：{_f(e.get('3月均值%'))}% / {_f(e.get('6月均值%'))}% / "
                     f"{_f(e.get('12月均值%'))}%　"
                     f"胜率 {_f(e.get('3月胜率%'), 1)}% / {_f(e.get('6月胜率%'), 1)}% / "
                     f"{_f(e.get('12月胜率%'), 1)}%")
        if r.get("counter_evidence"):
            c = r["counter_evidence"]
            L.append(f"- 对照状态实测：{c.get('状态')}（n={_f(c.get('样本数'), 0)}）　"
                     f"前瞻均值 3/6/12 月：{_f(c.get('3月均值%'))}% / {_f(c.get('6月均值%'))}% / "
                     f"{_f(c.get('12月均值%'))}%　"
                     f"胜率 {_f(c.get('3月胜率%'), 1)}% / {_f(c.get('6月胜率%'), 1)}% / "
                     f"{_f(c.get('12月胜率%'), 1)}%")
        L.append(f"- 说明：{r['note']}")
        L.append("")

    if macro_bt:
        L.append(f"## {cnt.next()}、宏观规则明细（回测A）")
        L.append("")
        s = macro_bt.get("summary")
        if isinstance(s, dict):
            b = s.get("基准(沪深300)") or {}
            L.append(f"- 样本：{s.get('行数', '—')} 个月末　区间：{s.get('区间', '—')}　"
                     f"全样本基准（沪深300）前瞻均值："
                     f"3 月 {_f(b.get('3月均值%'), 2)}%　6 月 {_f(b.get('6月均值%'), 2)}%　"
                     f"12 月 {_f(b.get('12月均值%'), 2)}%")
        else:
            L.append(str(s or ""))
        L.append("")
        for rule, df in (macro_bt.get("tables") or {}).items():
            L.append(f"### {rule}")
            L.append("")
            L.append(df.to_markdown(index=False))
            L.append("")

    if port_bt:
        L.append(f"## {cnt.next()}、个股组合回测（回测B）")
        L.append("")
        m = port_bt.get("metrics") or {}
        port = m.get("组合") or {}
        bench = m.get("沪深300") or {}
        exc = m.get("超额") or {}
        L.append(f"- 每期持仓数：{port_bt.get('top_n')}　单边成本：{port_bt.get('cost_rate')}　"
                 f"期数：{_f(port.get('期数'), 0)}")
        L.append(f"- 组合：累计收益 {_f(port.get('累计收益%'), 2)}%　"
                 f"年化 {_f(port.get('年化收益%'), 2)}%　夏普 {_f(port.get('夏普'))}　"
                 f"最大回撤 {_f(port.get('最大回撤%'), 2)}%　胜率 {_f(port.get('胜率%'), 1)}%")
        L.append(f"- 沪深300：累计收益 {_f(bench.get('累计收益%'), 2)}%　"
                 f"年化 {_f(bench.get('年化收益%'), 2)}%　"
                 f"最大回撤 {_f(bench.get('最大回撤%'), 2)}%")
        L.append(f"- 累计超额 {_f(exc.get('累计超额%'), 2)}%　"
                 f"平均每期超额 {_f(exc.get('平均每期超额%'), 2)}%　"
                 f"跑赢期数占比 {_f(exc.get('跑赢期数占比%'), 1)}%")
        for note in (m.get("_disclaimer") or []):
            L.append(f"- ⚠️ {note}")
        L.append("")
        p = port_bt.get("periods")
        if p is not None and len(p):
            cols = [c for c in ["调仓日", "下期", "持仓数", "换手率%", "组合收益%", "沪深300%",
                                "超额%", "组合净值", "基准净值"] if c in p.columns]
            L.append(p[cols].to_markdown(index=False))
            L.append("")

    if stock_bt:
        trades = stock_bt.get("trades")
        m = stock_bt.get("metrics") or {}
        s = m.get("汇总") or {}
        L.append(f"## {cnt.next()}、个股逐笔买卖回测（回测C，不组组合）")
        L.append("")
        L.append("按同一套买卖规则，在每个评估日对每只股票独立判断：买入门槛全通过就按"
                 "**次日收盘价**建仓，触发卖出条件就卖。每笔与「同期买入并持有」对比，"
                 "用来判断**卖出规则是否创造价值**。")
        L.append("")
        for k, v in s.items():
            if isinstance(v, (dict, list)):
                continue
            L.append(f"- {k}：{v}")
        by_reason = s.get("按卖出原因")
        if isinstance(by_reason, dict) and by_reason:
            L.append("")
            L.append("| 卖出原因 | 统计 |")
            L.append("|---|---|")
            for k, v in by_reason.items():
                L.append(f"| {k} | {v} |")
        for note in (m.get("_disclaimer") or []):
            L.append(f"- ⚠️ {note}")
        L.append("")
        if trades is not None and len(trades):
            L.append(f"逐笔明细共 {len(trades)} 笔，前 40 笔：")
            L.append("")
            L.append(trades.head(40).to_markdown(index=False))
            L.append("")

    if scores is not None and len(scores):
        L.append(f"## {cnt.next()}、当前个股打分（前 {min(top, len(scores))} 名）")
        L.append("")
        L.append(scores.head(top).to_markdown(index=False))
        L.append("")

    if track is not None and len(track):
        L.append(f"## {cnt.next()}、个股买卖决策与跟踪")
        L.append("")
        L.append("打分是连续的，决策是离散的。下表逐条核对了买入门槛与卖出条件，"
                 "动作由 `touzi/plans.py` 按 `config/settings.toml` 的 `[plan]` 段算出。"
                 "阈值改完重跑，结论立刻跟着变。")
        L.append("")
        L.append(_plan_rules_md(cfg.get("plan", {}) if cfg is not None else None))
        L.append("")
        counts = track["建议动作"].value_counts().to_dict()
        L.append("- 动作分布：" + "　".join(f"{k} {v}" for k, v in counts.items()))
        L.append("")
        show = track[track["建议动作"].isin(
            ["清仓", "减仓", "加仓", "建仓", "接近建仓", "持有"])]
        cols = [c for c in ["代码", "名称", "建议动作", "现价", "持仓成本", "浮动盈亏%",
                            "PE_TTM", "PB", "股息率%", "PE历史分位", "PB历史分位", "PE×PB",
                            "总分", "未通过的门槛", "触发的卖出条件", "预警"]
                if c in show.columns]
        if len(show):
            L.append(show[cols].head(80).to_markdown(index=False))
            L.append("")
        L.append("完整清单见 `output/tracking.csv`；持仓台账 `config/holdings.csv`。")
        L.append("")

    if freshness:
        L.append(f"## {cnt.next()}、数据新鲜度（哪一块实时、哪一块天然滞后）")
        L.append("")
        L.append("| 数据项 | 数据最新到 | 缓存抓取时刻 | 刷新周期 | 说明 |")
        L.append("|---|---|---|---|---|")
        for r in freshness:
            L.append(f"| {r.get('item','')} | {r.get('latest','-')} | {r.get('fetched','-')} | "
                     f"{r.get('cycle','-')} | {r.get('note','')} |")
        L.append("")

    L.append(f"## {cnt.next()}、方法与口径")
    L.append("")
    L.append("- 防未来函数：宏观按公布滞后后移；个股历史分位只用当期之前数据；"
             "组合回测按 point-in-time 取当日 PE/PB/股价并禁用截面分位；政策事件过滤未来日期。")
    L.append("- 三个偏差：幸存者偏差（退市股不在样本）、估值历史仅 5 年、"
             "point-in-time 下无历史全市场快照故关闭截面分位。")
    L.append("- 本机不可达已绕开的数据源：" + "；".join(FAILED_SOURCES))
    L.append("")
    L.append("- 数据源自身的已知偏差：")
    for c in DATA_CAVEATS:
        L.append(f"  - {c}")
    L.append("")
    if flags:
        L.append("```json")
        L.append(json.dumps(flags, ensure_ascii=False, indent=2, default=str))
        L.append("```")
        L.append("")
    L.append("---")
    L.append("")
    L.append("本报告由本地脚本自动生成，仅用于研究与复盘，**不构成投资建议**。")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(L), encoding="utf-8")
    return out_path
