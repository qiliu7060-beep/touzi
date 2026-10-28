import { marketClock, validCalendar } from './market-calendar.mjs';

export const CLOSE_KIND = 'touzi-close-v1';
export const CLOSE_PART_KIND = 'touzi-close-part-v1';
export const MAX_CLOSE_CODES = 10000;
export const MAX_MANIFEST_BYTES = 2 * 1024 * 1024;
export const MAX_PART_BYTES = 4 * 1024 * 1024;
export const CLOSE_ACTIONS = ['建仓', '接近建仓', '观望', '回避', '候选'];
const OFFSET = 28800000;
const DAY = 86400000;
const BASE_BUY_KEYS = ['total', 'quality', 'pe_percentile', 'pb_percentile',
  'dividend_yield', 'pe_abs', 'graham', 'macro', 'debt_equity', 'capex_to_profit'];
const PRIVATE_KEYS = new Set(['__proto__', 'prototype', 'constructor', 'holdings', 'ledger',
  'owner', 'owner_id', 'user_id', 'email', 'buy_price', 'buy_date', 'shares', '买入价', '买入日期', '股数']);
type JsonObject = Record<string, unknown>;
export type CloseIndex = { code: string; name: string; action: string; total: number | null; shard: string };
export type CloseShard = { path: string; sha256: string; codes: string[] };
export type CloseManifest = {
  schema_version: 1; kind: string; score_date: string; quote_at: string; built_at: string;
  trading_calendar: unknown; universe: Record<string, number>; plan: Record<string, number>;
  fees: Record<string, number>; financial_coverage?: unknown; index: CloseIndex[]; shards: CloseShard[];
};
export type CloseEntry = JsonObject & { code: string; name: string; action: string; total: number | null;
  price: number; quote_ts: string; buy_checks: JsonObject[]; sell_checks: JsonObject[]; sell_snapshot: JsonObject };
export function requireClose(condition: unknown, message = '收盘评分资产格式无效'): asserts condition {
  if (!condition) throw new Error(message);
}
export function closeObject(value: unknown): JsonObject {
  requireClose(value !== null && typeof value === 'object' && !Array.isArray(value));
  requireClose(Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
  return value as JsonObject;
}
function finite(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value); }
function nullableNumber(value: unknown): boolean { return value === null || finite(value); }
function text(value: unknown, length = 300): value is string { return typeof value === 'string' && value.length <= length; }
export function closeDay(value: unknown): string {
  requireClose(typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value));
  const stamp = Date.parse(value + 'T00:00:00Z');
  requireClose(Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === value);
  return value;
}
export function closeStamp(value: unknown): number {
  requireClose(typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value));
  closeDay(value.slice(0, 10));
  requireClose(Number(value.slice(11, 13)) < 24 && Number(value.slice(14, 16)) < 60 && Number(value.slice(17, 19)) < 60);
  const stamp = Date.parse(value); requireClose(Number.isFinite(stamp)); return stamp;
}
function publicTree(value: unknown): void {
  const pending: Array<{ value: unknown; depth: number }> = [{ value, depth: 0 }];
  let nodes = 0;
  while (pending.length) {
    const current = pending.pop()!; nodes++;
    requireClose(nodes <= 300000 && current.depth <= 20, '收盘资产结构超出限制');
    if (typeof current.value === 'number') requireClose(Number.isFinite(current.value));
    if (current.value && typeof current.value === 'object') {
      if (!Array.isArray(current.value)) closeObject(current.value);
      for (const [key, child] of Object.entries(current.value)) {
        requireClose(!PRIVATE_KEYS.has(key), '公开收盘资产不能含私人台账字段');
        pending.push({ value: child, depth: current.depth + 1 });
      }
    }
  }
}
function score(value: unknown): boolean { return value === null || finite(value) && value >= 0 && value <= 100; }
function code(value: unknown): value is string { return typeof value === 'string' && /^\d{6}$/.test(value); }
function closedQuote(value: unknown, day: string, built: number): number {
  const stamp = closeStamp(value), local = new Date(stamp + OFFSET);
  requireClose(local.toISOString().slice(0, 10) === day && local.getUTCHours() * 60 + local.getUTCMinutes() >= 900
    && local.getUTCHours() < 18 && stamp <= built, '行情不是该交易日的完整收盘行情');
  return stamp;
}
export function validateCloseManifest(value: unknown, now = Date.now()): CloseManifest {
  publicTree(value);
  const data = closeObject(value);
  requireClose(data.schema_version === 1 && data.kind === CLOSE_KIND);
  const day = closeDay(data.score_date), built = closeStamp(data.built_at);
  requireClose(Number.isFinite(now) && built <= now + 60000 && validCalendar(data.trading_calendar));
  const clock = marketClock(Date.parse(day + 'T16:00:00+08:00'), data.trading_calendar);
  requireClose(clock && clock.open && clock.day === day, '收盘日期不在已核验交易日历内');
  closedQuote(data.quote_at, day, built);
  const universe = closeObject(data.universe);
  for (const key of ['market_total', 'eligible_total', 'attempted', 'scored', 'failed', 'excluded'])
    requireClose(Number.isSafeInteger(universe[key]) && Number(universe[key]) >= 0 && Number(universe[key]) <= MAX_CLOSE_CODES);
  requireClose(universe.market_total === Number(universe.eligible_total) + Number(universe.excluded)
    && universe.attempted === Number(universe.scored) + Number(universe.failed)
    && universe.attempted === universe.eligible_total, '收盘任务必须完成所有候选批次');
  const plan = closeObject(data.plan), fees = closeObject(data.fees);
  for (const [key, item] of Object.entries(plan)) {
    requireClose(/^(?:buy_|sell_)/.test(key) || key === 'reduce_ratio', '公开规则键无效');
    requireClose(finite(item));
  }
  for (const key of ['sell_stop_loss', 'sell_take_profit', 'reduce_ratio', 'buy_max_debt_equity', 'buy_max_capex_to_profit']) requireClose(finite(plan[key]));
  requireClose(Number(plan.buy_max_debt_equity) > 0 && Number(plan.buy_max_capex_to_profit) > 0);
  requireClose(Number(plan.sell_stop_loss) >= -1 && Number(plan.sell_stop_loss) < 0
    && Number(plan.sell_take_profit) > 0 && Number(plan.reduce_ratio) >= 0 && Number(plan.reduce_ratio) <= 1);
  for (const key of ['commission_rate', 'commission_min', 'stamp_duty_rate', 'transfer_fee_rate'])
    requireClose(finite(fees[key]) && Number(fees[key]) >= 0 && (key === 'commission_min' || Number(fees[key]) <= 1));
  requireClose(Array.isArray(data.index) && data.index.length > 0 && data.index.length <= MAX_CLOSE_CODES
    && data.index.length === universe.scored && Array.isArray(data.shards) && data.shards.length > 0 && data.shards.length <= MAX_CLOSE_CODES);
  const membership = new Map<string, string>(), paths = new Set<string>();
  for (const raw of data.shards) {
    const part = closeObject(raw);
    requireClose(typeof part.path === 'string' && new RegExp('^close-score-parts/' + day + '-[a-f0-9]{8,64}-[0-9]+\\.json$').test(part.path)
      && !paths.has(part.path) && typeof part.sha256 === 'string' && /^[a-f0-9]{64}$/.test(part.sha256)
      && Array.isArray(part.codes) && part.codes.length > 0 && part.codes.length <= 100);
    paths.add(part.path);
    for (const item of part.codes) { requireClose(code(item) && !membership.has(item)); membership.set(item, part.path); }
  }
  const found = new Set<string>();
  for (const raw of data.index) {
    const row = closeObject(raw);
    requireClose(code(row.code) && !found.has(row.code) && text(row.name, 100)
      && CLOSE_ACTIONS.includes(String(row.action)) && finite(row.total) && score(row.total) && typeof row.shard === 'string'
      && membership.get(row.code) === row.shard);
    found.add(row.code);
  }
  requireClose(found.size === membership.size, '收盘索引与分片覆盖不一致');
  return data as unknown as CloseManifest;
}

function validateChecks(value: unknown, buy: boolean): JsonObject[] {
  requireClose(Array.isArray(value) && value.length <= 40);
  const seen = new Set<string>(), checks: JsonObject[] = [];
  for (const raw of value) {
    const item = closeObject(raw);
    requireClose(text(item.key, 60) && !seen.has(item.key) && text(item.label, 500)
      && typeof item.hit === 'boolean' && typeof item.known === 'boolean' && (!item.hit || item.known)
      && (nullableNumber(item.value) || !buy && ['sell_roe_decline', 'sell_growth_negative'].includes(item.key) && text(item.value, 300))
      && (nullableNumber(item.threshold) || !buy && ['sell_roe_decline', 'sell_growth_negative'].includes(item.key) && text(item.threshold, 100)) && text(item.note, 1000)
      && (buy ? item.level === '' || item.level === undefined : ['清仓', '减仓', '预警'].includes(String(item.level))));
    if (!buy) requireClose(/^sell_/.test(item.key) && !['sell_stop_loss', 'sell_take_profit'].includes(item.key));
    if (!buy && /^(sell_debt_equity|sell_equity_nonpositive|sell_capex_to_profit|sell_annual_loss|sell_cumulative_loss)$/.test(item.key))
      requireClose(item.level === '预警');
    seen.add(item.key); checks.push(item);
  }
  if (buy) requireClose(BASE_BUY_KEYS.every(key => seen.has(key)), '收盘买入条件不完整');
  return checks;
}
function validateFinancial(entry: JsonObject, built: number, checks: JsonObject[], plan: Record<string, number>): void {
  requireClose(typeof entry.financial_capability === 'boolean');
  if (!entry.financial_capability) {
    requireClose(entry.financial_analysis === null || entry.financial_analysis === undefined);
    requireClose(checks.filter(item => ['debt_equity', 'capex_to_profit'].includes(String(item.key))).every(item => !item.hit));
    return;
  }
  const analysis = closeObject(entry.financial_analysis), metrics = closeObject(analysis.metrics);
  requireClose(['ok', 'partial', 'missing', 'stale', 'invalid', 'not_applicable'].includes(String(analysis.status)));
  requireClose(analysis.source === 'sina-finance-report' && analysis.currency === 'CNY' && analysis.unit === 'yuan'
    && analysis.statement_scope === 'consolidated');
  const fetched = analysis.fetched_at === null ? null : closeStamp(analysis.fetched_at);
  if (fetched !== null) requireClose(fetched <= built + 60000);
  const window = closeObject(analysis.window);
  requireClose(Number.isSafeInteger(window.start_year) && Number.isSafeInteger(window.end_year)
    && Number(window.end_year) - Number(window.start_year) === 4);
  requireClose(Array.isArray(analysis.years) && analysis.years.length <= 5);
  for (const key of ['debt_equity', 'capex_to_profit']) {
    const metric = closeObject(metrics[key]), check = checks.find(item => item.key === key)!;
    const threshold = plan[key === 'debt_equity' ? 'buy_max_debt_equity' : 'buy_max_capex_to_profit'];
    // 原财务优良标签与新增买入门槛是不同字段，不以标签代替配置中的买入阈值。
    const excellentThreshold = key === 'debt_equity' ? 0.8 : 0.25;
    requireClose(['ok', 'missing', 'stale', 'invalid', 'not_applicable'].includes(String(metric.status)) && nullableNumber(metric.value));
    requireClose(check.threshold === threshold);
    if (metric.status === 'ok') {
      requireClose(finite(metric.value) && metric.value >= 0 && metric.excellent === (metric.value < excellentThreshold));
      if (check.known) requireClose(check.value === metric.value, '买入条件的实际值必须与财务指标原值一致');
      requireClose(fetched !== null && built - fetched <= 86400000, '已核验财务指标必须有生成时有效的实际获取时间');
      if (key === 'debt_equity') requireClose(finite(metric.total_equity) && metric.total_equity > 0);
      else requireClose(finite(metric.net_profit_sum) && metric.net_profit_sum > 0
        && finite(metric.capex_sum) && metric.capex_sum >= 0
        && Math.abs(metric.value - metric.capex_sum / metric.net_profit_sum) <= 1e-12);
    }
    else requireClose(metric.excellent === null);
    if (check.hit) {
      const industry = String(entry.industry || '');
      const exempt = metric.status === 'not_applicable' && (/(银行|保险|证券|金融)/.test(industry) || key === 'debt_equity' && /房地产/.test(industry));
      requireClose(exempt || metric.status === 'ok' && finite(metric.value) && metric.value < threshold,
        '财务买入门槛不能用缺失或未达标值通过');
      if (exempt) requireClose(/豁免|不适用/.test(String(check.note)), '行业豁免必须明确说明');
    }
  }
}
export function validateClosePart(value: unknown, manifest: CloseManifest, descriptor: CloseShard): Record<string, CloseEntry> {
  publicTree(value);
  const data = closeObject(value);
  requireClose(data.schema_version === 1 && data.kind === CLOSE_PART_KIND && data.score_date === manifest.score_date);
  const rawEntries = closeObject(data.entries), codes = Object.keys(rawEntries);
  requireClose(codes.length === descriptor.codes.length && codes.length > 0 && codes.length <= 100
    && descriptor.codes.every(item => Object.hasOwn(rawEntries, item)), '收盘分片代码覆盖不一致');
  const index = new Map(manifest.index.map(item => [item.code, item])), built = closeStamp(manifest.built_at);
  const entries: Record<string, CloseEntry> = Object.create(null);
  for (const stockCode of codes) {
    const item = closeObject(rawEntries[stockCode]), row = index.get(stockCode);
    requireClose(row && row.shard === descriptor.path && item.code === stockCode && item.name === row.name
      && item.action === row.action && item.total === row.total && score(item.total)
      && finite(item.price) && item.price > 0);
    requireClose(closedQuote(item.quote_ts, manifest.score_date, built) <= closeStamp(manifest.quote_at),
      '单股行情时间不能晚于整个收盘快照的最晚行情时间');
    for (const key of ['quality', 'valuation', 'dividend', 'policy']) if (item[key] !== undefined) requireClose(score(item[key]));
    for (const key of ['pe', 'pb', 'dy']) requireClose(nullableNumber(item[key]));
    requireClose(text(item.industry, 100) && text(item.grade, 10));
    const buyChecks = validateChecks(item.buy_checks, true), sellChecks = validateChecks(item.sell_checks, false);
    validateFinancial(item, built, buyChecks, manifest.plan);
    if (item.action === '建仓') requireClose(buyChecks.every(check => check.known && check.hit), '建仓必须满足所有适用买入门槛');
    const snapshot = closeObject(item.sell_snapshot);
    for (const key of ['exit', 'reduce', 'warn', 'missing', 'missing_warn', 'financial_warn', 'financial_missing'])
      requireClose(Array.isArray(snapshot[key]) && snapshot[key].length <= 40 && snapshot[key].every(item => text(item, 500)));
    for (const [key, level] of [['exit', '清仓'], ['reduce', '减仓'], ['warn', '预警']]) {
      const expected = sellChecks.filter(check => check.level === level && check.known && check.hit).map(check => String(check.label)).sort();
      requireClose(JSON.stringify([...snapshot[key] as string[]].sort()) === JSON.stringify(expected), '收盘卖出条件与摘要不一致');
    }
    entries[stockCode] = item as CloseEntry;
  }
  return entries;
}

/** 公共收盘分与实时行情使用不同的时效；下一交易日开盘不清除前一收盘分。 */
export function closeFreshness(manifest: Pick<CloseManifest, 'score_date' | 'trading_calendar'>, now = Date.now()): 'ok' | 'delayed' {
  const clock = marketClock(now, manifest.trading_calendar);
  if (!clock) return 'delayed';
  const local = new Date(now + OFFSET), minute = local.getUTCHours() * 60 + local.getUTCMinutes();
  let expected = clock.day;
  if (!clock.open || minute < 900) {
    let stamp = Date.parse(clock.day + 'T00:00:00Z'), found = false;
    for (let i = 0; i < 20; i++) {
      stamp -= DAY;
      const candidate = new Date(stamp).toISOString().slice(0, 10);
      const check = marketClock(Date.parse(candidate + 'T16:00:00+08:00'), manifest.trading_calendar);
      if (!check) return 'delayed';
      if (check.open) { expected = candidate; found = true; break; }
    }
    if (!found) return 'delayed';
  }
  return manifest.score_date === expected ? 'ok' : 'delayed';
}
