const assert = require('node:assert/strict');

global.window = {
  __SITE__: {
    as_of: new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10),
    plan: { sell_stop_loss: -0.25, sell_take_profit: 1.5, reduce_ratio: 0.5 },
    fees: { commission_rate: 0.00025, commission_min: 5, stamp_duty_rate: 0.0005, transfer_fee_rate: 0.00001 },
    entries: {
      '000001': { name: '测试股票', sell_snapshot: { exit: [], reduce: [], warn: [], missing: [] } },
      '000002': { name: '公开清仓', sell_snapshot: { exit: ['总分 ≤ 62'], reduce: [], warn: [], missing: [] } },
      '000003': { name: '公开减仓', sell_snapshot: { exit: [], reduce: ['总分 ≤ 67'], warn: [], missing: [] } },
      '000005': { name: '缺失数据', sell_snapshot: { exit: [], reduce: [], warn: [], missing: ['PE 自身分位'] } },
      '000004': { name: '待核验' }
    }
  }
};

const { oneRow, summarize } = require('../scripts/static/app.js');

function row(code, price, buy = 10) {
  return oneRow({ '代码': code, '名称': '', '买入价': buy, '股数': 100, '买入日期': '2026-10-12' },
    price == null ? null : { price });
}

assert.equal(row('000001', 10).action, '一切照旧');
assert.equal(row('000001', 10).level, 'hold');
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
