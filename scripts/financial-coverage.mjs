// 可用指标、行业适用性和展示字段完整性分别统计，不将行情覆盖冒充财报覆盖。
/** @param {Record<string, any>} entries */
export function financialCoverage(entries) {
  const result = {total:0, reports:{}, metrics:{}, quality:{full:0,partial:0,missing:0},
    fields:{expected:0,deducted_parent_missing:0}, reasons:{industry_unknown:0,amounts_missing:0}};
  for (const entry of Object.values(entries || {})) {
    result.total++;
    const a = entry.financial_analysis || {}, status = a.status || 'missing';
    result.reports[status] = (result.reports[status] || 0) + 1;
    for (const key of ['debt_equity','capex_to_profit','annual_net_profit','cumulative_net_profit']) {
      const state = (a.metrics && a.metrics[key] || {}).status || 'missing';
      const bucket = result.metrics[key] || (result.metrics[key] = {});
      bucket[state] = (bucket[state] || 0) + 1;
    }
    const covered = a.quality && a.quality.coverage;
    result.quality[covered === 1 ? 'full' : Number.isFinite(covered) && covered > 0 ? 'partial' : 'missing']++;
    result.fields.expected += 5;
    const years = a.years || [];
    result.fields.deducted_parent_missing += 5 - years.slice(0,5).filter(row => Number.isFinite(row.deducted_parent_net_profit)).length;
    if (!String(entry.industry || '').trim()) result.reasons.industry_unknown++;
    if (years.length !== 5 || years.some(row => ['revenue','operating_profit','net_profit','operating_cash_flow','capex'].some(key => !Number.isFinite(row[key])))) result.reasons.amounts_missing++;
  }
  return result;
}
