// 日历常量由 scripts/sync_live_runtime.py 从经核验公告数据生成。
export const MARKET_CALENDAR = {"version":"cn-a-2026-v1","start":"2026-01-01","end":"2026-12-31","exchanges":["SSE","SZSE","BSE"],"sources":["https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml","https://www.szse.cn/disclosure/notice/general/t20251222_618087.html","https://www.bse.cn/important_news/200027428.html"],"closures":[["2026-01-01","2026-01-04"],["2026-02-15","2026-02-23"],["2026-04-04","2026-04-06"],["2026-05-01","2026-05-05"],["2026-06-19","2026-06-21"],["2026-09-25","2026-09-27"],["2026-10-01","2026-10-07"]]};
const DAY_MS = 86400000;
const OFFSET_MS = 28800000;
/** @param {unknown} value */
export function validCalendar(value) {
  return JSON.stringify(value) === JSON.stringify(MARKET_CALENDAR);
}
/** @param {string} day */
function tradingDay(day) {
  if (day < MARKET_CALENDAR.start || day > MARKET_CALENDAR.end) return null;
  const weekday = new Date(day + 'T12:00:00+08:00').getUTCDay();
  return weekday !== 0 && weekday !== 6 && !MARKET_CALENDAR.closures.some(([a, b]) => day >= a && day <= b);
}
/** @param {string} day @param {number} direction */
function adjacentTradingDay(day, direction) {
  let stamp = Date.parse(day + 'T00:00:00Z');
  // 公告日历外及超过20个自然日均拒绝，不推测新年度或临时停市。
  for (let i = 0; i < 20; i++) {
    stamp += direction * DAY_MS;
    const candidate = new Date(stamp).toISOString().slice(0, 10);
    const open = tradingDay(candidate);
    if (open === null) return null;
    if (open) return candidate;
  }
  return null;
}
/** @param {number} now @param {unknown} calendar */
export function marketClock(now, calendar) {
  if (!Number.isFinite(now) || !validCalendar(calendar)) return null;
  const local = new Date(now + OFFSET_MS), day = local.toISOString().slice(0, 10);
  const open = tradingDay(day);
  if (open === null) return null;
  const minute = local.getUTCHours() * 60 + local.getUTCMinutes();
  const phase = !open || minute < 570 || minute >= 900 ? 'closed'
    : minute >= 690 && minute < 780 ? 'break' : 'trading';
  const quoteDay = open && minute >= 570 ? day : adjacentTradingDay(day, -1);
  return quoteDay ? { day, phase, quoteDay, open } : null;
}
/** @param {{price:number,quote_ts:string}} quote @param {number} now @param {unknown} calendar */
export function calendarQuoteFresh(quote, now, calendar) {
  const clock = marketClock(now, calendar), stamp = Date.parse(quote.quote_ts);
  if (!clock || !Number.isFinite(stamp) || !(quote.price > 0) || stamp > now + 60000) return null;
  const local = new Date(stamp + OFFSET_MS), day = local.toISOString().slice(0, 10);
  const minute = local.getUTCHours() * 60 + local.getUTCMinutes();
  if (day !== clock.quoteDay || !tradingDay(day)) return null;
  if (clock.phase === 'trading') return now - stamp <= 120000 ? 'trading' : null;
  if (clock.phase === 'break') return minute >= 688 && minute <= 690 ? 'break' : null;
  return minute >= 900 && minute < 1080 ? 'closed' : null;
}
/** @param {number} built @param {string} quoteAt @param {unknown} calendar */
export function calendarModelExpiry(built, quoteAt, calendar) {
  if (!validCalendar(calendar) || !marketClock(built, calendar)) return null;
  // 只有生成时已核验的完整收盘基线才能跨休市；盘中模型仍保留36小时上限。
  if (calendarQuoteFresh({price:1,quote_ts:quoteAt}, built, calendar) !== 'closed') return built + 36 * 3600000;
  const day = new Date(Date.parse(quoteAt) + OFFSET_MS).toISOString().slice(0, 10);
  const next = adjacentTradingDay(day, 1);
  return next ? Date.parse(next + 'T09:30:00+08:00') : null;
}
/** @param {number} built @param {string} quoteAt @param {number} now @param {unknown} calendar */
export function closedBaseline(built, quoteAt, now, calendar) {
  const expiry = calendarModelExpiry(built, quoteAt, calendar);
  return expiry !== null && built <= now + 60000 && now < expiry
    && now - built <= 20 * DAY_MS
    && calendarQuoteFresh({price:1,quote_ts:quoteAt}, built, calendar) === 'closed'
    && calendarQuoteFresh({price:1,quote_ts:quoteAt}, now, calendar) === 'closed';
}
