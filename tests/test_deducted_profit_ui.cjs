/* 无网络、无DOM：执行实际财务表渲染器，核验独立来源、旧缓存及HTML转义。 */
const assert = require('node:assert/strict');
global.window = {__SITE__: {entries: {}, stock_weights: {quality: .3}}};
const {financialHTML} = require('../scripts/static/app.js');
const entry = {financial_analysis: {
  status: 'ok', window: {start_year: 2021, end_year: 2025, years: [2021,2022,2023,2024,2025]},
  years: [{fiscal_year: 2025, report_date: '2025-12-31', deducted_parent_net_profit: -45393719289.83,
    deducted_parent_evidence: {status: 'stale', fetched_at: '2026-11-01T10:00:00+08:00',
      supplier_disclosed_date: '2026-04-01'}}], metrics: {}, quality: {},
  source: 'sina-finance-report', fetched_at: '2026-11-06T10:00:00+08:00'
}};
let html = financialHTML(entry);
assert.match(html, /-453\.94/);
assert.match(html, /旧缓存\/已过期 · 新浪 NPCUT/);
assert.match(html, /获取：2026-11-01T10:00:00\+08:00/);
assert.match(html, /供应商公告日：2026-04-01（未独立核验）/);
const row = entry.financial_analysis.years[0];
row.deducted_parent_net_profit = null;
row.deducted_parent_evidence = {status: 'missing', reason: '<script>bad</script>'};
html = financialHTML(entry);
assert.match(html, /缺失 · &lt;script&gt;bad&lt;\/script&gt;/);
assert.ok(!html.includes('<script>bad</script>'));
row.deducted_parent_net_profit = 0;
row.deducted_parent_evidence = {status: 'ok', fetched_at: '2026-11-06T10:00:00+08:00'};
assert.match(financialHTML(entry), /0\.00<br><span class="small muted">新浪关键指标 NPCUT/);
// 兼容旧快照：没有新增证据结构仍可渲染。
delete row.deducted_parent_evidence;
assert.doesNotThrow(() => financialHTML(entry));
console.log('Deducted profit renderer checks passed');
