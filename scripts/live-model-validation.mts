import type { Model } from './live-scoring.mjs';
import { validCalendar, marketClock, calendarModelExpiry, closedBaseline } from './market-calendar.mjs';

const RULE_LEVELS: Record<string, string> = {
  sell_pe_percentile: '清仓', sell_pb_percentile: '清仓', sell_graham: '清仓',
  sell_pe_abs: '清仓', sell_dividend_yield: '清仓', sell_total: '清仓',
  sell_reduce_total: '减仓', sell_macro_score: '减仓', sell_macro_reduce_score: '减仓',
  sell_cashflow: '预警', sell_roe_decline: '预警', sell_growth_negative: '预警',
};
const FINANCIAL_RULE_LEVELS: Record<string, string> = {
  sell_debt_equity: '预警', sell_equity_nonpositive: '预警', sell_capex_to_profit: '预警',
  sell_annual_loss: '预警', sell_cumulative_loss: '预警',
};
const FINANCIAL_STATUSES = ['ok', 'partial', 'missing', 'stale', 'invalid', 'not_applicable'];
const QUALITY_KEYS = ['roe', 'cashflow', 'debt', 'stability', 'capex'];
const POOL_KEYS = ['pe_history', 'pb_history', 'pe_industry', 'pb_industry', 'pe_market', 'pb_market'];
const HOURS_36 = 36 * 3600000;
function requireValue(condition: unknown): asserts condition {
  if (!condition) throw new Error('公开评分基础数据格式无效或已过期');
}
function object(value: unknown): Record<string, unknown> {
  requireValue(value !== null && typeof value === 'object' && !Array.isArray(value));
  requireValue(Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
  return value as Record<string, unknown>;
}
function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}
function nullable(value: unknown): boolean { return value === null || finite(value); }
function text(value: unknown, max = 300): value is string {
  return typeof value === 'string' && value.length <= max;
}
function dateOnly(value: unknown): string {
  requireValue(typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value));
  const stamp = Date.parse(value + 'T00:00:00+08:00');
  requireValue(Number.isFinite(stamp) && new Date(stamp + 28800000).toISOString().slice(0, 10) === value);
  return value;
}
function isoStamp(value: unknown): number {
  requireValue(typeof value === 'string'
    && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value));
  const stamp = Date.parse(value);
  requireValue(Number.isFinite(stamp));
  // Date.parse 会归一化某些无效日期，先单独校验源日期。
  dateOnly(value.slice(0, 10));
  requireValue(Number(value.slice(11, 13)) < 24 && Number(value.slice(14, 16)) < 60 && Number(value.slice(17, 19)) < 60);
  return stamp;
}
function sourceStamp(value: unknown): number {
  if (typeof value === 'string' && value.includes('T')) return isoStamp(value);
  requireValue(typeof value === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?$/.test(value));
  return isoStamp(value.replace(' ', 'T') + (value.length === 16 ? ':00' : '') + '+08:00');
}
function weights(value: unknown, keys: string[]): void {
  const record = object(value);
  keys.forEach(key => requireValue(finite(record[key]) && record[key] >= 0));
  requireValue(keys.reduce((sum, key) => sum + Number(record[key]), 0) > 0);
}

function financialAnalysis(value: unknown, now: number, plan: Record<string, unknown>): void {
  const analysis = object(value);
  const status = (value: unknown) => requireValue(FINANCIAL_STATUSES.includes(String(value)));
  const optionalStamp = (value: unknown) => {
    if (value !== null) requireValue(isoStamp(value) <= now + 60000);
  };
  status(analysis.status);
  requireValue(analysis.source === 'sina-finance-report' && analysis.currency === 'CNY'
    && analysis.unit === 'yuan' && analysis.statement_scope === 'consolidated');
  optionalStamp(analysis.fetched_at);
  optionalStamp(analysis.source_updated_at);
  const window = object(analysis.window);
  requireValue(Number.isSafeInteger(window.start_year) && Number.isSafeInteger(window.end_year)
    && Number(window.start_year) >= 1990 && Number(window.end_year) <= new Date(now + 28800000).getUTCFullYear()
    && Number(window.end_year) - Number(window.start_year) === 4);
  requireValue(Array.isArray(window.years) && window.years.length === 5
    && window.years.every((year, index) => year === Number(window.start_year) + index));
  const years = window.years;
  requireValue(Array.isArray(analysis.years) && analysis.years.length <= 5);
  let previous = 0;
  for (const raw of analysis.years) {
    const row = object(raw);
    requireValue(Number.isSafeInteger(row.fiscal_year) && Number(row.fiscal_year) > previous
      && years.includes(row.fiscal_year));
    previous = Number(row.fiscal_year);
    requireValue(dateOnly(row.report_date) === `${row.fiscal_year}-12-31`);
    for (const key of ['revenue', 'operating_profit', 'net_profit', 'deducted_parent_net_profit',
      'operating_cash_flow', 'capex', 'total_liabilities', 'total_equity', 'net_margin', 'capex_to_profit']) {
      requireValue(nullable(row[key]));
    }
    optionalStamp(row.source_updated_at);
  }
  const metrics = object(analysis.metrics);
  for (const key of ['debt_equity', 'capex_to_profit', 'annual_net_profit', 'cumulative_net_profit']) {
    const metric = object(metrics[key]);
    status(metric.status);
    requireValue(nullable(metric.value));
    if (metric.status === 'ok') requireValue(finite(metric.value));
    if (key === 'debt_equity' || key === 'capex_to_profit') {
      requireValue(metric.excellent === null || typeof metric.excellent === 'boolean');
      // 非适用、过期或缺失均不能冒充已满足优良标准。
      if (metric.status !== 'ok') requireValue(metric.excellent === null);
      else requireValue(metric.excellent === (Number(metric.value) < Number(plan[
        key === 'debt_equity' ? 'sell_debt_equity' : 'sell_capex_to_profit'])));
    }
    if (key === 'debt_equity') {
      requireValue(nullable(metric.total_equity));
      if (metric.report_date !== null) dateOnly(metric.report_date);
    }
    if (key === 'capex_to_profit') {
      requireValue(nullable(metric.net_profit_sum) && nullable(metric.capex_sum));
    }
  }
  requireValue(Array.isArray(analysis.loss_years) && analysis.loss_years.length <= 5
    && new Set(analysis.loss_years).size === analysis.loss_years.length
    && analysis.loss_years.every(year => years.includes(year)));
  requireValue(Array.isArray(analysis.notes) && analysis.notes.length <= 30
    && analysis.notes.every(note => text(note, 1000)));
  const quality = object(analysis.quality);
  requireValue(finite(quality.coverage) && quality.coverage >= 0 && quality.coverage <= 1);
  requireValue(Array.isArray(quality.missing) && quality.missing.length <= 5
    && quality.missing.every(key => QUALITY_KEYS.includes(String(key))));
  for (const group of ['weights', 'sub_scores', 'contributions']) {
    const record = object(quality[group]);
    requireValue(Object.keys(record).length === QUALITY_KEYS.length);
    for (const key of QUALITY_KEYS) {
      requireValue(group === 'weights' ? finite(record[key]) && Number(record[key]) >= 0 :
        nullable(record[key]) && (record[key] === null || Number(record[key]) >= 0 && Number(record[key]) <= 100));
    }
  }
}

/** 网络 JSON 必须经过整体验证后才允许进入纯评分镜像。缺失值显式保留 null。 */
export function validateModel(value: unknown, now: number): Model {
  requireValue(Number.isFinite(now));
  const model = object(value);
  requireValue(model.schema_version === 1 && ['touzi-live-v1', 'touzi-live-v2'].includes(String(model.model_version))
    && model.provenance === 'python-score-one-live-inputs');
  const finance = model.model_version === 'touzi-live-v2';
  const rules = finance ? { ...RULE_LEVELS, ...FINANCIAL_RULE_LEVELS } : RULE_LEVELS;
  const built = isoStamp(model.built_at), expires = isoStamp(model.expires_at);
  const asOf = dateOnly(model.as_of), quote = sourceStamp(model.quote_at);
  const quoteISO = new Date(quote).toISOString();
  const calendar = model.trading_calendar;
  const hasCalendar = calendar !== undefined;
  let validationNow = now;
  if (hasCalendar) {
    requireValue(validCalendar(calendar) && marketClock(now, calendar) !== null);
    const expectedExpiry = calendarModelExpiry(built, quoteISO, calendar);
    requireValue(expectedExpiry !== null && expires === expectedExpiry);
    const atBuild = marketClock(built, calendar);
    requireValue(atBuild !== null && atBuild.quoteDay === asOf);
    // 已验证收盘基线跨休市保留，输入新鲜度仍以真实生成时刻核验。
    if (closedBaseline(built, quoteISO, now, calendar)) validationNow = built;
  }
  requireValue(built <= now + 60000 && validationNow - built <= HOURS_36
    && expires > now && expires > built && (hasCalendar || expires - built <= HOURS_36));
  requireValue(typeof model.quote_at === 'string' && model.quote_at.slice(0, 10) === asOf
    && quote <= now + 60000 && quote <= built + 5 * 60000 && validationNow - quote <= HOURS_36);
  requireValue(finite(model.quote_coverage) && model.quote_coverage >= .9 && model.quote_coverage <= 1);

  const settings = object(model.settings), valuation = object(settings.valuation), dividend = object(settings.dividend);
  weights(valuation, ['pe_weight', 'pb_weight', 'dividend_yield_weight']);
  weights(dividend, ['level_weight', 'years_weight', 'spread_weight']);
  weights(settings.stock_weights, ['valuation', 'quality', 'dividend', 'policy']);
  const stockWeights = object(settings.stock_weights);
  requireValue(Object.keys(stockWeights).every(key => ['valuation', 'quality', 'dividend', 'policy'].includes(key)));
  for (const key of ['primary_bonus', 'graham_number']) requireValue(finite(valuation[key]) && valuation[key] >= 0);
  const primary = object(valuation.industry_primary);
  requireValue(Object.keys(primary).length <= 1000);
  for (const [industry, metric] of Object.entries(primary)) {
    requireValue(text(industry) && ['pe', 'pb', 'dividend_yield'].includes(String(metric)));
  }
  for (const key of ['excellent_yield', 'full_credit_years', 'spread_over_bond']) {
    requireValue(finite(dividend[key]) && dividend[key] >= 0);
  }

  const plan = object(model.plan), fees = object(model.fees);
  for (const [key, value] of Object.entries(plan)) requireValue((key.startsWith('sell_') || key === 'reduce_ratio') && finite(value));
  for (const key of ['sell_pe_percentile', 'sell_pb_percentile', 'sell_graham', 'sell_pe_abs',
    'sell_dividend_yield', 'sell_total', 'sell_reduce_total', 'sell_macro_score', 'sell_macro_reduce_score',
    'sell_stop_loss', 'sell_take_profit', 'sell_cashflow_floor', 'sell_negative_growth_years', 'reduce_ratio']) {
    requireValue(finite(plan[key]));
  }
  for (const key of ['sell_pe_percentile', 'sell_pb_percentile', 'reduce_ratio']) requireValue(Number(plan[key]) >= 0 && Number(plan[key]) <= 1);
  for (const key of ['sell_total', 'sell_reduce_total', 'sell_macro_score', 'sell_macro_reduce_score']) requireValue(Number(plan[key]) >= 0 && Number(plan[key]) <= 100);
  requireValue(Number(plan.sell_stop_loss) >= -1 && Number(plan.sell_stop_loss) <= 0
    && Number(plan.sell_take_profit) >= 0 && Number.isSafeInteger(plan.sell_negative_growth_years)
    && Number(plan.sell_negative_growth_years) > 0);
  if (finance) {
    for (const key of Object.keys(FINANCIAL_RULE_LEVELS)) requireValue(finite(plan[key]) && Number(plan[key]) >= 0);
  }
  for (const key of ['commission_rate', 'stamp_duty_rate', 'transfer_fee_rate']) {
    requireValue(finite(fees[key]) && fees[key] >= 0 && fees[key] <= 1);
  }
  requireValue(finite(fees.commission_min) && fees.commission_min >= 0);

  const pools = object(model.pools);
  requireValue(Object.keys(pools).length <= 4000);
  let samples = 0;
  for (const [key, pool] of Object.entries(pools)) {
    requireValue(/^[a-f0-9]{24}$/.test(key) && Array.isArray(pool) && pool.length <= 10000);
    requireValue(pool.every(sample => finite(sample) && sample > 0));
    samples += pool.length;
  }
  requireValue(samples <= 1000000);
  const entries = object(model.entries), codes = Object.keys(entries);
  requireValue(codes.length > 0 && codes.length <= 600);
  for (const code of codes) {
    requireValue(/^\d{6}$/.test(code));
    const input = object(entries[code]);
    requireValue(input.code === code && text(input.name) && text(input.industry));
    requireValue(typeof input.input_as_of === 'string' && input.input_as_of.length <= 40);
    const inputDate = dateOnly(input.input_as_of.slice(0, 10));
    // 跨日盘前允许用前一日收盘，基础输入截止日仍可为本轮构建日期。
    const inputAt = Date.parse(inputDate + 'T00:00:00+08:00');
    requireValue(inputDate >= asOf && inputAt <= validationNow + 60000 && validationNow - inputAt <= HOURS_36);
    object(input.provenance);
    if (finance) financialAnalysis(input.financial_analysis, validationNow, plan);
    const vin = object(input.valuation_input), din = object(input.dividend_input);
    requireValue(vin.code === code && din.code === code && vin.industry === input.industry);
    for (const key of ['pe_ttm', 'pb', 'dividend_yield', 'pe_percentile_ts', 'pb_percentile_ts',
      'dy_percentile_ts', 'pe_percentile_cs', 'pb_percentile_cs', 'dy_percentile_cs',
      'pe_percentile_industry', 'pb_percentile_industry']) requireValue(nullable(vin[key]));
    for (const [key, datum] of Object.entries(vin)) {
      if (key.includes('percentile') && datum !== null) requireValue(finite(datum) && datum >= 0 && datum <= 1);
    }
    for (const key of ['dps_ttm', 'price', 'dividend_yield', 'bond_yield', 'dividend_times', 'finance_times']) requireValue(nullable(din[key]));
    requireValue((din.dps_ttm === null || Number(din.dps_ttm) >= 0)
      && Number.isSafeInteger(din.consecutive_years) && Number(din.consecutive_years) >= 0);
    const fixed = object(input.fixed_parts), history = object(input.history_live), refs = object(input.pool_refs);
    for (const key of ['quality', 'policy']) requireValue(nullable(fixed[key])
      && (fixed[key] === null || Number(fixed[key]) >= 0 && Number(fixed[key]) <= 100));
    for (const key of ['pe', 'pb']) requireValue(typeof history[key] === 'boolean');
    for (const key of POOL_KEYS) requireValue(typeof refs[key] === 'string' && Object.hasOwn(pools, refs[key]));
    requireValue(Array.isArray(input.sell_checks) && input.sell_checks.length === Object.keys(rules).length);
    const seen = new Set<string>();
    for (const raw of input.sell_checks) {
      const check = object(raw);
      requireValue(typeof check.key === 'string' && Object.hasOwn(rules, check.key) && !seen.has(check.key));
      seen.add(check.key);
      requireValue(check.level === rules[check.key] && text(check.label, 500)
        && typeof check.hit === 'boolean' && typeof check.known === 'boolean' && (check.known || !check.hit));
      if (!['sell_roe_decline', 'sell_growth_negative'].includes(check.key)) {
        const planKey = check.key === 'sell_cashflow' ? 'sell_cashflow_floor' : check.key;
        requireValue(finite(check.threshold) && check.threshold === plan[planKey]);
      } else requireValue(text(check.threshold));
      if (Object.hasOwn(FINANCIAL_RULE_LEVELS, check.key)) {
        requireValue(nullable(check.value) && text(check.note, 1000));
        if (check.known) {
          const analysis = object(input.financial_analysis), metrics = object(analysis.metrics);
          requireValue(finite(check.value) && analysis.fetched_at !== null
            && validationNow - isoStamp(analysis.fetched_at) <= HOURS_36
            && !['stale', 'missing'].includes(String(analysis.status)));
          const metricKey = { sell_debt_equity: 'debt_equity', sell_equity_nonpositive: 'debt_equity',
            sell_capex_to_profit: 'capex_to_profit', sell_annual_loss: 'annual_net_profit',
            sell_cumulative_loss: 'cumulative_net_profit' }[check.key];
          const metric = object(metrics[metricKey!]);
          requireValue(check.key === 'sell_equity_nonpositive' ?
            ['ok', 'invalid'].includes(String(metric.status)) && finite(metric.total_equity) : metric.status === 'ok');
        }
      }
    }
  }
  // 上方逐字段检查与 Model 契约一致，计算模块不会接触未验证的网络对象。
  return model as Model;
}
