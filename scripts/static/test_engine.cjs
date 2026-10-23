/* 对照测试：scripts/static/app.js 的 oneRow() vs touzi/portfolio.py 的 Portfolio._one()。
 *
 * 用法（在仓库根目录）：
 *   F:\ana\python.exe scripts\static\ref_portfolio.py > .tmp\ref.json
 *   node scripts\static\test_engine.cjs .tmp\ref.json
 *
 * 存在的理由：静态版网站把费用公式搬到了浏览器里，Python 那一份还在后台重算用。
 * 两套公式一旦走样，公网版算出来的钱就跟本机版对不上了，而且**不会报任何错**。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const refPath = process.argv[2] || path.join('.tmp', 'ref.json');
// PowerShell 的 > 重定向可能带 BOM，先剥掉再解析
const refRaw = fs.readFileSync(refPath, 'utf8').replace(/^\uFEFF/, '');
const ref = JSON.parse(refRaw);

// 旧基准只导出部分金额列；补取同一 Python _one 的成本和双边费用，避免用JS公式自证。
const oracle = spawnSync(process.env.TOUZI_TEST_PYTHON || 'python', ['-c', `
import json,sys
from pathlib import Path
sys.path[:0] = [str(Path.cwd() / 'vendor'), str(Path.cwd())]
from touzi.config import load_config
from touzi.portfolio import Portfolio, FeeModel
ref=json.load(sys.stdin)
pf=Portfolio(load_config(), fees=FeeModel(**ref['fees_key']))
pf.plan=dict(pf.plan, reduce_ratio=ref['plan_reduce_ratio'])
out=[]
for case in ref['cases']:
    a=dict(case['holding']); a['成本金额']=a['买入价']*a['股数']
    src=case['tracking']
    t={'现价':src['现价'], '建议动作':src['action'], '触发的卖出条件':src['triggers'], '预警':src['warn']}
    out.append(pf._one(a,t))
print(json.dumps(out,ensure_ascii=False,allow_nan=False))
`], { input: JSON.stringify(ref), encoding: 'utf8', cwd: path.join(__dirname, '..', '..'), env: {
  ...process.env, PYTHONIOENCODING: 'utf-8',
} });
if (oracle.status !== 0) { throw new Error('Python完整金额基准失败：' + oracle.stderr); }
const fullExpect = JSON.parse(oracle.stdout);

// 固定有效工作日和源时间，避免随执行当天变化而把费用用例误变成过期数据用例。
const NOW = Date.parse('2026-10-23T10:00:00+08:00');
const RealDate = Date;
global.Date = class extends RealDate {
  constructor(...args) { super(...(args.length ? args : [NOW])); }
  static now() { return NOW; }
};

// 搭一个最小浏览器环境，再加载 app.js
global.window = {};
global.window.__SITE__ = {
  fees: ref.fees_key,
  plan: { reduce_ratio: ref.plan_reduce_ratio, sell_stop_loss: -0.25, sell_take_profit: 1.5,
    sell_pe_percentile: 0.9, sell_pb_percentile: 0.9 },
  as_of: '2026-10-23', quote_at: '2026-10-23 10:00:00', quote_coverage: 1,
  thr: {},
  entries: {},
  interval: 15
};
ref.cases.forEach((c) => {
  global.window.__SITE__.entries[c.holding['代码']] = {
    name: c.holding['名称'],
    sell_snapshot: {
      exit: c.tracking.action === '清仓' ? [c.tracking.triggers] : [],
      reduce: c.tracking.action === '减仓' ? [c.tracking.triggers] : [],
      warn: c.tracking.warn ? [c.tracking.warn] : [], missing: [], missing_warn: [],
    },
  };
  // 报价PE/PB低于有效的历史阈值，让本测试只验证指定公共动作对应的费用。
  global.window.__SITE__.thr[c.holding['代码']] = { source: '个股5年历史', as_of: '2026-10-23',
    pe: { known: true, samples: 100, boundary: 100, percentile: 0.9 },
    pb: { known: true, samples: 100, boundary: 100, percentile: 0.9 } };
});

const engine = require(path.join(__dirname, 'app.js'));

const MAP = [
  ['现价', 'price'], ['市值', 'mv'], ['浮动盈亏', 'pnl'], ['浮动盈亏%', 'pnl_pct'],
  ['建议卖出%', 'sellPct'], ['建议卖出股数', 'sellShares'], ['卖出金额', 'sellAmount'],
  ['到账金额', 'netIn'], ['扣费后净盈利', 'net_profit'], ['扣费后净盈利%', 'net_profit_pct'],
  ['成本金额', 'costAmount'], ['买入费用', 'buyFee'], ['含费成本', 'costWithFees'], ['卖出费用', 'sellFee']
];
const NUMERIC = new Set(MAP.map((m) => m[0]));

let bad = 0, checks = 0;
console.log('费用公式对照：Python 基准 ' + refPath);
console.log('费率 ' + JSON.stringify(ref.fees_key) + '  减仓比例 ' + ref.plan_reduce_ratio);
console.log('');

ref.cases.forEach((c, index) => {
  const got = engine.oneRow(c.holding, { code: c.holding['代码'], price: c.tracking['现价'],
    pe_ttm: 10, pb: 1, quote_ts: '2026-10-23 10:00:00' });
  const problems = [];
  MAP.forEach(([py, js]) => {
    const want = Object.prototype.hasOwnProperty.call(c.expect, py) ? c.expect[py] : fullExpect[index][py];
    const have = got[js];
    checks += 1;
    if (NUMERIC.has(py)) {
      const w = (want === null || want === undefined) ? null : Number(want);
      const h = (have === null || have === undefined) ? null : Number(have);
      if (w === null || h === null) {
        if (w !== h) { problems.push(`${py}: python=${want} js=${have}`); }
        return;
      }
      if (!Number.isFinite(w) || !Number.isFinite(h) || Math.abs(w - h) > 0.011) {
        problems.push(`${py}: python=${want} js=${have}`);
      }
      return;
    }
    if (String(want) !== String(have)) { problems.push(`${py}: python=${JSON.stringify(want)} js=${JSON.stringify(have)}`); }
  });
  // 当前静态页不再沿用旧跟踪表的“持有/观望”，缺数据和无股数的提示优先级也已改变。
  // 保留动作/风险语义断言；金额提示后缀继续逐字对照Python，不以新措辞替换旧标准答案。
  const hasPrice = Number.isFinite(c.tracking['现价']) && c.tracking['现价'] > 0;
  const actionable = ['清仓', '减仓'].includes(c.tracking.action);
  const wantAction = actionable ? c.tracking.action : hasPrice ? '一切照旧' : '无法评估';
  const wantLevel = actionable ? 'act' : !hasPrice ? 'na' : c.tracking.warn ? 'watch' : 'hold';
  checks += 2;
  if (got.action !== wantAction) { problems.push(`动作: expected=${wantAction} js=${got.action}`); }
  if (got.level !== wantLevel) { problems.push(`级别: expected=${wantLevel} js=${got.level}`); }
  if (actionable) {
    checks += 1;
    if (!got.alert.includes(c.tracking.action + '条件已触发') || !got.alert.includes(c.tracking.triggers)) {
      problems.push('动作提醒必须保留触发条件');
    }
    if (c.holding['股数'] > 0) {
      const suffix = String(c.expect['提醒']).match(/。这部分扣费后净盈利 .*$/);
      checks += 1;
      if (!suffix || !got.alert.endsWith(suffix[0])) { problems.push('净盈利提示金额与Python不一致'); }
    } else {
      checks += 1;
      if (!got.alert.includes('请补填股数') || got.alert.includes('净盈利')) { problems.push('无股数须保留触发提醒，且不能虚构金额'); }
    }
  } else {
    checks += 1;
    if (!got.alert.includes(hasPrice ? '一切照旧' : '无法评估')) { problems.push('持有/未知状态提示不符合当前安全语义'); }
    if (c.tracking.warn) {
      checks += 1;
      if (!got.alert.includes(c.tracking.warn)) { problems.push('已知预警不得丢失'); }
    }
  }
  if (problems.length) {
    bad += 1;
    console.log('✗ ' + c.name);
    problems.forEach((p) => console.log('    ' + p));
  } else {
    console.log('✓ ' + c.name);
  }
});

// 四等分对比：同一笔现金流、四只 ETF、买卖佣金和整手约束都必须进入结果。
const dates = [['2025-01-10', 1.00], ['2025-02-10', 1.10]];
const benchmark = {
  lot_size: 100,
  instruments: ['沪深300 ETF', '上证50 ETF', '标普500 ETF', '纳指 ETF'].map((label, i) => ({
    code: String(510000 + i), label, color: '#fff', prices: dates,
  })),
};
const cmp = engine.comparisonData(
  [{ '代码': '600000', '买入日期': '2025-01-10', '买入价': '10', '股数': '100' }],
  { '600000': [['2025-01-10', 10], ['2025-02-10', 11]] },
  benchmark,
);
checks += 4;
if (cmp.series && cmp.series.length === 6) { console.log('✓ 收益对比生成 6 条线'); }
else { bad += 1; console.log('✗ 收益对比没有生成 6 条线'); }
if (cmp.invested > 1000) { console.log('✓ 对比投入金额包含买入手续费'); }
else { bad += 1; console.log('✗ 对比投入金额没有包含买入手续费'); }
if (cmp.summaries[5].returnPct > 0 && cmp.summaries[5].returnPct < 10) {
  console.log('✓ 四等分组合收益已扣佣金并受整手约束');
} else { bad += 1; console.log('✗ 四等分组合收益异常'); }
if (cmp.summaries[0].returnPct > 0 && cmp.summaries[0].returnPct < 10) {
  console.log('✓ 我的持仓曲线包含买卖手续费');
} else { bad += 1; console.log('✗ 我的持仓曲线手续费异常'); }

console.log('');
console.log(bad ? `对照失败：${bad}/${ref.cases.length} 个用例不一致（共 ${checks} 项检查）`
  : `对照通过：${ref.cases.length} 个用例、${checks} 项检查全部一致`);
process.exit(bad ? 1 : 0);
