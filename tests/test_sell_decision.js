const assert = require('node:assert/strict');

Date.now = () => Date.parse('2026-10-21T02:00:00Z');

global.window = {
  __SITE__: {
    as_of: new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10),
    quote_at: new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 16).replace('T', ' '),
    quote_coverage: 1,
    plan: { sell_pe_percentile: 0.8, sell_pb_percentile: 0.8, sell_stop_loss: -0.25, sell_take_profit: 1.5, reduce_ratio: 0.5 },
    fees: { commission_rate: 0.00025, commission_min: 5, stamp_duty_rate: 0.0005, transfer_fee_rate: 0.00001 },
    entries: {
      '000001': { name: '测试股票', sell_snapshot: { exit: [], reduce: [], warn: [], missing: [],
        missing_warn: ['净利润增速连续 2 个年报为负'] } },
      '000002': { name: '公开清仓', sell_snapshot: { exit: ['总分 ≤ 62'], reduce: [], warn: [], missing: [] } },
      '000003': { name: '公开减仓', sell_snapshot: { exit: [], reduce: ['总分 ≤ 67'], warn: [], missing: [] } },
      '000005': { name: '缺失数据', sell_snapshot: { exit: [], reduce: [], warn: [], missing: ['PE 自身分位'] } },
      '000004': { name: '待核验' }
    }
  }
};

for (const code of ['000001', '000002', '000003']) {
  (window.__SITE__.thr ||= {})[code] = {source:'个股5年历史',as_of:'2026-10-21',
    pe:{known:true,boundary:20,samples:21,percentile:0.8},
    pb:{known:true,boundary:2,samples:21,percentile:0.8}};
}

const { oneRow, summarize, sellDecision, valuationQuoteValid, parseQuoteText } = require('../scripts/static/app.js');

function row(code, price, buy = 10) {
  return oneRow({ '代码': code, '名称': '', '买入价': buy, '股数': 100, '买入日期': '2026-10-12' },
    price == null ? null : { price, code, pe_ttm:10, pb:1, quote_ts:'2026-10-21 10:00:00' });
}

assert.equal(row('000001', 10).action, '一切照旧');
assert.match(row('000001', 10).alert, /预警数据待核验/);
assert.equal(row('000001', 10).level, 'watch');
assert.equal(row('000001', 7.5).action, '清仓');
assert.equal(row('000001', 7.5).sellShares, 100);
assert.match(row('000001', 7.5).alert, /相对买入价浮亏/);
assert.equal(row('000001', 25).action, '减仓');
assert.equal(row('000001', 25).sellShares, 50);
assert.equal(row('000002', 10).action, '清仓');
assert.equal(row('000002', null).level, 'act');
assert.equal(row('000003', 7.5).action, '清仓');
assert.equal(row('000004', 10).action, '无法评估');
assert.equal(row('000005', 10).action, '无法评估');
assert.equal(row('000001', null).action, '无法评估');
assert.equal(summarize([row('000001', 10), row('000002', 10), row('000004', 10)])['需要动手'], 1);
console.log('sell decision tests passed');

const site = window.__SITE__;
const h = {'代码':'000001', '买入价':10};
const labels = {sell_pe_percentile:'PE 回到自身历史分位 ≥ 80%',
  sell_pb_percentile:'PB 回到自身历史分位 ≥ 80%'};
const e = {sell_snapshot:{exit:[],reduce:[],warn:[],missing:[],percentile_labels:labels}};
const q = {code:'000001',price:10,pe_ttm:10,pb:1,quote_ts:'2026-10-21 10:00:00'};
const decision = (extra = {}, entry = e) => sellDecision(h, entry, {...q, ...extra});
assert.equal(decision().action, '一切照旧');
assert.equal(decision({pe_ttm:20,pb:2}).action, '一切照旧'); // 严格 >
assert.equal(decision({pe_ttm:20.000001}).action, '清仓');
assert.equal(decision({pb:2.000001}).action, '清仓');
for (const extra of [{pe_ttm:null},{pe_ttm:undefined},{pe_ttm:-10},{pe_ttm:0},
  {pe_ttm:Infinity},{pe_ttm:NaN},{pb:null},{pb:-1},{pb:Infinity},
  {code:'000002'},{code:undefined},{price:0},{price:Infinity},
  {quote_ts:''},{quote_ts:'2026-10-20 15:00:00'},
  {quote_ts:'2026-10-21 10:05:00'},{quote_ts:'2026-02-30 10:00:00'},
  {quote_ts:'2026-10-21 09:40:00'}]) {
  const d = decision(extra);
  assert.equal(d.action, '无法评估', JSON.stringify(extra));
  assert.match(d.warn, /实时判断无法完成/);
}
// 一项未知不阻止另一项触发；个人止损/止盈及其他快照仍工作。
assert.equal(decision({pe_ttm:-1,pb:3}).action,'清仓');
assert.equal(decision({pe_ttm:null,price:7.5}).action,'清仓');
assert.equal(decision({pb:null,price:25}).action,'减仓');
const snapExit = {sell_snapshot:{...e.sell_snapshot,exit:[labels.sell_pe_percentile,'总分 ≤ 62']}};
assert.equal(decision({},snapExit).triggers,'总分 ≤ 62');
const oldValuation = {sell_snapshot:{...e.sell_snapshot,exit:[labels.sell_pe_percentile]}};
assert.equal(decision({},oldValuation).action,'一切照旧');
assert.equal(decision({pe_ttm:null},oldValuation).action,'清仓'); // 无报价时保留快照风险
const unknownSnapshot = {sell_snapshot:{...e.sell_snapshot,missing:[labels.sell_pe_percentile]}};
assert.equal(decision({},unknownSnapshot).action,'一切照旧');
for (const change of [undefined, {source:'行业内截面'}, {as_of:'2026-09-01'},
  {pe:{known:false}}, {pe:{known:true,boundary:20,samples:4,percentile:.8}},
  {pe:{known:true,boundary:20,samples:21,percentile:.9}}]) {
  const before = site.thr['000001'];
  site.thr['000001'] = change ? {...before,...change} : undefined;
  assert.equal(decision().action,'无法评估');
  site.thr['000001'] = before;
}
for (const [now, stamp, expected] of [
  ['2026-10-21T04:00:00Z','2026-10-21 11:30:00',true], // 午休
  ['2026-10-21T08:00:00Z','2026-10-21 15:00:00',true],
  ['2026-10-21T08:00:00Z','2026-10-21 14:00:00',false],
  ['2026-10-24T02:00:00Z','2026-10-23 15:00:00',true], // 周末
  ['2026-10-26T01:00:00Z','2026-10-23 15:00:00',true], // 周一开盘前
  ['2026-10-26T02:00:00Z','2026-10-23 15:00:00',false],
  ['2026-10-21T04:00:00Z','2026-10-21 12:00:00',false]
]) {
  assert.equal(valuationQuoteValid({...q,quote_ts:stamp},'000001',Date.parse(now)),expected);
}
const fields = Array(88).fill(''); fields[2]='000001'; fields[3]='10'; fields[30]='20261021100000';
fields[39]='20bad'; fields[46]='Infinity';
const parsed = parseQuoteText('v_sz000001="'+fields.join('~')+'";')['000001'];
assert.equal(parsed.pe_ttm,null); assert.equal(parsed.pb,null);
console.log('dynamic valuation tests passed');
