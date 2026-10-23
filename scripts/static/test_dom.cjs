/* 公网版「客户端渲染」这一路的测试。
 *
 * 为什么需要它：`scripts/static/app.js` 里的 renderLedger / renderLive / renderKpi /
 * apply / 表单读写 / CSV 导入导出——**这些函数只有在浏览器里才会跑**，而这里没有浏览器。
 * 它们一旦写错，用户打开 /my.html 看到的就是一片空白，而服务端自检（check_static.py）
 * 只看静态 HTML，完全看不出来。
 *
 * 所以这里搭一个极简 DOM（够 app.js 用即可），喂一份**伪造的腾讯行情**（ASCII 名称，
 * 免得依赖 GBK 解码），然后把「录入 → 渲染 → 报价回来 → 再渲染 → 导入 → 删除」整条
 * 链路走一遍，断言表格里出现的字。
 *
 * 用法：node scripts/static/test_dom.cjs [dist/site.js]
 */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');
const sitePath = process.argv[2] || path.join(ROOT, 'dist', 'site.js');

/* ------------------------------------------------------------------ 极简 DOM */
let console_err = [];
function mkEl(tag, attrs) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    _attrs: Object.assign({}, attrs || {}),
    value: '', textContent: '', innerHTML: '', className: '', checked: false,
    style: {}, offsetWidth: 1, disabled: false, children: [],
    classList: {
      add() { }, remove() { }, contains() { return false; },
    },
    getAttribute(n) { return this._attrs[n] === undefined ? null : this._attrs[n]; },
    setAttribute(n, v) { this._attrs[n] = v; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    appendChild(c) { this.children.push(c); return c; },
    remove() { }, click() { },
    focus() { },
  };
  return el;
}

const byId = {};
const qcells = [];        // 供 document.querySelectorAll('[data-q]') 用
const pickRows = [];      // 供 tr[data-pick] 用

const documentShim = {
  readyState: 'complete',
  hidden: false,
  activeElement: { tagName: 'BODY' },
  body: mkEl('body'),
  getElementById(id) { return byId[id] || null; },
  querySelectorAll(sel) {
    if (sel === '[data-q]') { return qcells; }
    if (sel === 'tr[data-pick]') { return pickRows; }
    return [];
  },
  createElement(t) { return mkEl(t); },
  addEventListener() { },
};

function mkInput(id, value) {
  const e = mkEl('input');
  e.value = value === undefined ? '' : value;
  byId[id] = e;
  return e;
}
['f_idx', 'f_code', 'f_name', 'f_date', 'f_price', 'f_shares', 'f_note', 'f_msg',
  'f_import', '__pdate', '__pnote', '__buildstate'].forEach((id) => mkInput(id, ''));
byId['__ledger'] = mkEl('tbody');
byId['__live'] = mkEl('tbody');
byId['__kpi'] = mkEl('div');
byId['__compare'] = mkEl('div');
byId['__ledgernote'] = mkEl('p');
byId['__livets'] = mkEl('b');
byId['__livestate'] = mkEl('span');
byId['__dot'] = mkEl('span');
byId['__toast'] = mkEl('div');

const store = {};
const localStorageShim = {
  getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem(k, v) { store[k] = String(v); },
  removeItem(k) { delete store[k]; },
};

/* --------------------------------------------------- 伪造的腾讯行情响应 */
function mkQuoteLine(sym, code, price) {
  const f = new Array(88).fill('');
  f[0] = '1';
  f[1] = 'T' + code.slice(-3);          // 名字只用 ASCII，避免依赖 GBK 解码
  f[2] = code;
  f[3] = price.toFixed(2);
  f[4] = (price * 1.01).toFixed(2);
  f[5] = price.toFixed(2);
  f[30] = '20261008150000';
  f[31] = '0.00';
  f[32] = '0.50';
  f[33] = price.toFixed(2);
  f[34] = price.toFixed(2);
  f[36] = '1'; f[37] = '1'; f[38] = '0.50';
  f[39] = '19.28'; f[43] = '1.00'; f[44] = '100.00'; f[45] = '100.00'; f[46] = '6.25';
  return 'v_' + sym + '="' + f.join('~') + '";';
}
const PRICES = { '600519': 1255.79, '000651': 38.32 };
let fetchCount = 0;

function fakeFetch(url) {
  fetchCount += 1;
  if (String(url).indexOf('/fqkline/get?') >= 0) {
    const decoded = decodeURIComponent(String(url));
    const m = decoded.match(/param=([a-z]+)(\d{6}),/);
    const sym = m ? (m[1] + m[2]) : 'sh600519';
    const code = m ? m[2] : '600519';
    const base = PRICES[code] === undefined ? 10 : PRICES[code];
    const payload = { data: {} };
    payload.data[sym] = { qfqweek: [
      ['2025-01-10', String(base), String(base), String(base), String(base), '1'],
      ['2026-10-09', String(base * 1.10), String(base * 1.10), String(base * 1.10), String(base * 1.10), '1'],
    ] };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
  }
  const codes = String(url).split('q=')[1].split(',');
  const body = codes.map((s) => {
    const code = s.replace(/^[a-z]+/, '');
    const p = PRICES[code] === undefined ? 10 : PRICES[code];
    return mkQuoteLine(s, code, p);
  }).join('');
  const bytes = Buffer.from(body, 'latin1');   // 全 ASCII，latin1 就是它的字节
  return Promise.resolve({
    ok: true,
    status: 200,
    arrayBuffer: () => Promise.resolve(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)),
  });
}

/* ------------------------------------------------------------ 装配环境 */
global.document = documentShim;
global.localStorage = localStorageShim;
global.fetch = fakeFetch;
global.confirm = () => true;
global.location = { href: '', reload() { }, hidden: false };
global.window = {
  __SITE__: null,
  scrollTo() { },
  addEventListener() { },
  location: global.location,
  document: documentShim,
};
const realErr = console.error;
console.error = (...a) => { console_err.push(a.join(' ')); };

// dist/site.js 里就是 window.__SITE__ = {...};
const siteTxt = fs.readFileSync(sitePath, 'utf8');
const mm = siteTxt.match(/window\.__SITE__\s*=\s*(\{.*\})\s*;/s);
if (!mm) { realErr('site.js 里找不到 window.__SITE__'); process.exit(1); }
global.window.__SITE__ = JSON.parse(mm[1]);
global.window.__SITE__.seed_holdings = [];     // 公网版默认不带持仓

require(path.join(__dirname, 'app.js'));
const W = global.window;

/* ------------------------------------------------------------- 断言工具 */
let pass = 0;
const fails = [];
function ok(name, cond, extra) {
  if (cond) { pass += 1; console.log('  ✓ ' + name); }
  else { fails.push(name + (extra ? '  → ' + extra : '')); console.log('  ✗ ' + name + (extra ? '  → ' + extra : '')); }
}
function has(hay, needle) { return String(hay).indexOf(needle) >= 0; }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------------ 开跑 */
(async () => {
  console.log('客户端渲染测试（伪造行情，不走网络）');
  console.log('site.js：entries ' + Object.keys(global.window.__SITE__.entries).length
    + ' 只，发布 ' + global.window.__SITE__.built_at);
  console.log('');

  await sleep(60);   // 等 boot() 里的第一次 tick 完成

  console.log('[1] 空台账开局');
  ok('boot 后 __buildstate 以textContent显示实际发布时间',
    has(byId['__buildstate'].textContent, '评分生成于 ' + global.window.__SITE__.built_at)
      && has(byId['__buildstate'].textContent, '行情报价 ' + (global.window.__SITE__.quote_at || '—')),
    byId['__buildstate'].textContent);
  ok('台账表显示「还没有任何持仓」', has(byId['__ledger'].innerHTML, '还没有任何持仓'));
  ok('实时表显示「台账为空」', has(byId['__live'].innerHTML, '台账为空'));
  ok('KPI 显示 0 只持仓', has(byId['__kpi'].innerHTML, '持仓只数'));
  // 空台账 + 没有已渲染的 data-q 单元格时，确实没有代码可查（真实浏览器里
  // my.html 空台账也是这样），所以这里不要求发请求——见 [3] 加完持仓之后就发了。

  console.log('\n[2] 通过表单录入一笔（100 股 @1450）');
  byId['f_code'].value = '600519';
  byId['f_price'].value = '1450.00';
  byId['f_shares'].value = '100';
  byId['f_date'].value = '2025-01-10';
  W.__holdSave();
  ok('台账表出现 600519', has(byId['__ledger'].innerHTML, '<code>600519</code>'));
  ok('台账表出现 1450 与 100', has(byId['__ledger'].innerHTML, '1450') && has(byId['__ledger'].innerHTML, '100'));
  ok('写进了 localStorage', has(store['touzi.ledger.v1'] || '', '600519'));
  ok('提示文案说已新增', has(byId['f_msg'].textContent, '新增') || has(byId['f_msg'].textContent, '覆盖'));
  await sleep(120);   // __renderAll 会立刻催一次行情（tickSoon）
  ok('录入后立刻去取了行情（不等 15 秒）', fetchCount > 0, 'fetchCount=' + fetchCount);
  await sleep(120);
  ok('收益对比渲染出折线图与四等分组合', has(byId['__compare'].innerHTML, '<svg')
    && has(byId['__compare'].innerHTML, '四等分组合')
    && has(byId['__compare'].innerHTML, '投入金额'), byId['__compare'].innerHTML.slice(0, 300));

  console.log('\n[3] 实时盈亏表：结构与初值');
  const live = byId['__live'].innerHTML;
  ok('有 data-q="600519.price"', has(live, 'data-q="600519.price"'));
  ok('有 data-q="600519.mv"', has(live, 'data-q="600519.mv"'));
  ok('有 data-q="600519.pnl"', has(live, 'data-q="600519.pnl"'));
  ok('有 data-q="600519.net_profit"', has(live, 'data-q="600519.net_profit"'));
  ok('含费成本 = 145,037.70（145000 + 佣金36.25 + 过户1.45）',
    has(live, '145,037.70'), live.slice(0, 400));
  ok('市值 = 125,579.00（1255.79 × 100）', has(live, '125,579.00'));
  ok('浮动盈亏 = -19,421.00', has(live, '-19,421.00'));

  console.log('\n[4] KPI 卡片');
  const kpi = byId['__kpi'].innerHTML;
  ok('KPI 有 summary.mv 挂载点', has(kpi, 'data-q="summary.mv"'));
  ok('KPI 有 summary.pnl 挂载点', has(kpi, 'data-q="summary.pnl"'));
  ok('KPI 总市值 125,579.00', has(kpi, '125,579.00'));

  console.log('\n[5] apply() 真的会改写带 data-q 的单元格');
  const cell = mkEl('td', { 'data-q': '600519.price', 'data-nd': '3', 'data-suffix': '' });
  cell.textContent = '0.000';
  qcells.push(cell);
  W.__renderAll();
  ok('单元格被写成 1,255.790', cell.textContent === '1,255.790', '实际 ' + JSON.stringify(cell.textContent));
  const cell2 = mkEl('td', { 'data-q': 'summary.pnl_pct', 'data-nd': '2', 'data-suffix': '%' });
  cell2.textContent = '0.00%';
  qcells.push(cell2);
  W.__renderAll();
  ok('百分比单元格带 % 且为负', /^-13\.\d\d%$/.test(cell2.textContent), '实际 ' + JSON.stringify(cell2.textContent));

  console.log('\n[6] 同一笔重复保存要去重（键＝代码+日期+价）');
  const before = (byId['__ledger'].innerHTML.match(/data-idx=/g) || []).length;
  byId['f_code'].value = '600519';
  byId['f_price'].value = '1450.00';
  byId['f_shares'].value = '100';
  byId['f_date'].value = '2025-01-10';
  W.__holdSave();
  const after = (byId['__ledger'].innerHTML.match(/data-idx=/g) || []).length;
  ok('没有新增行', before === after, before + ' → ' + after);
  ok('提示说是覆盖', has(byId['f_msg'].textContent, '覆盖'));

  console.log('\n[7] 换一个买入价＝新增一笔（分批买入）');
  byId['f_price'].value = '1300.00';
  byId['f_shares'].value = '50';
  W.__holdSave();
  const after2 = (byId['__ledger'].innerHTML.match(/data-idx=/g) || []).length;
  ok('变成 2 行', after2 === 2, '实际 ' + after2);

  console.log('\n[8] CSV 导入（含一行非法代码要被跳过）');
  byId['f_import'].value = '代码,名称,买入日期,买入价,股数,备注\r\n'
    + '000651,格力电器,2023-06-01,32.00,1000,导入的\r\n'
    + '9,坏行,2024-01-01,1,1,应当跳过\r\n';
  W.__holdImport();
  await sleep(120);
  ok('导入了 000651', has(byId['__ledger'].innerHTML, '<code>000651</code>'));
  ok('提示里说跳过了坏行', has(byId['__toast'].textContent, '跳过'), byId['__toast'].textContent);
  ok('实时表出现 000651 且金额对得起 38.32×1000',
    has(byId['__live'].innerHTML, '38,320.00'), '');

  console.log('\n[9] 从「选股·勾选建仓」打勾加入');
  const row = mkEl('tr', { 'data-pick': '601398', 'data-name': '工商银行' });
  const cb = mkEl('input'); cb.checked = true;
  const ip = mkEl('input'); ip.value = '6.00';
  const is = mkEl('input'); is.value = '1000';
  row.querySelector = (s) => (s.indexOf('checkbox') >= 0 ? cb : (s.indexOf('.ip') >= 0 ? ip : is));
  pickRows.push(row);
  byId['__pdate'].value = '2026-01-05';
  W.__pickAdd();
  ok('601398 进了台账', has(byId['__ledger'].innerHTML, '<code>601398</code>'));
  ok('打勾加入的那笔用了我填的买入价与股数',
    has(store['touzi.ledger.v1'] || '', '6.00') && has(store['touzi.ledger.v1'] || '', '1000'));

  console.log('\n[10] 删除一行');
  const n1 = (byId['__ledger'].innerHTML.match(/data-idx=/g) || []).length;
  W.__holdDel(0, '600519');
  const n2 = (byId['__ledger'].innerHTML.match(/data-idx=/g) || []).length;
  ok('行数减 1', n2 === n1 - 1, n1 + ' → ' + n2);
  ok('localStorage 里也少了那笔',
    (JSON.parse(store['touzi.ledger.v1'] || '[]')).length === n2,
    store['touzi.ledger.v1']);

  console.log('\n[11] 清空');
  W.__holdWipe();
  ok('台账回到空态', has(byId['__ledger'].innerHTML, '还没有任何持仓'));
  ok('localStorage 也清了', (store['touzi.ledger.v1'] || '[]') === '[]', store['touzi.ledger.v1']);

  console.log('\n[12] 没有意外的 console.error');
  ok('无 console.error', console_err.length === 0, console_err.join(' | '));
  console.error = realErr;

  console.log('');
  if (fails.length) {
    console.log('客户端渲染测试失败，' + fails.length + ' 项：');
    fails.forEach((f) => console.log('  ✗ ' + f));
    process.exit(1);
  }
  console.log('客户端渲染测试通过：' + pass + ' 项断言全部成立。');
  process.exit(0);
})();
