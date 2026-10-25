import { calendarQuoteFresh } from './market-calendar.mjs';
// Python touzi/signals/{valuation,dividend}.py 的唯一服务端镜像。
export type Numeric = number | null;
export type Check = { key: string; label: string; hit: boolean; known: boolean; level: string; threshold: unknown; value: unknown; note: string };
export type LiveInput = {
  code: string; name: string; industry: string; input_as_of: string;
  valuation_input: Record<string, unknown>; dividend_input: Record<string, unknown>;
  fixed_parts: Record<string, Numeric>; history_live: Record<string, boolean>;
  pool_refs: Record<string, string>; sell_checks: Check[]; provenance: Record<string, unknown>;
  financial_analysis?: Record<string, unknown>;
};
export type Model = {
  schema_version: number; model_version: string; built_at: string; expires_at: string;
  as_of: string; quote_at: string; quote_coverage: number; provenance: string;
  trading_calendar?: unknown;
  settings: { valuation: Record<string, unknown>; dividend: Record<string, unknown>; stock_weights: Record<string, number> };
  entries: Record<string, LiveInput>; pools: Record<string, number[]>;
  plan: Record<string, number>; fees: Record<string, number>;
};
export type Quote = { code: string; price: number; pe: Numeric; pb: Numeric; quote_ts: string };
export const FINANCIAL_RULES = ['sell_debt_equity', 'sell_equity_nonpositive', 'sell_capex_to_profit',
  'sell_annual_loss', 'sell_cumulative_loss'] as const;
export const FINANCIAL_METRICS: Record<string, string> = {
  sell_debt_equity: 'debt_equity', sell_equity_nonpositive: 'debt_equity',
  sell_capex_to_profit: 'capex_to_profit', sell_annual_loss: 'annual_net_profit',
  sell_cumulative_loss: 'cumulative_net_profit',
};
const finite = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x);
const num = (x: unknown): Numeric => finite(x) ? x : null;
const clamp = (x: Numeric): Numeric => x === null ? null : Math.min(100, Math.max(0, x));
export function percentile(value: Numeric, pool: number[]): Numeric {
  const valid = pool.filter(x => finite(x) && x > 0);
  return value === null || value <= 0 || valid.length < 5 ? null : valid.filter(x => x < value).length / valid.length;
}
export function piecewise(x: number, points: number[][]): number {
  const p = [...points].sort((a, b) => a[0] - b[0]);
  if (x <= p[0][0]) return p[0][1];
  if (x >= p[p.length - 1][0]) return p[p.length - 1][1];
  for (let i = 1; i < p.length; i++) {
    const [x0, y0] = p[i - 1], [x1, y1] = p[i];
    if (x0 <= x && x <= x1) return x1 === x0 ? y1 : y0 + (x - x0) / (x1 - x0) * (y1 - y0);
  }
  return p[p.length - 1][1];
}
// Python round(float, digits) 使用实际二进制浮点值与 ties-to-even。
export function pyRound(x: number, digits: number): number {
  if (!finite(x) || x === 0) return x;
  const buffer = new ArrayBuffer(8), view = new DataView(buffer);
  view.setFloat64(0, Math.abs(x));
  const bits = view.getBigUint64(0), one = BigInt(1);
  const exp = Number((bits >> BigInt(52)) & BigInt(2047));
  let numerator = bits & ((one << BigInt(52)) - one);
  if (exp) numerator += one << BigInt(52);
  numerator *= BigInt(10) ** BigInt(digits);
  const power = (exp ? exp - 1023 : -1022) - 52;
  const denominator = power < 0 ? one << BigInt(-power) : one;
  if (power > 0) numerator <<= BigInt(power);
  let quotient = numerator / denominator;
  const doubled = numerator % denominator * BigInt(2);
  if (doubled > denominator || (doubled === denominator && quotient % BigInt(2) === one)) quotient += one;
  return Math.sign(x) * Number(quotient) / 10 ** digits;
}
const PCTL = [[0,100],[.1,95],[.25,85],[.4,72],[.5,63],[.6,52],[.75,35],[.9,15],[1,0]];
function weighted(values: Record<string, Numeric>, weights: Record<string, unknown>, positiveOnly = false): Numeric {
  let sum = 0, weight = 0;
  for (const [key, score] of Object.entries(values)) {
    const w = num(weights[key]);
    if (score !== null && w !== null && (!positiveOnly || w > 0)) { sum += score * w; weight += w; }
  }
  return weight > 0 ? sum / weight : null;
}
export function computeEntry(model: Model, input: LiveInput, quote: Quote) {
  const v = model.settings.valuation, d = model.settings.dividend;
  const din = input.dividend_input, vin = input.valuation_input;
  const pool = (k: string) => model.pools[input.pool_refs[k]];
  const dy = num(din.dps_ttm) === null ? null : Number(din.dps_ttm) / quote.price * 100;
  const detail: Record<string, unknown> = {};
  const metricScore = (key: 'pe' | 'pb', value: Numeric): Numeric => {
    if (key === 'pe' && value !== null && value <= 0) return 0;
    if (value === null || value <= 0) return null;
    const history = input.history_live[key] ? percentile(value, pool(key + '_history')) : num(vin[key + '_percentile_ts']);
    const options: [Numeric, string][] = [[history, '个股5年历史'],
      [percentile(value, pool(key + '_industry')), '行业内截面'],
      [percentile(value, pool(key + '_market')), '全市场截面']];
    const chosen = options.find(([value]) => value !== null);
    if (!chosen) return null;
    detail[key + '_percentile'] = pyRound(chosen[0]!, 4);
    detail[key + '_percentile_source'] = chosen[1];
    return piecewise(chosen[0]!, PCTL);
  };
  const peScore = metricScore('pe', quote.pe), pbScore = metricScore('pb', quote.pb);
  const dyPct = num(vin.dy_percentile_ts) ?? num(vin.dy_percentile_cs);
  const dyScore = dy !== null && dy > 0 ? (dyPct !== null ? piecewise(1 - dyPct, PCTL) :
    piecewise(dy, [[0,0],[1,30],[2,55],[3,75],[4.5,95],[6,100]])) : 0;
  let valuation = weighted({ pe: peScore, pb: pbScore, dy: dyScore },
    { pe: v.pe_weight, pb: v.pb_weight, dy: v.dividend_yield_weight });
  const primary = (v.industry_primary as Record<string, unknown>)[input.industry];
  const primaryScore = primary === 'pe' ? peScore : primary === 'pb' ? pbScore : primary === 'dividend_yield' ? dyScore : null;
  if (primaryScore !== null && valuation !== null) valuation = clamp(valuation + Number(v.primary_bonus || 0) * 50);
  valuation = clamp(valuation);
  if (quote.pe !== null && quote.pe > 0 && quote.pb !== null && quote.pb > 0) detail.graham_product = pyRound(quote.pe * quote.pb, 2);
  const level = piecewise(dy ?? 0, [[0,0],[1,20],[2,45],[3,68],[4,86],[Number(d.excellent_yield),100],[12,100]]);
  const linear = (x: number, good: number) => good === 0 ? 50 : Math.min(100, Math.max(0, x / good * 100));
  const years = linear(Number(din.consecutive_years || 0), Number(d.full_credit_years));
  const bond = num(din.bond_yield);
  const spread = bond === null ? null : linear((dy ?? 0) - bond, Number(d.spread_over_bond) * 3);
  const penalty = finite(din.finance_times) && finite(din.dividend_times) && din.finance_times > din.dividend_times ? 12 : 0;
  const divRaw = weighted({ level, years, spread }, { level: d.level_weight, years: d.years_weight, spread: d.spread_weight });
  const dividend = clamp(divRaw === null ? null : divRaw - penalty);
  const parts = { valuation, quality: input.fixed_parts.quality, dividend, policy: input.fixed_parts.policy };
  const total = clamp(weighted(parts, model.settings.stock_weights, true));
  const grade = total === null ? 'N/A' : total >= 80 ? 'A+' : total >= 72 ? 'A' : total >= 62 ? 'B' : total >= 50 ? 'C' : 'D';
  const checks = input.sell_checks.map(check => {
    let value: unknown = check.value, known = check.known, hit = check.hit, note = check.note;
    const key = check.key, thr = Number(check.threshold);
    if (key === 'sell_pe_percentile' || key === 'sell_pb_percentile') {
      const metric = key === 'sell_pe_percentile' ? 'pe' : 'pb';
      value = num(detail[metric + '_percentile']);
      known = detail[metric + '_percentile_source'] === '个股5年历史' && value !== null;
      hit = known && Number(value) >= thr;
      note = detail[metric + '_percentile_source'] === '个股5年历史' ? '' : '自身历史分位缺失，不能用截面分位触发清仓';
    } else if (['sell_graham','sell_pe_abs','sell_dividend_yield','sell_total','sell_reduce_total'].includes(key)) {
      value = key === 'sell_graham' ? num(detail.graham_product) : key === 'sell_pe_abs' ? quote.pe : key === 'sell_dividend_yield' ? dy : total;
      known = value !== null;
      hit = known && (key === 'sell_pe_abs' ? Number(value) > 0 && Number(value) >= thr :
        key === 'sell_graham' ? Number(value) >= thr : key === 'sell_dividend_yield' ? Number(value) < thr : Number(value) <= thr);
      note = key === 'sell_pe_abs' && value !== null && Number(value) <= 0 ? 'PE 为负（亏损）' :
        key === 'sell_dividend_yield' && value === null ? '股息率数据缺失，不能据此认定已不分红' : '';
    }
    if (!known && !note) note = '数据缺失，未核验';
    return { ...check, value, hit, known, note };
  });
  const select = (level: string) => checks.filter(c => c.known && c.hit && c.level === level).map(c => c.label);
  const financialMetrics = input.financial_analysis?.metrics as Record<string, { status: string }> | undefined;
  const sell_snapshot = { exit: select('清仓'), reduce: select('减仓'), warn: select('预警'),
    missing: checks.filter(c => !c.known && c.level !== '预警').map(c => c.label),
    missing_warn: checks.filter(c => !c.known && c.level === '预警').map(c => c.label),
    financial_warn: checks.filter(c => FINANCIAL_RULES.some(key => key === c.key) && c.known && c.hit).map(c => c.label),
    financial_missing: checks.filter(c => FINANCIAL_RULES.some(key => key === c.key) && !c.known
      && financialMetrics?.[FINANCIAL_METRICS[c.key]]?.status !== 'not_applicable').map(c => c.label) };
  return { ...parts, total, grade, pe: quote.pe, pb: quote.pb, dy, price: quote.price,
    name: input.name, industry: input.industry, quote_ts: quote.quote_ts,
    valuation_detail: detail, sell_snapshot, sell_checks: checks, input_as_of: input.input_as_of, provenance: input.provenance,
    // 旧模型继续计算行情分，但不冒充已经提供新财务分析。
    financial_capability: model.model_version === 'touzi-live-v2',
    financial_analysis: model.model_version === 'touzi-live-v2' ? input.financial_analysis : null };
}

export function quoteFresh(quote: Quote, now: number, calendar?: unknown): 'trading' | 'break' | 'closed' | null {
  if (calendar !== undefined) return calendarQuoteFresh(quote, now, calendar) as 'trading' | 'break' | 'closed' | null;
  const stamp = Date.parse(quote.quote_ts), age = now - stamp;
  if (!Number.isFinite(stamp) || age < -60000 || quote.price <= 0) return null;
  const local = new Date(now + 28800000), q = new Date(stamp + 28800000);
  const date = local.toISOString().slice(0, 10), qdate = q.toISOString().slice(0, 10);
  const minute = local.getUTCHours() * 60 + local.getUTCMinutes();
  const qm = q.getUTCHours() * 60 + q.getUTCMinutes(), day = local.getUTCDay();
  if (date === qdate && day >= 1 && day <= 5) {
    if ((minute >= 570 && minute < 690) || (minute >= 780 && minute < 900)) return age <= 120000 ? 'trading' : null;
    if (minute >= 690 && minute < 780) return qm >= 688 && qm <= 690 ? 'break' : null;
    if (minute >= 900) return qm >= 900 ? 'closed' : null;
  }
  // 不伪造交易日历：只允许前一自然日收盘，节假日旧报价停止核验。
  const previous = new Date(now + 28800000 - 86400000).toISOString().slice(0, 10);
  return (minute < 570 || day === 0 || day === 6) && qdate === previous && qm >= 900 ? 'closed' : null;
}
