import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { webcrypto, createHash } from 'node:crypto';
import { browserTestRuntimes } from './browser-test-runtime.mjs';
const helper = new URL('../lib/investment-browser.mjs', import.meta.url);
const api = await import(existsSync(helper) ? helper.href : new URL('../../scripts/investment-browser.mjs', import.meta.url).href);
const fixture = JSON.parse(readFileSync(new URL('./macro-context-fixture.json', import.meta.url), 'utf8'));
const calendar = fixture.trading_calendar;
const initial = Date.parse('2026-11-16T18:00:00+08:00');
function signed(value) { value.snapshot_id = createHash('sha256').update(api.canonicalInvestmentContext(value)).digest('hex'); return value; }
assert.equal((await api.validateInvestmentContext(fixture, initial, calendar, webcrypto)).fresh, true);
assert.equal(api.investmentContextProjection(fixture).schema_version, 1);
assert.equal(api.validateInvestment(api.investmentContextProjection(fixture), initial, calendar).fresh, true);
for (const mutate of [
  x => delete x.macro.rules.cpi,
  x => x.macro.score++,
  x => x.macro.inputs.cpi.fetch_status = 'fallback',
  x => x.macro.inputs.cpi.source = 'https://attacker.example',
  x => x.macro.inputs.cpi.observation_date = '2026-11-31',
  x => x.macro.inputs.cpi.fetched_at = '2026-11-17T19:00:00+08:00',
  x => x.macro.inputs.cpi.fetched_at = '2026-11-16T17:58:00',
  x => x.macro.inputs.cpi.release_at = '2026-11-17T18:00:00+08:00',
  x => x.bond.yield_pct++,
  x => x.macro.inputs.private_holdings = {},
]) { const bad = structuredClone(fixture); mutate(bad); await assert.rejects(api.validateInvestmentContext(signed(bad), initial, calendar, webcrypto)); }
// Date.parse会把非法日期及24:00规范化，新v2必须与Python拒绝同一坏时间。
for (const [observed, fetched] of [
  ['2026-02-28', '2026-02-30T17:58:00+08:00'],
  ['2026-10-31', '2026-11-15T24:00:00+08:00'],
]) {
  const bad = structuredClone(fixture);
  bad.macro.inputs.cpi.observation_date = observed; bad.macro.inputs.cpi.fetched_at = fetched;
  await assert.rejects(api.validateInvestmentContext(signed(bad), initial, calendar, webcrypto));
}
const normalizedBuild = structuredClone(fixture);
normalizedBuild.built_at = normalizedBuild.macro.computed_at = '2026-11-15T24:00:00+08:00';
for (const input of Object.values(normalizedBuild.macro.inputs)) input.fetched_at = '2026-11-15T17:58:00+08:00';
normalizedBuild.bond.fetched_at = normalizedBuild.macro.inputs.cn_bond_10y.fetched_at;
await assert.rejects(api.validateInvestmentContext(signed(normalizedBuild), initial, calendar, webcrypto));
const tampered = structuredClone(fixture); tampered.macro.regime = 'tampered';
await assert.rejects(api.validateInvestmentContext(tampered, initial, calendar, webcrypto));
const oldInput = structuredClone(fixture); oldInput.macro.inputs.cpi.fetched_at = '2026-11-10T17:58:00+08:00';
assert.equal((await api.validateInvestmentContext(signed(oldInput), initial, calendar, webcrypto)).fresh, false, '新的computed_at不能复活旧抓取证据');
const exponent = structuredClone(fixture); exponent.macro.inputs.cpi.value = 1e-7;
assert.equal((await api.validateInvestmentContext(signed(exponent), initial, calendar, webcrypto)).fresh, true);
for (const status of [301,302,303,307,308,404,500]) await assert.rejects(api.readInvestmentResponse(new Response(null, { status })));
await assert.rejects(api.readInvestmentResponse(new Response('x'.repeat(65537))));
await assert.rejects(api.readInvestmentResponse(new Response('{bad json')));
await assert.rejects(api.readInvestmentResponse(new Response(new Uint8Array([255,254]))));

// 复用项目浏览器测试的轻量DOM语义，真实节点身份和文本赋值不依赖公网。
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


class Document extends Element {
  constructor(html, pathname) { super('document'); this.innerHTML = html; this.location = { pathname }; this.hidden = false; this.readyState = 'loading'; }
  createElement(tag) { return new Element(tag); }
  getElementById(id) { return this.querySelector('#' + id); }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
}
function page(path = '/live/index.html', marked = false) {
  const marker = key => marked ? ' data-macro-card="' + key + '"' : '';
  return new Document('<main><div class="grid5">'
    + '<div class="card"' + marker('score') + '><div class="kv"><b>宏观背景分</b><span>旧状态</span></div><div class="big">48.5</div></div>'
    + '<div class="card"' + marker('position') + '><div class="kv"><b>目标仓位</b><span>旧仓位</span></div><div class="big">55%</div></div>'
    + '<div class="card"' + marker('computed_at') + '><div class="kv"><b>决策时点</b><span>旧日期</span></div><div class="big">2026-10-12</div></div>'
    + '<div class="card"><div class="kv"><b>我的持仓浮动盈亏</b></div><input id="private-input" value="123"></div></div>'
    + '<div id="sec-macro"' + (/report/.test(path) ? ' data-macro-history="true"' : '') + '><h2>宏观背景分与目标仓位</h2><div class="card">旧五模块解释</div></div>'
    + '<div id="sec-freshness"><table><tr><td>2026-10-12历史输入</td></tr></table></div>'
    + '<div class="card"><h3>数据新鲜度</h3><p>十类历史数据统计</p><p>宏观本质上不可能实时，不要指望它变快。<a href="data.html">详情</a></p></div>'
    + '<div id="sec-policy">历史政策内容</div></main>', path);
}
async function exercise(create, label) {
  for (const marked of [false, true]) {
    const doc = page('/live/macro.html', marked); let clock = initial, requests = 0, fail = false, payload = structuredClone(fixture);
    const calls = [];
    const controller = create(doc, { path: doc.location.pathname, cloud: label.includes('cloud'), calendar, now: () => clock, crypto: webcrypto,
      fetch: async (url, init) => { requests++; calls.push({ url, init }); if (fail) throw Error('offline'); return Response.json(payload); } });
    const score = doc.querySelector('[data-macro-card="score"] .big');
    assert.equal(score.textContent, '待核验', label + ':初始旧分不能冒充当前值');
    const privateInput = doc.getElementById('private-input'); const amount = doc.getElementById('__investment-amount'); amount.value = '3000';
    await controller.refresh();
    assert.equal(requests, 1, label + ':三处只共享一次请求');
    assert.equal(score.textContent, '55.55');
    assert.equal(doc.querySelector('[data-macro-card="position"] .big').textContent, '64.1%');
    assert.equal(doc.querySelector('[data-macro-card="computed_at"] .big').textContent, fixture.macro.computed_at);
    assert.match(doc.getElementById('__investment-result').textContent, /1923.00元/);
    assert.equal(doc.getElementById('__investment-amount'), amount);
    assert.equal(doc.getElementById('private-input'), privateInput);
    assert.equal(doc.getElementById('sec-policy').textContent, '历史政策内容');
    assert.equal(doc.querySelectorAll('[data-macro-live-details]').length, 1);
    assert.equal(doc.querySelectorAll('[data-macro-rule]').length, 5);
    assert.match(doc.querySelector('[data-macro-input="cpi"]').textContent, /观测期 2026-10-31/);
    assert.match(doc.querySelector('[data-macro-input="cpi"]').textContent, /官方披露 未知/);
    const disclosure = doc.querySelector('[data-macro-sources]'); disclosure.open = true;
    await controller.refresh(); assert.equal(requests, 1, '五分钟节流');
    fail = true; await controller.refresh(true);
    assert.equal(score.textContent, '55.55'); assert.equal(controller.state.status, 'historical');
    assert.match(doc.querySelector('[data-macro-card="score"] .kv span').textContent, /历史参考/);
    assert.match(doc.getElementById('__investment-result').textContent, /历史示例/);
    assert.equal(disclosure.open, true);
    fail = false; payload = signed({ ...structuredClone(fixture), built_at: '2026-11-16T17:59:00+08:00', macro: { ...structuredClone(fixture.macro), computed_at: '2026-11-16T17:59:00+08:00' } });
    await controller.refresh(true); assert.equal(controller.state.status, 'historical'); assert.equal(controller.state.value.snapshot_id, fixture.snapshot_id);
    payload = structuredClone(fixture); payload.macro.rules.cpi.explain = '<img src=x onerror=alert(1)>'; signed(payload);
    payload.built_at = '2026-11-16T18:01:00+08:00'; payload.macro.computed_at = payload.built_at; signed(payload); clock += 60000;
    await controller.refresh(true); assert.equal(controller.state.status, 'ok');
    assert.equal(doc.querySelector('img'), null, '外部解释只写textContent');
    assert.match(doc.querySelector('[data-macro-rule="cpi"]').textContent, /<img src=x/);
    assert.equal(disclosure.open, true);
    const beforeHide = requests; doc.hidden = true; controller.invalidate(); await controller.refresh(); assert.equal(requests, beforeHide);
    doc.hidden = false; await controller.refresh(); assert.equal(requests, beforeHide + 1, '恢复补查');
    assert.equal(amount.value, '3000');
    clock = Date.parse('2026-11-20T18:00:00+08:00'); controller.render(); assert.equal(controller.state.status, 'historical');
    assert.match(doc.getElementById('__investment-status').textContent, /暂停当前自动建议/);
    assert.ok(calls.every(call => call.init.redirect === 'manual' && call.init.cache === 'no-store'));
    assert.ok(calls.every(call => label.includes('cloud') ? call.url === '/api/investment/context' : call.url.endsWith('/investment-context-v2.json')));
  }
}
await exercise(api.createMacroEnvironment, 'module');
// 两套真实运行时从同步生成块中导出相同controller，不能只测试独立helper。
for (const runtime of browserTestRuntimes(import.meta.url)) {
  const context = { module: { exports: {} }, window: { __SITE__: {} }, TextEncoder, TextDecoder, Response, AbortSignal, crypto: webcrypto };
  runInNewContext(readFileSync(runtime.file, 'utf8'), context);
  assert.equal(typeof context.module.exports.createMacroEnvironment, 'function', '同步runtime必须包含v2控制器');
  await exercise(context.module.exports.createMacroEnvironment, runtime.cloud ? 'cloud runtime' : 'static runtime');
}
// 多次隐藏/恢复与在途请求交错时，补查只消费一次，不能留下强制重复请求。
const resumeDoc = page(); let resumeRequests = 0, completeFirst;
const resumeController = api.createMacroEnvironment(resumeDoc, { calendar, now: () => initial, crypto: webcrypto,
  fetch: async () => { resumeRequests++; if (resumeRequests === 1) return new Promise(resolve => { completeFirst = resolve; }); return Response.json(fixture); } });
const firstFlight = resumeController.refresh();
resumeDoc.hidden = true; resumeDoc.hidden = false; resumeController.invalidate(); await resumeController.refresh();
resumeDoc.hidden = true; completeFirst(Response.json(fixture)); await firstFlight;
resumeDoc.hidden = false; resumeController.invalidate(); await resumeController.refresh();
await resumeController.refresh(); assert.equal(resumeRequests, 2, '恢复补查后不能因旧forcePending再请求一次');
const homeDoc = page('/live/index.html');
const oldHomeStats = homeDoc.querySelectorAll('main .card h3').find(node => node.textContent === '数据新鲜度').parentNode.querySelector('p');
const homeController = api.createMacroEnvironment(homeDoc, { path: homeDoc.location.pathname, calendar, now: () => initial, fetch: async () => Response.json(fixture), crypto: webcrypto });
const homeCopy = homeDoc.querySelector('[data-macro-freshness-copy]');
assert.match(homeCopy.textContent, /每五分钟检查统一宏观源/);
assert.match(homeCopy.textContent, /新数据发布后可重新评分/);
assert.doesNotMatch(homeCopy.textContent, /不可能实时|不要指望/);
assert.equal(homeCopy.querySelector('a').getAttribute('href'), 'data.html');
assert.equal(oldHomeStats.textContent, '十类历史数据统计', '只更新对应说明段，不改旧统计');
homeController.render(); assert.equal(homeDoc.querySelector('[data-macro-freshness-copy]'), homeCopy, '不重复替换段落');
const historyDoc = page('/live/report.html');
api.createMacroEnvironment(historyDoc, { path: historyDoc.location.pathname, calendar, now: () => initial, fetch: async () => Response.json(fixture), crypto: webcrypto });
assert.equal(historyDoc.getElementById('sec-macro').textContent, '宏观背景分与目标仓位旧五模块解释', '完整历史报告不能被当前宏观覆盖');
assert.equal(historyDoc.querySelector('[data-macro-freshness-copy]'), null, '历史报告原说明不接管');
const dataDoc = page('/live/data.html');
const dataController = api.createMacroEnvironment(dataDoc, { path: dataDoc.location.pathname, calendar, now: () => initial, fetch: async () => Response.json(fixture), crypto: webcrypto });
await dataController.refresh();
assert.match(dataDoc.getElementById('sec-freshness').textContent, /历史发布快照/);
assert.match(dataDoc.getElementById('sec-freshness').textContent, /2026-10-12历史输入/);
assert.match(dataDoc.querySelector('[data-macro-input="cpi"]').textContent, /实际获取/);
console.log('Macro v2 digest, per-source freshness, old/new HTML, shared display, input retention, failure/recovery, XSS and dual-runtime contracts passed');
