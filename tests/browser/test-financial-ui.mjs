import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { browserTestRuntimes } from './browser-test-runtime.mjs';
const runtimes = browserTestRuntimes(import.meta.url);

// 明确为确定性样本，不代表任何实际公司的财报或卖出结论。
const now = Date.parse('2026-10-23T10:00:00+08:00');
const analysis = {
  status: 'ok', source: 'sina-finance-report', currency: 'CNY', unit: 'yuan', statement_scope: 'consolidated',
  fetched_at: '2026-10-23T09:00:00+08:00', source_updated_at: null,
  window: { start_year: 2021, end_year: 2025, years: [2021, 2022, 2023, 2024, 2025] },
  years: [2021, 2022, 2023, 2024, 2025].map(fiscal_year => ({ fiscal_year, report_date: `${fiscal_year}-12-31`,
    revenue: 1000000000, operating_profit: 300000000, net_profit: 200000000, deducted_parent_net_profit: null,
    operating_cash_flow: 250000000, capex: 0, net_margin: 0.2, capex_to_profit: 0 })),
  metrics: { debt_equity: { value: 0.799999, status: 'ok', excellent: true, report_date: '2026-06-30' },
    capex_to_profit: { value: 0, status: 'ok', excellent: true, net_profit_sum: 1000000000, capex_sum: 0 } },
  quality: { coverage: 1, missing: [], weights: { roe: .25, cashflow: .2, debt: .2, stability: .2, capex: .15 },
    sub_scores: { roe: 80, cashflow: 80, debt: 100, stability: 80, capex: 100 },
    contributions: { roe: 20, cashflow: 16, debt: 20, stability: 16, capex: 15 } },
  notes: ['<script>alert(1)</script>'], loss_years: [],
};
const entry = { name: '测试样本', financial_analysis: analysis };
function harness(path, dom = false) {
  const nodes = {}, main = { appendChild(node) { nodes[node.id] = node; } };
  function node() {
    return { value: '', _html: '', style: {}, children: [], handlers: {},
      get innerHTML() { return this._html; },
      set innerHTML(value) { this._html = value; this.children = []; },
      addEventListener(type, handler) { this.handlers[type] = handler; this[type] = handler; },
      appendChild(child) { this.children.push(child); }, focus() { this.focused = true; } };
  }
  const select = node(); select.value = '600519';
  nodes.__financialcode = select;
  nodes.__financialbody = {
    _html: '', details: [],
    get innerHTML() { return this._html; },
    set innerHTML(value) {
      this._html = value;
      // 模拟真实DOM重绘：旧details节点被替换，新节点默认折叠。
      this.details = ['coverage', 'quality'].filter(key => value.includes('data-financial-detail="' + key + '"'))
        .map(key => ({open: false, getAttribute() { return key; }}));
    },
    querySelectorAll() { return this.details; },
  };
  for (const id of ['__financialsearch', '__financialform', '__financialresults', '__financialmessage']) { nodes[id] = node(); }
  const context = { module: { exports: {} }, window: { __SITE__: {
    stock_weights: { quality: .3 }, entries: { '600519': entry, '000001': { name: '缺失样本' } } } } };
  if (dom) { context.document = { readyState: 'loading', addEventListener() {},
    getElementById(id) { return nodes[id] || null; }, querySelector() { return main; },
    createElement() { return node(); } }; }
  runInNewContext(readFileSync(path, 'utf8'), context);
  return { api: context.module.exports, nodes, select, context };
}
for (const { file: path } of runtimes) {
  const h = harness(path, true), api = h.api;
  let html = api.financialHTML(entry);
  assert.match(html, /2021—2025/);
  assert.match(html, /0\.80 倍<\/strong> · 优良/, '使用后端未舍入判定，不拿显示0.80重新比较');
  assert.match(html, /0\.00%<\/strong> · 达标/);
  assert.match(html, />10\.00<\/td>/, '人民币元转亿元');
  assert.match(html, />20\.00%<\/td>/);
  assert.match(html, /暂无数据/);
  assert.match(html, /来源更新时间：未知/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, />4\.50<\/td>/, '15质量分乘实际30%质量权重');
  assert.equal((html.match(/scope="row">202/g) || []).length, 5);
  assert.match(api.financialHTML({}), /无法判断/);
  const exempt = structuredClone(entry);
  exempt.financial_analysis.metrics.debt_equity = { status: 'not_applicable', value: null, excellent: null };
  exempt.financial_analysis.status = 'stale';
  exempt.financial_analysis.loss_years = [2023];
  assert.match(api.financialHTML(exempt), /行业不适用/);
  assert.match(api.financialHTML(exempt), /旧缓存\/已过期/);
  assert.match(api.financialHTML(exempt), /亏损年份：2023/);
  api.renderFinancials();
  assert.match(h.nodes.__financialbody.innerHTML, /2021—2025/);
  assert.match(h.nodes.__financialbody.innerHTML, /数据不足 1只/);
  assert.match(h.nodes.__financialbody.innerHTML, /旧数据 0只/);
  assert.match(h.nodes.__financialbody.innerHTML, /全部缺失 1只/);
  h.nodes.__financialbody.details.forEach(detail => { detail.open = true; });
  api.renderFinancials();
  assert.equal(h.nodes.__financialbody.details.length, 2);
  assert.ok(h.nodes.__financialbody.details.every(detail => detail.open), '定时刷新保留财报覆盖和质量明细展开状态');
  h.select.value = '000001'; h.select.change.call(h.select);
  assert.match(h.nodes.__financialbody.innerHTML, /尚无企业财务数据/);
  api.renderFinancials();
  assert.equal(h.select.value, '000001', '定时刷新保留用户所选股票');
  const input = h.nodes.__financialsearch, results = h.nodes.__financialresults;
  const message = h.nodes.__financialmessage;
  const submit = () => h.nodes.__financialform.submit({ preventDefault() {} });
  input.value = '测试'; input.input();
  assert.equal(results.children.length, 1, '中文部分名称可检索');
  assert.equal(h.select.value, '000001', '只输入不擅自切换当前股票');
  submit();
  assert.equal(h.select.value, '600519', '唯一匹配确定切换');
  input.value = '样本'; input.input(); submit();
  assert.equal(results.children.length, 2, '多个匹配需明确选择');
  results.children[0].click();
  assert.equal(h.select.value, '000001');
  assert.equal(input.focused, true);
  input.value = '600519'; input.compositionstart(); input.input(); submit();
  assert.equal(h.select.value, '000001', '中文组合输入时不提交');
  let prevented = false;
  input.keydown({ key: 'Enter', isComposing: true, preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  input.compositionend(); submit();
  assert.equal(h.select.value, '600519', '代码检索保留前导零语义且可确认');
  input.value = '不存在的企业'; input.input(); submit();
  assert.match(message.textContent, /没有匹配结果/);
  assert.equal(h.select.value, '600519', '无匹配不改变已选股票');
  input.value = '缺失样本'; input.input();
  api.renderFinancials();
  assert.equal(input.value, '缺失样本', '刷新保留正在输入的查询');
  assert.equal(h.select.value, '600519');
  assert.equal(results.children.length, 1);
  submit();
  assert.equal(h.select.value, '000001', '六位代码前导零保留');
  input.value = '样本'; input.input();
  h.context.window.__SITE__.entries['000002'] = { name: '新增样本' };
  api.renderFinancials();
  assert.equal(input.value, '样本', '模型覆盖集合变化也保留查询');
  assert.equal(h.select.value, '000001', '模型覆盖集合变化不重置已有选择');
  assert.equal(results.children.length, 3, '候选列表随新的覆盖集合更新');
  assert.match(h.nodes.__financials.innerHTML, /每年5月1日/);
}
const cloudRuntime = runtimes.find(runtime => runtime.cloud);
if (cloudRuntime) {
const h = harness(cloudRuntime.file);
const api = h.api;
api.monitorState.model = 'touzi-live-v2';
const check = (hit, label = '债务股权比率达到阈值') => ({ key: 'sell_debt_equity', known: true, hit, label });
const stock = { ...entry, financial_capability: true, __live_model: 'touzi-live-v2', price: 10,
  quote_ts: '2026-10-23T10:00:00+08:00', sell_checks: [check(true)],
  sell_snapshot: { financial_warn: ['债务股权比率达到阈值'], financial_missing: [] } };
const positions = [{ '代码': '600519', '名称': '测试样本' }];
const assess = (old, e = stock, fresh = true) => api.reconcileFinancialAlerts(old, positions, { '600519': e }, now, fresh);
let result = assess({});
assert.equal(result.changed.length, 1);
assert.equal(result.alerts['600519'].pending, false);
result = assess(result.alerts, { ...stock, sell_checks: [check(true, '债务股权比率新金额或报告期')] });
assert.equal(result.changed.length, 0, '同一规则金额或标签变化不重复提醒');
const old = result.alerts;
result = assess(old, { ...stock, financial_capability: false });
assert.equal(result.alerts['600519'].pending, true);
assert.equal(result.changed.length, 0);
result = assess(result.alerts, { ...stock, sell_checks: [check(false)], sell_snapshot: { financial_warn: [], financial_missing: [] } });
assert.equal(Object.keys(result.alerts).length, 0, '有效恢复包括确认行业豁免时清除旧财务提醒');
result = assess(result.alerts);
assert.equal(result.changed.length, 1, '确认恢复后再次触发');
assert.equal(Object.keys(api.reconcileFinancialAlerts(result.alerts, [], {}, now, true).alerts).length, 0);
assert.equal(assess({}, { ...stock, sell_checks: [], sell_snapshot: { financial_warn: [], financial_missing: ['未知'] } }).changed.length, 0);
}
console.log(`financial UI: ${runtimes.map(runtime => runtime.cloud ? 'cloud' : 'static').join('+')}, five-year units/missing/precision/source/coverage, DOM selection and applicable warning regressions passed`);
