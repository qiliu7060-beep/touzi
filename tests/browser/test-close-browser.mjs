import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { webcrypto, createHash } from 'node:crypto';
import { browserTestRuntimes } from './browser-test-runtime.mjs';
import { sortCloseIndex } from '../lib/close-browser.mjs';

const mixed = [
  {code:'000004',action:'观望',total:99}, {code:'000003',action:'建仓',total:50},
  {code:'000002',action:'建仓',total:50}, {code:'000001',action:'建仓',total:null},
  {code:'000006',action:'回避',total:100}, {code:'000005',action:'接近建仓',total:40},
  {code:'000008',action:'待核验',total:90}, {code:'000007',action:'候选',total:90},
];
const untouched = JSON.stringify(mixed);
assert.deepEqual(sortCloseIndex(mixed).map(item => item.code), ['000002','000003','000001','000005','000004','000006','000007','000008']);
assert.equal(JSON.stringify(mixed), untouched, '展示排序不能改写快照身份');

// 测试用轻量DOM，解析真实生成的HTML并保留节点身份，不连接用户账户或真实行情。
class TextNode {
  constructor(value) { this.nodeType = 3; this.nodeValue = value; }
  get textContent() { return this.nodeValue; }
}
class Element {
  constructor(tag, attrs = {}) { this.nodeType = 1; this.tagName = tag.toUpperCase(); this.attrs = { ...attrs }; this.childNodes = []; this.style = {}; this.listeners = {}; this.checked = false; this._value = ''; }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get id() { return this.attrs.id || ''; } set id(value) { this.attrs.id = value; }
  get className() { return this.attrs.class || ''; } set className(value) { this.attrs.class = value; }
  get value() { return this._value || (this.tagName === 'SELECT' && this.children[0] && this.children[0].getAttribute('value')) || ''; }
  set value(value) { this._value = String(value); }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  set textContent(value) { this.childNodes = [new TextNode(String(value))]; }
  get innerHTML() { return this._html || this.textContent; }
  set innerHTML(html) {
    this._html = html; this.childNodes = [];
    const stack = [this];
    for (const token of html.match(/<[^>]+>|[^<]+/g) || []) {
      if (token.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
      if (!token.startsWith('<')) { stack.at(-1).appendChild(new TextNode(token.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&'))); continue; }
      const match = token.match(/^<(\w+)([^>]*)>/); if (!match) continue;
      const attrs = {};
      for (const attr of match[2].matchAll(/([\w-]+)(?:="([^"]*)"|'([^']*)'|=([^\s>]+))?/g)) attrs[attr[1]] = attr[2] ?? attr[3] ?? attr[4] ?? '';
      const element = new Element(match[1], attrs); stack.at(-1).appendChild(element);
      if (!['input','br','hr','img','meta','link'].includes(match[1])) stack.push(element);
    }
  }
  appendChild(node) { if (node.parentNode) node.parentNode.removeChild(node); node.parentNode = this; this.childNodes.push(node); return node; }
  removeChild(node) { this.childNodes.splice(this.childNodes.indexOf(node), 1); node.parentNode = null; return node; }
  insertBefore(node, before) { if (node.parentNode) node.parentNode.removeChild(node); const index = this.childNodes.indexOf(before); if (index < 0) return this.appendChild(node); node.parentNode = this; this.childNodes.splice(index, 0, node); return node; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return Object.hasOwn(this.attrs, key) ? this.attrs[key] : null; }
  hasAttribute(key) { return Object.hasOwn(this.attrs, key); }
  matches(selector) {
    if (selector.includes(' ')) { const parts = selector.split(' '); const last = parts.pop(); return this.matches(last) && !!(this.parentNode && this.parentNode.closest(parts.join(' '))); }
    const match = selector.match(/^(\w+)?(?:#([\w-]+)|\.([\w-]+))?(?:\[([^=\]]+)(?:="?([^"\]]*)"?)?\])?$/);
    return !!match && (!match[1] || this.tagName === match[1].toUpperCase()) && (!match[2] || this.id === match[2])
      && (!match[3] || this.className.split(' ').includes(match[3])) && (!match[4] || this.hasAttribute(match[4]) && (match[5] === undefined || this.getAttribute(match[4]) === match[5]));
  }
  querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentNode ? this.parentNode.closest(selector) : null; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name, event = {}) { for (const callback of this.listeners[name] || []) callback.call(this, { preventDefault() {}, ...event }); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  focus() {}
}

const date = '2026-10-23', quoteAt = date + 'T15:01:00+08:00', builtAt = date + 'T17:23:00+08:00';
const start = Date.parse('2026-10-26T10:00:00+08:00');
const calendar = JSON.parse(readFileSync(browserTestRuntimes(import.meta.url)[0].file, 'utf8').match(/const MARKET_CALENDAR = (.*);/)[1]);
const index = Array.from({ length: 1001 }, (_, offset) => ({ code: String(offset + 1).padStart(6, '0'), name: '虚拟企业' + (offset + 1),
  action: offset % 3 === 0 ? '建仓' : offset % 3 === 1 ? '接近建仓' : '观望', total: 90 - offset / 100,
  shard: `close-score-parts/${date}-abcdef-${Math.floor(offset / 100)}.json` }));
function entry(item) { return { ...item, price: 10, pe: 8, pb: 1, dy: 4, quote_ts: quoteAt,
  buy_checks: [{ key: 'debt_equity', label: '债务股权比率<0.8', hit: true, known: true, value: .6, threshold: .8 }],
  sell_snapshot: { exit: [], reduce: [], warn: [], missing: [], missing_warn: [], financial_missing: [], financial_warn: [] }, sell_checks: [],
  financial_capability: true, financial_analysis: { status: 'missing', metrics: {}, years: [], quality: {} } }; }
const allEntries = Object.fromEntries(index.map(item => [item.code, entry(item)]));
const base = { schema_version: 1, kind: 'touzi-close-v1', status: 'ok', score_date: date, quote_at: quoteAt, built_at: builtAt,
  trading_calendar: calendar, index, universe: { market_total: 1200, eligible_total: 1005, attempted: 1005, scored: 1001, failed: 4, excluded: 195 },
  plan: { sell_stop_loss: -.25, sell_take_profit: 1.5, reduce_ratio: .5 },
  fees: { commission_min: 5, commission_rate: .00025, stamp_duty_rate: .0005, transfer_fee_rate: .00001 }, errors: [] };

function harness(file) {
  let now = start, offline = false, corrupt = false, localMissing = false;
  const requests = [], writes = [];
  const root = new Element('html'), head = root.appendChild(new Element('head')), main = root.appendChild(new Element('main'));
  main.appendChild(new Element('p', { class: 'sub' })).textContent = '财务两项并非新增买入硬门槛';
  const card = main.appendChild(new Element('section', { class: 'card' }));
  card.innerHTML = '<h3>旧跟踪选股</h3><table><tr><th>旧表头</th></tr><tr data-pick="000001" data-name="虚拟企业1"><td><input type="checkbox"></td>'
    + '<td></td>'.repeat(9) + '<td><input class="ip" type="text"></td><td><input class="is" type="text"></td></tr></table>';
  root.appendChild(new Element('input', { id: '__pdate' })).value = '2026-10-26';
  root.appendChild(new Element('input', { id: '__pnote' })).value = '';
  const document = { head, readyState: 'loading', hidden: false, addEventListener() {},
    getElementById(id) { return root.querySelector('#' + id); }, querySelector(selector) { return root.querySelector(selector); },
    querySelectorAll(selector) { return root.querySelectorAll(selector); }, createElement(tag) { return new Element(tag); } };
  class Clock extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } }
  const parts = new Map();
  for (let i = 0; i < index.length; i += 100) {
    const group = index.slice(i, i + 100), path = group[0].shard;
    const text = JSON.stringify({ schema_version: 1, kind: 'touzi-close-part-v1', score_date: date, entries: Object.fromEntries(group.map(item => [item.code, allEntries[item.code]])) });
    parts.set(path, text);
  }
  const manifest = { ...base, shards: [...parts].map(([path, text]) => ({ path, sha256: createHash('sha256').update(text).digest('hex'), codes: Object.keys(JSON.parse(text).entries) })) };
  async function fetch(url, init) {
    requests.push(url);
    if (offline || localMissing && url === 'close-scores.json') return new Response('', { status: 404 });
    if (url.startsWith('/api/scores/close?')) {
      const params = new URL(url, 'https://test.invalid').searchParams;
      const wanted = params.has('codes') ? params.get('codes').split(',') : sortCloseIndex(index).filter(item => (!params.get('q') || (item.code + ' ' + item.name).includes(params.get('q')))
        && (!params.get('action') || item.action === params.get('action'))).slice((Number(params.get('page') || 1) - 1) * 100, Number(params.get('page') || 1) * 100).map(item => item.code);
      return Response.json({ ...base, entries: Object.fromEntries(wanted.map(code => [code, allEntries[code]])) });
    }
    const relative = url.replace('https://raw.githubusercontent.com/qiliu7060-beep/touzi/close-data/', '');
    if (relative === 'close-scores.json') return Response.json(manifest);
    if (parts.has(relative)) return new Response(corrupt ? '{}' : parts.get(relative));
    if (url === '/api/me') return Response.json({ user: { id: 'virtual-test-only', displayName: '虚拟测试账户' } });
    if (url === '/api/ledger') {
      if (init && init.method === 'PUT') { writes.push(JSON.parse(init.body).holdings); }
      return Response.json({ holdings: [] });
    }
    throw new Error('测试禁止未声明网络请求：' + url);
  }
  const location = { pathname: '/live/pick.html' };
  const context = { module: { exports: {} }, window: { __SITE__: { entries: {} }, location }, location, document,
    Date: Clock, TextDecoder, TextEncoder, AbortSignal, crypto: webcrypto, fetch, setTimeout() {}, clearTimeout() {}, localStorage: { getItem() { return null; }, setItem(key, value) { writes.push(JSON.parse(value)); } } };
  runInNewContext(readFileSync(file, 'utf8'), context);
  return { api: context.module.exports, context, doc: document, oldCard: card, requests, writes, parts, manifest,
    setNow(value) { now = value; }, setOffline(value) { offline = value; }, setCorrupt(value) { corrupt = value; }, setLocalMissing(value) { localMissing = value; } };
}

for (const target of browserTestRuntimes(import.meta.url)) {
  const h = harness(target.file, target.cloud), controller = h.api.closeState;
  assert.equal(await h.api.loadCloseScores(true), true);
  assert.equal(controller.state.index.length, 1001, '全池索引不受600动态模型或40跟踪行限制');
  assert.ok(h.doc.querySelector('main p.sub').textContent.includes('适用行业须同时满足原买入条件'));
  assert.ok(!h.doc.querySelector('main p.sub').textContent.includes('并非新增买入硬门槛'));
  const accepted = controller.state.snapshot;
  assert.throws(() => controller.accept({ ...base, entries: {}, built_at: date + 'T17:24:00+08:00' }, true),
    /同日合格收盘评分不能/, '同日另一版本不能覆盖已经接受的合格收盘分');
  assert.equal(controller.state.snapshot, accepted);
  assert.equal(JSON.stringify(accepted.index), JSON.stringify(index), '接纳时不能原地重排来源索引');
  assert.ok(controller.state.index.slice(0, 334).every(item => item.action === '建仓'), '全索引分组先于分页');
  const firstCloseChanges = [{ code: '000001', action: '清仓', reasons: ['收盘总分跌破门槛', '相对买入价浮亏达到止损线'] }];
  const riskEntries = { '000001': { sell_snapshot: { exit: ['收盘总分跌破门槛'], reduce: [] } } };
  const closeClock = { day: date, phase: 'closed' };
  const notifyClose = h.api.closePublicChanges;
  const firstNotice = notifyClose(firstCloseChanges, {}, riskEntries, { score_date: date }, true, closeClock, false);
  assert.equal(firstNotice.length, 1, '17:23首次合格收盘公共风险不能被休市分支吞掉');
  assert.equal(firstNotice[0].reasons.join('；'), '收盘总分跌破门槛', '盘后不能把个人成本波动混作新公共风险');
  assert.equal(notifyClose(firstCloseChanges, { '000001': firstNotice[0] }, riskEntries,
    { score_date: date }, true, closeClock, false).length, 0, '同一快照重复检查与离线恢复不重复提示');
  assert.equal(notifyClose(firstCloseChanges, {}, riskEntries, { score_date: date }, false, closeClock, false).length, 0,
    '坏来源或延迟快照不能新发公共提醒');
  assert.equal(notifyClose(firstCloseChanges, {}, riskEntries, { score_date: date }, true,
    { day: '2026-10-25', phase: 'closed' }, false).length, 0, '周末首次打开历史收盘只复核不新触发');
  assert.equal(notifyClose([{ code: '000001', action: '清仓', reasons: ['相对买入价浮亏达到止损线'] }], {}, riskEntries,
    { score_date: date }, true, closeClock, false).length, 0);
  const financeNotice = [{ code: '000001', reasons: ['资本开支需要复核'] }];
  assert.equal(notifyClose(financeNotice, {}, riskEntries, { score_date: date }, true, closeClock, true).length, 1,
    '当日新合格收盘的财务预警允许首次提示');
  assert.equal(notifyClose(financeNotice, {}, riskEntries, { score_date: date }, false, closeClock, true).length, 0);
  assert.throws(() => controller.accept({ ...base, entries: { '000001': { ...allEntries['000001'], price: -1 } } }, true));
  assert.throws(() => controller.accept({ ...base, entries: {}, universe: { ...base.universe, scored: 999 } }, true));
  assert.throws(() => controller.accept({ ...base, entries: {}, index: [...index, index[0]] }, true));
  assert.equal(controller.state.snapshot, accepted, '无效明细/覆盖/重复索引不能部分接纳');
  assert.equal(h.api.publicScoreFresh(start), true, '下个交易日盘中仍展示上个合格收盘分');
  assert.equal(h.oldCard.hidden, true, '旧40行入口必须隐藏');
  assert.equal(h.doc.querySelectorAll('#__close-selection').length, 1);
  assert.equal(h.doc.getElementById('__close-rows').querySelectorAll('tr[data-pick]').length, 100);
  const firstRow = h.doc.getElementById('__close-rows').querySelector('tr[data-pick]');
  const firstBox = firstRow.querySelector('input[type=checkbox]'), firstPrice = firstRow.querySelector('input.ip');
  assert.equal(firstPrice.value, '10.000', '未编辑的旧HTML默认值不能覆盖新收盘价');
  firstBox.checked = true; firstPrice.value = '9.99'; firstRow.querySelector('input.is').value = '300'; firstRow.dispatch('input');
  assert.equal(controller.state.entries['000001'].total, 90);
  // 输入价格变化、实时行情变化均不能改变公共收盘分。
  h.context.window.__SITE__.entries['000001'].price = 999;
  h.api.renderCloseSelection();
  assert.equal(firstRow.querySelectorAll('td')[4].textContent, '90.00');
  await controller.setView('', '', 2);
  assert.equal(h.doc.getElementById('__close-rows').querySelectorAll('tr[data-pick]')[0].getAttribute('data-pick'), '000301');
  assert.equal(h.api.closePickDrafts()[0].price, '9.99');
  assert.equal(h.api.closePickDrafts()[0].shares, '300');
  // 实际调用现有加入台账入口，拦截全部写入，只验证虚拟候选载荷包括不在当前页的选择。
  if (target.cloud) await h.api.loadLedger();
  await h.context.window.__pickAdd();
  assert.equal(h.writes.length, 1);
  assert.equal(h.writes[0].length, 1);
  assert.equal(h.writes[0][0][target.cloud ? 'code' : '代码'], '000001');
  assert.equal(Number(h.writes[0][0][target.cloud ? 'buyPrice' : '买入价']), 9.99);
  assert.equal(Number(h.writes[0][0][target.cloud ? 'shares' : '股数']), 300);
  if (target.cloud) {
    // 走真实监控渲染与通知出口，防止过滤已放行却被最后的休市显示分支吞掉。
    h.setNow(Date.parse(date + 'T17:30:00+08:00'));
    const notices = [];
    h.context.window.__toast = message => notices.push(message);
    h.context.window.__SITE__.entries['000001'].sell_snapshot.exit = ['收盘总分跌破门槛'];
    h.api.renderMonitor(true);
    assert.equal(notices.length, 1, '17:23新合格收盘公共风险必须实际发出首次通知');
    assert.match(h.doc.getElementById('__newalert').textContent, /新触发或条件变化/);
    assert.match(h.doc.getElementById('__sellalerts').textContent, /新收盘条件触发/);
    h.api.renderMonitor(true);
    assert.equal(notices.length, 1, '同一合格快照重复检查不重发');
    h.setOffline(true); await controller.refresh([]); h.api.renderMonitor(true);
    assert.equal(notices.length, 1, '来源失败期间不重发');
    h.setOffline(false); await controller.refresh([]); h.api.renderMonitor(true);
    assert.equal(notices.length, 1, '离线恢复相同条件不重发');
    h.setNow(start);
  }
  const search = h.doc.getElementById('__financialsearch');
  search.value = '虚拟企业1001'; h.doc.getElementById('__financialform').dispatch('submit');
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.doc.getElementById('__financialcode').value, '001001', '财务中文搜索覆盖尚未显示的全池股票');
  assert.ok(controller.state.entries['001001'], '选择全池股票后按需读取财务分片');
  await controller.setView('虚拟企业1001', '', 1);
  assert.equal(h.doc.getElementById('__close-rows').querySelectorAll('tr[data-pick]').length, 1);
  assert.equal(h.doc.getElementById('__close-rows').querySelector('tr[data-pick]').getAttribute('data-pick'), '001001');
  await controller.setView('', '', 1);
  assert.equal(h.doc.getElementById('__close-rows').querySelector('tr[data-pick]'), firstRow);
  assert.equal(firstRow.querySelector('input.ip'), firstPrice, '刷新与翻页不得替换原输入节点');
  assert.equal(firstPrice.value, '9.99'); assert.equal(firstBox.checked, true);
  assert.equal(h.api.closePickDrafts().length, 1);
  h.setNow(Date.parse('2026-10-26T15:30:00+08:00'));
  assert.equal(controller.isCurrent(), false);
  h.setOffline(true); await controller.refresh([]);
  assert.equal(controller.state.status, 'delayed');
  assert.equal(controller.state.snapshot.score_date, date);
  assert.equal(controller.state.entries['000001'].total, 90, '网络失败不得清空历史固定分');
  assert.equal(firstRow.querySelectorAll('td')[4].textContent, '90.00');
  assert.match(h.doc.getElementById('__close-coverage').textContent, /更新延迟/);
  assert.ok(h.requests.every(url => !url.includes('/api/scores/live')), '主动产品链不能再请求动态评分');
  if (!target.cloud) {
    const failed = harness(target.file, false); failed.setCorrupt(true);
    assert.equal(await failed.api.loadCloseScores(true), false);
    assert.equal(failed.api.closeState.state.snapshot, null, '摘要不一致不得接纳第一份快照');
    const fallback = harness(target.file, false); fallback.setLocalMissing(true);
    assert.equal(await fallback.api.loadCloseScores(true), true);
    assert.ok(fallback.requests.some(url => url.startsWith('https://raw.githubusercontent.com/qiliu7060-beep/touzi/close-data/')));
    const traversal = harness(target.file, false); traversal.manifest.shards[0].path = '../private.json';
    assert.equal(await traversal.api.loadCloseScores(true), false);
    assert.ok(!traversal.requests.some(url => url.includes('private.json')), '拒绝非法分片路径，不发出额外请求');
  }
  console.log(`${target.file}: fixed close scores, >600 full-pool pagination/search, drafts/nodes, failed-source retention and independent clock passed`);
}
