"""政策红利打分：行业政策底分（结构性）+ 政策事件加成（催化性）。

## 为什么拆成两层

「国家政策的红利」在实际投资中体现为两种完全不同的东西：

1. **结构性倾斜**——五年规划、产业目录把某些行业长期放在资源倾斜的位置
   （半导体、新能源、高端装备、军工、数字经济）。这是慢变量，决定一个行业
   能不能长期享受融资、税收、订单上的便利。
2. **事件性催化**——某次会议、某个文件、某次专项补贴在短期内引爆行情。
   这是快变量，有明确的时间衰减。

用一层打分无法同时表达「长期受扶持」和「刚出了利好」这两件事，
所以本模块 = 行业底分（上限 ``industry_max``，默认 80）+ 事件加成
（上限 ``event_max``，默认 20）。

## 事件衰减

单个事件的贡献按半衰期衰减：``impact × 0.5 ** (距今天数 / half_life_days)``。
默认半衰期 180 天——一份产业规划的市场影响力大约在半年后减半，
两年后基本湮没。所有事件贡献求和后封顶在 ``event_max``，
避免一次政策密集期把分数顶穿。

## ⚠️ 这张表是「人工判断」而不是「抓来的数据」

``config/industry_policy.csv`` 里的分数**不是任何接口返回的客观数据**，
而是把公开政策文件（政府工作报告、五年规划、产业目录）折算成 0~100 的
主观打分。它必须由使用者按自己的判断维护，并且带 ``as_of`` 日期以便审计。
系统不会假装它是客观事实——报告里会原样展示这张表和它的日期。

这是本系统中**唯一**带主观成分的模块，也是唯一需要人工更新的配置文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..util import clamp
from .macro import RuleResult

__all__ = [
    "PolicyInput",
    "score_policy",
    "load_industry_policy",
    "load_policy_events",
    "DEFAULT_INDUSTRY_POLICY",
    "INDUSTRY_ALIASES",
]


#: 行业政策优先级打分（0~100）。来源：公开政策文件的人工折算，非接口数据。
#: 分档依据 —— 90+：明确列为国家战略且持续投入；75~89：长期受扶持；
#: 60~74：中性偏支持；45~59：政策中性；<45：处于调控或去产能压力下。
DEFAULT_INDUSTRY_POLICY: dict[str, tuple[float, str]] = {
    # 科技自立 / 新质生产力：政策资源最集中的方向
    "电子": (96.0, "半导体与集成电路列为国家战略首位，大基金与税收优惠持续加码"),
    "计算机": (92.0, "信创、人工智能、数据要素均属国家级战略方向"),
    "通信": (88.0, "5G/6G 与算力网络纳入新型基础设施，运营商资本开支受政策引导"),
    "电力设备": (90.0, "新能源装机与特高压持续受双碳目标驱动"),
    "国防军工": (87.0, "国防预算稳定增长，装备现代化为长期确定方向"),
    "机械设备": (82.0, "高端装备国产替代与工业母机专项支持"),
    "汽车": (80.0, "新能源汽车购置税减免与以旧换新政策延续"),
    # 医药与消费：政策方向分化
    "医药生物": (72.0, "创新药审批加速与医保谈判并存，创新受益、仿制承压"),
    "食品饮料": (62.0, "促消费政策托底，但无专项产业扶持"),
    "家用电器": (70.0, "以旧换新与家电下乡补贴直接拉动需求"),
    "农林牧渔": (68.0, "粮食安全与种业振兴为长期政策主线"),
    "社会服务": (64.0, "文旅消费受促消费政策支持，波动较大"),
    "商贸零售": (58.0, "消费刺激政策受益，但行业竞争格局分散"),
    "纺织服饰": (52.0, "出口退税与产业转移政策，无显著倾斜"),
    "轻工制造": (52.0, "政策中性"),
    "美容护理": (50.0, "政策中性，主要由消费景气驱动"),
    # 周期与传统产业：供给侧与转型
    "有色金属": (74.0, "新能源金属与战略小金属受资源安全政策支持"),
    "基础化工": (60.0, "新材料方向受支持，传统化工受能耗双控约束"),
    "钢铁": (48.0, "产能置换与超低排放改造带来成本压力，属调控行业"),
    "煤炭": (56.0, "能源保供要求下产量受控，双碳长期压制"),
    "石油石化": (54.0, "能源安全托底，但双碳目标限制长期扩张"),
    "公用事业": (66.0, "电力市场化改革与绿电消纳政策受益"),
    "交通运输": (60.0, "物流降本与基建投资受益，政策中性偏支持"),
    "建筑材料": (44.0, "受地产链拖累，政策以稳为主而非扶持"),
    "建筑装饰": (46.0, "地方化债约束基建扩张，政策支持力度下降"),
    "环保": (70.0, "美丽中国与双碳目标带来持续订单"),
    # 金融地产：调控与稳定并存
    "银行": (58.0, "让利实体与净息差收窄压力，政策以稳定为主"),
    "非银金融": (56.0, "资本市场改革利好券商，但监管趋严"),
    "房地产": (42.0, "政策底已现但基调仍是「房住不炒」，去库存压力未解"),
    "综合": (50.0, "无明确行业政策方向"),
}

#: 新浪行业板块名 → 申万一级行业名的别名映射（新浪的板块划分比申万更细）。
INDUSTRY_ALIASES: dict[str, str] = {
    "电子器件": "电子", "电子元件": "电子", "半导体": "电子",
    "电子信息": "计算机", "软件服务": "计算机", "互联网": "计算机",
    "通讯行业": "通信",
    "电力行业": "公用事业", "供水供气": "公用事业", "环保行业": "环保",
    "机械行业": "机械设备", "仪器仪表": "机械设备",
    "汽车制造": "汽车", "汽车配件": "汽车",
    "生物制药": "医药生物", "化学制药": "医药生物", "中药": "医药生物",
    "医疗器械": "医药生物", "医疗行业": "医药生物",
    "食品行业": "食品饮料", "酿酒行业": "食品饮料", "饮料制造": "食品饮料",
    "家电行业": "家用电器",
    "农牧饲渔": "农林牧渔",
    "旅游酒店": "社会服务", "酒店旅游": "社会服务",
    "商业百货": "商贸零售", "商业贸易": "商贸零售",
    "纺织服装": "纺织服饰", "服装鞋类": "纺织服饰",
    "有色金属": "有色金属", "小金属": "有色金属",
    "化工行业": "基础化工", "化学原料": "基础化工",
    "钢铁行业": "钢铁", "煤炭行业": "煤炭", "石油行业": "石油石化",
    "金融行业": "非银金融", "券商信托": "非银金融", "保险": "非银金融",
    "银行": "银行", "房地产": "房地产",
    "交运设备": "交通运输", "交通运输": "交通运输",
    "工程建设": "建筑装饰", "水泥建材": "建筑材料",
    "航天航空": "国防军工", "船舶制造": "国防军工",
    "电源设备": "电力设备", "输配电气": "电力设备",
    "造纸印刷": "轻工制造", "木业家具": "轻工制造",
    "文化传媒": "传媒", "传媒娱乐": "传媒",
}


class PolicyInput(dict):
    """政策打分的输入容器。"""

    FIELDS = ("code", "name", "industry", "policy_table", "events", "as_of")

    def __init__(self, **kw: Any):
        super().__init__({k: kw.get(k) for k in self.FIELDS})


def _resolve_path(cfg, key: str) -> Path | None:
    """把配置里的相对路径解析成工作区内的绝对路径。"""
    raw = cfg.policy.get(key)
    if not raw:
        return None
    try:
        return cfg.resolve(raw)
    except Exception:
        return None


def load_industry_policy(cfg, path: Path | None = None) -> pd.DataFrame:
    """读行业政策表；文件不存在就用内置默认表。

    返回列：``industry`` / ``score``(0~100) / ``note`` / ``as_of``。
    """
    p = path or _resolve_path(cfg, "industry_policy_file")
    if p is not None and Path(p).exists():
        try:
            df = pd.read_csv(p, dtype=str).fillna("")
            cols = {c.lower().strip(): c for c in df.columns}
            ind_col = cols.get("industry") or cols.get("行业")
            score_col = cols.get("score") or cols.get("政策分")
            if ind_col and score_col:
                out = pd.DataFrame({
                    "industry": df[ind_col].astype(str).str.strip(),
                    "score": pd.to_numeric(df[score_col], errors="coerce"),
                    "note": df[cols.get("note", "note")].astype(str) if "note" in cols else "",
                    "as_of": df[cols["as_of"]].astype(str) if "as_of" in cols else "",
                })
                out = out.dropna(subset=["score"])
                if len(out):
                    out.attrs["source"] = str(p)
                    return out
        except Exception as exc:  # 读坏了就退回内置表，不让报告崩掉
            import logging
            logging.getLogger(__name__).warning("读取行业政策表失败(%s)，改用内置默认表：%s", p, exc)

    out = pd.DataFrame(
        [{"industry": k, "score": v[0], "note": v[1], "as_of": ""}
         for k, v in DEFAULT_INDUSTRY_POLICY.items()]
    )
    out.attrs["source"] = "内置默认表（config/industry_policy.csv 不存在）"
    return out


def load_policy_events(cfg, path: Path | None = None) -> pd.DataFrame:
    """读政策事件表。返回列 ``date``/``title``/``industry``/``impact``/``note``。

    ``industry`` 留空表示影响全市场（如降准、降息、中央经济工作会议）。
    ``impact`` 取 0~1，表示该事件对相关行业的利好强度。
    """
    p = path or _resolve_path(cfg, "event_file")
    empty = pd.DataFrame(columns=["date", "title", "industry", "impact", "note"])
    if p is None or not Path(p).exists():
        empty.attrs["source"] = f"事件表不存在（{p}）"
        return empty
    try:
        df = pd.read_csv(p, dtype=str).fillna("")
        cols = {c.lower().strip(): c for c in df.columns}
        need = ("date", "industry", "impact")
        if not all(k in cols for k in need):
            empty.attrs["source"] = f"事件表缺列（需要 date/industry/impact）：{p}"
            return empty
        out = pd.DataFrame({
            "date": pd.to_datetime(df[cols["date"]], errors="coerce"),
            "title": df[cols["title"]].astype(str) if "title" in cols else "",
            "industry": df[cols["industry"]].astype(str).str.strip(),
            "impact": pd.to_numeric(df[cols["impact"]], errors="coerce"),
            "note": df[cols["note"]].astype(str) if "note" in cols else "",
        }).dropna(subset=["date", "impact"])
        out.attrs["source"] = str(p)
        return out
    except Exception as exc:
        empty.attrs["source"] = f"读取事件表失败：{exc}"
        return empty


def _canon(industry: str) -> str:
    """把行业名归一到申万一级口径。"""
    s = str(industry or "").strip()
    if not s:
        return ""
    if s in DEFAULT_INDUSTRY_POLICY:
        return s
    if s in INDUSTRY_ALIASES:
        return INDUSTRY_ALIASES[s]
    for alias, canon in INDUSTRY_ALIASES.items():
        if alias in s or s in alias:
            return canon
    for canon in DEFAULT_INDUSTRY_POLICY:
        if canon in s or s in canon:
            return canon
    return s


def _match_row(table: pd.DataFrame, industry: str) -> tuple[float, str, str] | None:
    """在行业政策表里找匹配行，返回 (score, note, matched_name)。"""
    if table is None or len(table) == 0:
        return None
    target = _canon(industry)
    if not target:
        return None
    for _, row in table.iterrows():
        if _canon(row["industry"]) == target:
            return float(row["score"]), str(row.get("note", "")), str(row["industry"])
    # 退一步做子串匹配
    for _, row in table.iterrows():
        a, b = str(row["industry"]), target
        if a and (a in b or b in a):
            return float(row["score"]), str(row.get("note", "")), str(row["industry"])
    return None


def _decay(days: float, half_life: float) -> float:
    if not np.isfinite(days) or days < 0:
        return 0.0
    return float(0.5 ** (days / max(half_life, 1.0)))


def score_policy(data: PolicyInput | dict, cfg) -> RuleResult:
    """政策总分 = 行业底分(≤industry_max) + 事件加成(≤event_max)。"""
    pcfg = cfg.policy
    d = dict(data)
    industry = str(d.get("industry") or "")
    as_of = pd.Timestamp(d.get("as_of")) if d.get("as_of") else pd.Timestamp.today()
    industry_max = float(pcfg.industry_max)
    event_max = float(pcfg.event_max)
    half_life = float(pcfg.event_half_life_days)

    notes: list[str] = []
    detail: dict[str, Any] = {"industry_raw": industry}
    table = d.get("policy_table")
    if table is None:
        table = load_industry_policy(cfg)

    # ------------------------------------------------------ 行业底分
    hit = _match_row(table, industry)
    if hit is not None:
        raw, note, matched = hit
        industry_score = clamp(raw, 0.0, 100.0) / 100.0 * industry_max
        detail["industry_matched"] = matched
        detail["industry_policy_raw"] = raw
        notes.append(f"行业「{matched}」政策优先级 {raw:.0f}/100：{note}")
    else:
        industry_score = industry_max * 0.5  # 未收录行业给中性分，不奖不罚
        detail["industry_matched"] = None
        detail["industry_policy_raw"] = None
        notes.append(f"行业「{industry or '未知'}」不在政策表内，按中性 {industry_score:.0f} 分处理")

    # ------------------------------------------------------ 事件加成
    events = d.get("events")
    event_score = 0.0
    hits: list[dict[str, Any]] = []
    if events is not None and len(events) > 0:
        target = _canon(industry)
        for _, ev in events.iterrows():
            ev_date = pd.Timestamp(ev["date"])
            if ev_date > as_of:
                continue  # 防未来函数：只看已发生的事件
            ev_ind = str(ev.get("industry") or "").strip()
            # 行业为空 = 全市场性政策；否则要求行业匹配
            if ev_ind and _canon(ev_ind) != target:
                continue
            days = float((as_of - ev_date).days)
            w = _decay(days, half_life)
            if w < 0.02:
                continue
            contrib = float(ev["impact"]) * w * event_max
            event_score += contrib
            hits.append({
                "date": str(ev_date.date()),
                "title": str(ev.get("title") or ""),
                "industry": ev_ind or "全市场",
                "impact": round(float(ev["impact"]), 3),
                "decay_weight": round(w, 4),
                "contribution": round(contrib, 2),
                "days_ago": int(days),
            })
        hits.sort(key=lambda x: x["contribution"], reverse=True)
        event_score = min(event_score, event_max)
        if hits:
            top = hits[0]
            notes.append(
                f"近 {top['days_ago']} 天内有效政策事件 {len(hits)} 条，"
                f"贡献最大的是「{top['title']}」（衰减后 +{top['contribution']:.1f} 分）"
            )
        else:
            notes.append("近期无匹配的有效政策事件")
    else:
        notes.append("未加载政策事件表，事件加成计 0")

    detail["events"] = hits[:10]
    detail["event_count"] = len(hits)

    total = clamp(industry_score + event_score)
    if total >= 80:
        state = "政策强受益"
    elif total >= 65:
        state = "政策受益"
    elif total >= 50:
        state = "政策中性"
    elif total >= 35:
        state = "政策偏弱"
    else:
        state = "政策压制"

    detail["industry_score"] = round(industry_score, 2)
    detail["event_score"] = round(event_score, 2)
    return RuleResult(
        name="政策",
        score=total,
        state=state,
        detail=detail,
        explain=f"{d.get('code', '')} {d.get('name', '')}".strip() + "；" + "；".join(notes),
    )
