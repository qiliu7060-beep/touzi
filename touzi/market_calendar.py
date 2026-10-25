"""经交易所公告核验的有限日历；范围外不猜测交易日。"""
from datetime import datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
CALENDAR = json.loads((Path(__file__).resolve().parents[1] / "config/trading-calendar.json").read_text(encoding="utf-8"))


def trading_day(day):
    text = day.isoformat()
    if not CALENDAR["start"] <= text <= CALENDAR["end"]:
        return None
    return day.weekday() < 5 and not any(a <= text <= b for a, b in CALENDAR["closures"])


def adjacent_day(day, direction):
    for _ in range(20):
        day += timedelta(days=direction)
        state = trading_day(day)
        if state is None:
            return None
        if state:
            return day
    return None


def market_clock(now):
    now = now.astimezone(TZ)
    opened = trading_day(now.date())
    if opened is None:
        return None
    minute = now.hour * 60 + now.minute
    phase = "closed" if not opened or minute < 570 or minute >= 900 else "break" if 690 <= minute < 780 else "trading"
    quote_day = now.date() if opened and minute >= 570 else adjacent_day(now.date(), -1)
    return {"phase": phase, "quote_day": quote_day} if quote_day else None


def quote_state(quote, now):
    now, quote = now.astimezone(TZ), quote.astimezone(TZ)
    clock = market_clock(now)
    if not clock or quote > now + timedelta(minutes=1) or quote.date() != clock["quote_day"]:
        return None
    minute = quote.hour * 60 + quote.minute
    if clock["phase"] == "trading":
        return "trading" if now - quote <= timedelta(minutes=2) else None
    if clock["phase"] == "break":
        return "break" if 688 <= minute <= 690 else None
    return "closed" if 900 <= minute < 1080 else None


def model_expiry(built, quote):
    if not market_clock(built):
        return None
    if quote_state(quote, built) != "closed":
        return built + timedelta(hours=36)
    next_day = adjacent_day(quote.astimezone(TZ).date(), 1)
    return datetime.combine(next_day, datetime.min.time(), TZ) + timedelta(hours=9, minutes=30) if next_day else None
