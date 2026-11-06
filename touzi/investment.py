"""月度新增资金的公开环境；不改写已有总持仓模型。"""
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
import math
import pandas as pd
from touzi.market_calendar import CALENDAR, TZ, trading_day, adjacent_day


def bond_signal(series, now, rules):
    values = pd.to_numeric(series, errors="coerce")
    values.index = pd.to_datetime(values.index)
    if any(d.date() > now.date() for d in values.index):
        raise ValueError("国债源存在未来日期")
    values = values[~values.index.duplicated(keep="last")].sort_index()
    values = values[[d.date() <= now.date() and trading_day(d.date()) is True for d in values.index]]
    values = values[values.map(lambda x: math.isfinite(x) and x >= 0)]
    if len(values) < 65:
        raise ValueError("国债历史不足65个有效交易日")
    avg = float(values.iloc[-5:].mean())
    d20 = round((avg-float(values.iloc[-25:-20].mean()))*100, 8)
    d60 = round((avg-float(values.iloc[-65:-60].mean()))*100, 8)
    factor = 1 if d20 <= 0 else .75 if d20 < rules["caution_bp"] else .5 if d20 < rules["pause_bp"] else 0
    interval = ("≤0" if d20 <= 0 else f"0至不足{rules['caution_bp']:g}" if d20 < rules["caution_bp"]
                else f"{rules['caution_bp']:g}至不足{rules['pause_bp']:g}" if d20 < rules["pause_bp"]
                else f"≥{rules['pause_bp']:g}")
    trend_reason = f"20日趋势{d20}基点，命中{interval}基点区间，基础投入系数{factor:.0%}"
    long_reason = f"60日趋势{d60}基点，未达到{rules['long_pressure_bp']:g}基点压力阈值，不限制基础系数"
    if d60 >= rules["long_pressure_bp"]:
        long_reason = (f"60日趋势{d60}基点，达到{rules['long_pressure_bp']:g}基点压力阈值，"
                       + ("将投入系数限制为50%" if factor > .5 else "系数上限为50%，基础系数已不高于上限"))
        factor = min(factor, .5)
    return {"yield_pct": float(values.iloc[-1]), "observation_date": str(values.index[-1].date()),
            "fetched_at": now.isoformat(), "delta20_bp": d20, "delta60_bp": d60,
            "status": "ok", "factor": factor,
            "reasons": [trend_reason, long_reason, "国债收益率与已有固定利率债券价格通常反向变动", "趋势规则为可配置工程参数，未经绩效验证"]}


def allocate(amount, position, score, factor):
    text = str(amount)
    import re
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", text) or Decimal(text) > 100000000:
        raise ValueError("请输入0至100000000元，最多两位小数")
    if not all(math.isfinite(x) for x in (position, score, factor)) or not 0 <= position <= 1 or not 0 <= score <= 100 or factor not in (0, .5, .75, 1):
        raise ValueError("分配参数待核验")
    cents = int(Decimal(text)*100)
    stock = (Fraction(2, 3) if abs(position-2/3)<1e-12 else Fraction(int(position*1000000+.5), 1000000)) if score > 30 else Fraction(0)
    bond = min(Fraction(1, 3), 1-stock)*Fraction(str(factor))
    stocks = int(cents*stock)
    bonds = int(cents*bond)
    return {"stock": stocks, "bond": bonds, "cash": cents-stocks-bonds}
