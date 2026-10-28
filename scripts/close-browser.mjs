// 收盘快照与实时价格分别管理；失败时只保留有原始日期的已核验历史结果。
export function closeScoreIsCurrent(snapshot, now, calendarCheck) {
  const calendar = snapshot && snapshot.trading_calendar;
  if (!snapshot || !calendar || calendarCheck && !calendarCheck(calendar)) return false;
  const local = new Date(now + 28800000);
  let day = local.toISOString().slice(0, 10);
  if (day < calendar.start || day > calendar.end || !Array.isArray(calendar.closures)) return false;
  if (local.toISOString().slice(11, 16) < '15:00') day = new Date(Date.parse(day + 'T12:00:00Z') - 86400000).toISOString().slice(0, 10);
  for (let i = 0; i < 30; i++) {
    if (day < calendar.start) return false;
    const weekday = new Date(day + 'T12:00:00Z').getUTCDay();
    if (weekday !== 0 && weekday !== 6 && !calendar.closures.some(range => day >= range[0] && day <= range[1])) {
      return snapshot.score_date === day;
    }
    day = new Date(Date.parse(day + 'T12:00:00Z') - 86400000).toISOString().slice(0, 10);
  }
  return false;
}

export function validateCloseBrowserPayload(payload, now) {
  if (!payload || payload.schema_version !== 1 || payload.kind !== 'touzi-close-v1'
      || !/^\d{4}-\d{2}-\d{2}$/.test(payload.score_date || '')
      || !Number.isFinite(Date.parse(payload.built_at)) || Date.parse(payload.built_at) > now + 60000
      || !Number.isFinite(Date.parse(payload.quote_at)) || Date.parse(payload.quote_at) > Date.parse(payload.built_at)
      || new Date(Date.parse(payload.quote_at) + 28800000).toISOString().slice(0, 10) !== payload.score_date
      || new Date(Date.parse(payload.quote_at) + 28800000).toISOString().slice(11, 16) < '15:00'
      || !Array.isArray(payload.index) || !payload.index.length || payload.index.length > 10000 || !payload.entries || Array.isArray(payload.entries)) {
    throw new Error('收盘评分格式或日期未通过核验');
  }
  const seen = new Set();
  payload.index.forEach(item => {
    if (!item || !/^\d{6}$/.test(item.code) || seen.has(item.code) || typeof item.name !== 'string'
        || !['建仓','接近建仓','观望','回避','待核验','候选'].includes(item.action)
        || item.name.length > 100 || item.total !== null && (!Number.isFinite(item.total) || item.total < 0 || item.total > 100)) {
      throw new Error('收盘评分索引未通过核验');
    }
    seen.add(item.code);
  });
  const universe = payload.universe || {};
  if (!['market_total','eligible_total','attempted','scored','failed','excluded'].every(key => Number.isSafeInteger(universe[key]) && universe[key] >= 0)
      || universe.scored !== payload.index.length || universe.attempted !== universe.scored + universe.failed
      || universe.eligible_total !== universe.attempted || universe.market_total !== universe.eligible_total + universe.excluded) {
    throw new Error('收盘评分覆盖数量不一致');
  }
  if (!payload.plan || !payload.fees || !['sell_stop_loss','sell_take_profit','reduce_ratio'].every(key => Number.isFinite(payload.plan[key]))
      || payload.plan.sell_stop_loss < -1 || payload.plan.sell_stop_loss >= 0 || payload.plan.sell_take_profit <= 0
      || payload.plan.reduce_ratio < 0 || payload.plan.reduce_ratio > 1
      || !['commission_min','commission_rate','stamp_duty_rate','transfer_fee_rate'].every(key => Number.isFinite(payload.fees[key]) && payload.fees[key] >= 0)) {
    throw new Error('收盘评分规则或费率缺失');
  }
  Object.keys(payload.entries).forEach(code => {
    const entry = payload.entries[code];
    if (!seen.has(code) || !entry || entry.code && entry.code !== code
        || entry.total !== null && (!Number.isFinite(entry.total) || entry.total < 0 || entry.total > 100)
        || !Number.isFinite(entry.price) || entry.price <= 0 || !Number.isFinite(Date.parse(entry.quote_ts))
        || Date.parse(entry.quote_ts) > Date.parse(payload.built_at)
        || new Date(Date.parse(entry.quote_ts) + 28800000).toISOString().slice(0, 10) !== payload.score_date
        || new Date(Date.parse(entry.quote_ts) + 28800000).toISOString().slice(11, 16) < '15:00'
        || !Array.isArray(entry.buy_checks) || !entry.buy_checks.length || entry.buy_checks.length > 40
        || !entry.buy_checks.every(check => check && typeof check.key === 'string' && typeof check.label === 'string'
          && typeof check.hit === 'boolean' && typeof check.known === 'boolean' && (!check.hit || check.known)) || !entry.sell_snapshot
        || !['exit','reduce','warn','missing','missing_warn'].every(key => Array.isArray(entry.sell_snapshot[key]) && entry.sell_snapshot[key].every(value => typeof value === 'string'))) {
      throw new Error('收盘评分明细未通过核验');
    }
    const item = payload.index.find(value => value.code === code);
    if (entry.total !== item.total || entry.action !== item.action) throw new Error('收盘评分明细与索引不一致');
    if (entry.action === '建仓' && entry.buy_checks.some(check => !check.hit || !check.known)) throw new Error('收盘建仓条件未全部通过');
  });
  return payload;
}

function closeEscape(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
}

function closeSnapshotIdentity(payload) {
  return JSON.stringify(['kind', 'score_date', 'built_at', 'quote_at', 'universe', 'plan', 'fees', 'index', 'shards']
    .map(key => payload[key] === undefined ? null : payload[key]));
}

// 收盘后允许当日新公开风险首次提醒，旧收盘重复复核与个人成本变化继续抑制。
export function closePublicChanges(changed, previous, entries, snapshot, current, clock, financial) {
  if (!clock || clock.phase !== 'closed') return changed;
  if (!snapshot || !current || snapshot.score_date !== clock.day) return [];
  if (financial) return changed;
  return changed.flatMap(item => {
    const entry = entries[item.code], summary = entry && entry.sell_snapshot;
    if (!summary) return [];
    const exits = summary.exit || [], reductions = summary.reduce || [];
    const action = exits.length ? '清仓' : reductions.length ? '减仓' : '';
    const reasons = (action === '清仓' ? exits : reductions).filter(reason => item.reasons.includes(reason));
    const old = previous[item.code];
    if (!reasons.length || old && old.action === action && reasons.every(reason => old.reasons.includes(reason))) return [];
    return [Object.assign({}, item, { action, reasons })];
  });
}

export function createCloseBrowser(options) {
  const state = { snapshot: null, index: [], entries: {}, drafts: {}, query: '', action: '', page: 1, pageSize: 100,
    visible: [], status: 'waiting', error: '', lastAttempt: 0, loading: false, attempted: false };
  const request = options.fetch;
  const now = options.now || (() => Date.now());
  const pending = new Map(), parts = new Map();
  let viewRequest = 0;
  function notify() { if (options.onChange) options.onChange(state); }
  function matching() {
    const query = state.query.toLowerCase();
    return state.index.filter(item => (!query || item.code.includes(query) || item.name.toLowerCase().includes(query))
      && (!state.action || item.action === state.action));
  }
  function viewCodes() { return matching().slice((state.page - 1) * state.pageSize, state.page * state.pageSize).map(item => item.code); }
  function accept(payload, setVisible) {
    validateCloseBrowserPayload(payload, now());
    if (state.snapshot && payload.score_date === state.snapshot.score_date
        && closeSnapshotIdentity(payload) !== closeSnapshotIdentity(state.snapshot)) {
      throw new Error('同日合格收盘评分不能被另一版本覆盖');
    }
    if (state.snapshot && (payload.score_date < state.snapshot.score_date
        || payload.score_date === state.snapshot.score_date && Date.parse(payload.built_at) < Date.parse(state.snapshot.built_at))) return false;
    const same = state.snapshot && payload.score_date === state.snapshot.score_date && payload.built_at === state.snapshot.built_at;
    state.attempted = true;
    state.entries = Object.assign(same ? state.entries : {}, payload.entries);
    state.snapshot = payload; state.index = payload.index;
    state.status = payload.status === 'delayed' || !closeScoreIsCurrent(payload, now(), options.calendarCheck) ? 'delayed' : 'ok';
    state.error = Array.isArray(payload.errors) ? payload.errors.join('；') : '';
    if (setVisible) state.visible = viewCodes();
    notify(); return true;
  }
  async function json(url, maximum) {
    const response = await request(url, { cache: 'no-store', redirect: 'manual', signal: AbortSignal.timeout(15000) });
    if (!response.ok) throw new Error('收盘评分来源暂不可用');
    if (Number(response.headers && response.headers.get('content-length') || 0) > maximum) throw new Error('收盘评分响应过大');
    if (response.arrayBuffer) {
      let bytes;
      if (response.body && response.body.getReader) {
        const reader = response.body.getReader(), chunks = [];
        let length = 0;
        try {
          for (;;) {
            const chunk = await reader.read(); if (chunk.done) break;
            length += chunk.value.byteLength;
            if (length > maximum) throw new Error('收盘评分响应过大');
            chunks.push(chunk.value);
          }
        } finally { await reader.cancel().catch(() => {}); }
        bytes = new Uint8Array(length);
        let offset = 0; chunks.forEach(chunk => { bytes.set(chunk, offset); offset += chunk.byteLength; });
      } else bytes = new Uint8Array(await response.arrayBuffer());
      if (bytes.byteLength > maximum) throw new Error('收盘评分响应过大');
      return { value: JSON.parse(new TextDecoder().decode(bytes)), bytes };
    }
    const value = await response.json();
    if (JSON.stringify(value).length > maximum) throw new Error('收盘评分响应过大');
    return { value, bytes: null };
  }
  async function staticPayload(codes) {
    let base = '';
    let manifest;
    try { manifest = (await json('close-scores.json', 2 * 1024 * 1024)).value; }
    catch {
      base = 'https://raw.githubusercontent.com/qiliu7060-beep/touzi/close-data/';
      manifest = (await json(base + 'close-scores.json', 2 * 1024 * 1024)).value;
    }
    const shell = Object.assign({}, manifest, { entries: {} });
    validateCloseBrowserPayload(shell, now());
    if (!Array.isArray(manifest.shards) || manifest.shards.length > 1000) throw new Error('收盘评分分片目录缺失');
    const declared = new Map(), manifestCodes = new Set();
    manifest.shards.forEach(descriptor => {
      if (!descriptor || typeof descriptor.path !== 'string' || declared.has(descriptor.path)
          || !/^close-score-parts\/\d{4}-\d{2}-\d{2}-[a-f0-9]+-\d+\.json$/.test(descriptor.path)
          || !/^[a-f0-9]{64}$/.test(descriptor.sha256) || !Array.isArray(descriptor.codes)
          || !descriptor.codes.length || descriptor.codes.length > 100) throw new Error('收盘分片目录无效');
      declared.set(descriptor.path, descriptor);
      descriptor.codes.forEach(code => { if (!/^\d{6}$/.test(code) || manifestCodes.has(code)) throw new Error('收盘分片代码重复或无效'); manifestCodes.add(code); });
    });
    if (manifestCodes.size !== manifest.index.length || manifest.index.some(item => !declared.has(item.shard)
        || !declared.get(item.shard).codes.includes(item.code))) throw new Error('收盘目录与索引不一致');
    const index = new Map(manifest.index.map(item => [item.code, item]));
    const wanted = codes || manifest.index.filter(item => (!state.query || (item.code + ' ' + item.name).toLowerCase().includes(state.query.toLowerCase()))
      && (!state.action || item.action === state.action)).slice((state.page - 1) * state.pageSize, state.page * state.pageSize).map(item => item.code);
    const paths = [...new Set(wanted.map(code => index.get(code) && index.get(code).shard).filter(Boolean))];
    const entries = {};
    // 分片逐个读取，避免多个4MiB响应同时占用内存。
    for (const path of paths) {
      const descriptor = manifest.shards.find(item => item.path === path);
      if (!/^close-score-parts\/\d{4}-\d{2}-\d{2}-[a-f0-9]+-\d+\.json$/.test(path)
          || !descriptor || !/^[a-f0-9]{64}$/.test(descriptor.sha256) || !Array.isArray(descriptor.codes) || descriptor.codes.length > 100) {
        throw new Error('收盘评分分片路径未通过核验');
      }
      const key = path + ':' + descriptor.sha256;
      let part = parts.get(key);
      if (!part) {
        const loaded = await json(base + path, 4 * 1024 * 1024);
        const crypto = options.crypto || (typeof globalThis !== 'undefined' && globalThis.crypto);
        if (!loaded.bytes || !crypto || !crypto.subtle) throw new Error('当前环境无法核验收盘分片摘要');
        const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', loaded.bytes))).map(value => value.toString(16).padStart(2, '0')).join('');
        if (digest !== descriptor.sha256) throw new Error('收盘评分分片摘要不一致');
        part = loaded.value;
        if (part.schema_version !== 1 || part.kind !== 'touzi-close-part-v1' || part.score_date !== manifest.score_date
            || !part.entries || Object.keys(part.entries).length !== descriptor.codes.length
            || descriptor.codes.some(code => !Object.prototype.hasOwnProperty.call(part.entries, code)
              || !index.has(code) || index.get(code).shard !== path)) throw new Error('收盘评分分片内容不一致');
        // 完整分片校验后才进入缓存，不能仅验证本页选中的一行。
        validateCloseBrowserPayload(Object.assign({}, manifest, { entries: part.entries }), now());
        parts.set(key, part);
        if (parts.size > 30) parts.delete(parts.keys().next().value);
      }
      wanted.forEach(code => { if (part.entries[code]) entries[code] = part.entries[code]; });
    }
    return Object.assign({}, manifest, { entries, status: 'ok', errors: [] });
  }
  async function load(codes) {
    const generation = codes ? 0 : ++viewRequest;
    if (!request) return false;
    if (codes && (!Array.isArray(codes) || codes.length > 100 || codes.some(code => !/^\d{6}$/.test(code)))) throw new Error('收盘评分股票查询无效');
    state.loading = true; state.attempted = true; state.lastAttempt = now();
    try {
      let payload;
      if (options.cloud) {
        const query = codes ? 'codes=' + codes.join(',') : 'page=' + state.page + '&page_size=' + state.pageSize
          + '&q=' + encodeURIComponent(state.query) + '&action=' + encodeURIComponent(state.action);
        payload = (await json('/api/scores/close?' + query, 8 * 1024 * 1024)).value;
      } else payload = await staticPayload(codes);
      if (!codes && generation !== viewRequest) return false;
      if (payload.status === 'unavailable') throw new Error((payload.errors || []).join('；') || '尚无合格收盘评分');
      return accept(payload, !codes);
    } catch (error) {
      if (codes || generation === viewRequest) {
        state.error = String(error.message || error); state.status = state.snapshot ? 'delayed' : 'unavailable'; notify();
      }
      return false;
    } finally { state.loading = false; }
  }
  function ensure(codes) {
    const missing = [...new Set(codes)].filter(code => /^\d{6}$/.test(code) && state.index.some(item => item.code === code) && !state.entries[code]);
    if (!missing.length) return Promise.resolve(true);
    const key = missing.slice().sort().join(',');
    if (pending.has(key)) return pending.get(key);
    const result = (async () => {
      for (let i = 0; i < missing.length; i += 100) if (!await load(missing.slice(i, i + 100))) return false;
      return true;
    })().finally(() => pending.delete(key));
    pending.set(key, result); return result;
  }
  function remember(row) {
    const code = row.getAttribute('data-pick'), checkbox = row.querySelector('input[type=checkbox]');
    const price = row.querySelector('input.ip'), shares = row.querySelector('input.is');
    if (!/^\d{6}$/.test(code) || !checkbox || !price || !shares) return;
    state.drafts[code] = { code, name: row.getAttribute('data-name') || '', checked: checkbox.checked, price: price.value, shares: shares.value };
  }
  return { state, accept, load, ensure, matching, viewCodes, remember,
    isCurrent: () => !!state.snapshot && state.status === 'ok' && closeScoreIsCurrent(state.snapshot, now(), options.calendarCheck),
    selected: () => Object.values(state.drafts).filter(item => item.checked),
    setView(query, action, page) { state.query = String(query || '').trim().slice(0, 80); state.action = String(action || ''); state.page = Math.max(1, Math.floor(page || 1)); return load(); },
    refresh(codes) { return load().then(async accepted => { if (accepted) return ensure(codes || []); return false; }); } };
}

export function createCloseSelectionView(doc, controller, options) {
  const state = controller.state, rows = new Map();
  let host = null, body = null, signature = '';
  function capture() { if (body) Array.from(body.querySelectorAll('tr[data-pick]')).forEach(controller.remember); }
  function ensureHost() {
    if (host) return true;
    const original = Array.from(doc.querySelectorAll('tr[data-pick]'));
    if (!original.length && !options.isPickPage) return false;
    const main = doc.querySelector('main'); if (!main) return false;
    // 兼容旧发布HTML：说明位于旧卡片之外，接管后必须一并更新。
    const intro = main.querySelector('p.sub');
    if (intro) intro.textContent = '收盘评分固定在标明的交易日；适用行业须同时满足原买入条件、债务股权比率<0.8和五年资本开支比例<25%。行业豁免单列，不表示数值达标；缺失或过期时待核验。勾选、填写股数后可加入持仓。';
    original.forEach(row => {
      const checkbox = row.querySelector('input[type=checkbox]');
      const changed = checkbox && checkbox.checked || ['input.ip','input.is'].some(selector => {
        const input = row.querySelector(selector);
        return input && input.value !== (input.getAttribute('value') || '');
      });
      // 仅迁移用户修改；旧HTML的未编辑默认买入价不能压过最新收盘价。
      if (changed) controller.remember(row);
      const card = row.closest('.card'); if (card) { card.hidden = true; card.setAttribute('data-close-replaced', ''); }
    });
    host = doc.createElement('section'); host.id = '__close-selection'; host.className = 'card';
    host.innerHTML = '<h3>全 A 股收盘选股</h3><p class="small" id="__close-coverage"></p>'
      + '<p class="muted small">评分、买入条件和财务指标固定在标明的收盘日；适用行业须同时满足原买入条件、债务股权比率&lt;0.8和五年资本开支比例&lt;25%。行业豁免单列，不表示数值达标。盘中现价只用于行情和个人成本检查。</p>'
      + '<form id="__close-filter"><label>搜索股票（中文名称或代码） <input id="__close-query" type="search" maxlength="80" autocomplete="off"></label> '
      + '<label>收盘建议 <select id="__close-action"><option value="">全部</option><option>建仓</option><option>接近建仓</option><option>观望</option><option>回避</option><option>候选</option></select></label> <button type="submit" class="b">搜索</button></form>'
      + '<p id="__close-page-state" class="small" role="status"></p><div class="scroll"><table><thead><tr><th>选</th><th>代码</th><th>名称</th><th>收盘建议</th><th>收盘总分</th><th>收盘价</th><th>收盘 PE</th><th>收盘 PB</th><th>收盘股息率%</th>'
      + '<th data-pick-financial-header="debt_equity">债务股权比率（优良&lt;0.8）</th><th data-pick-financial-header="capex_to_profit">五年资本开支÷净利润（达标&lt;25%）</th><th data-pick-buy-checks>收盘买入条件</th><th>买入价</th><th>股数</th></tr></thead><tbody id="__close-rows"></tbody></table></div>'
      + '<p><button id="__close-prev" type="button" class="b b2">上一页</button> <button id="__close-next" type="button" class="b b2">下一页</button> <span id="__close-selected" class="small"></span></p>';
    const firstCard = original[0] && original[0].closest('.card');
    if (firstCard) main.insertBefore(host, firstCard); else main.appendChild(host);
    Array.from(doc.querySelectorAll('button')).forEach(button => {
      const handler = button.getAttribute('onclick') || '';
      if (handler.includes("__pickAll('ready')")) button.textContent = '本页全选「建仓」';
      if (handler.includes("__pickAll('near')")) button.textContent = '本页全选「接近建仓」';
    });
    body = doc.getElementById('__close-rows');
    doc.getElementById('__close-filter').addEventListener('submit', event => {
      event.preventDefault(); const input = doc.getElementById('__close-query'); if (input.__composing) return;
      capture(); controller.setView(input.value, doc.getElementById('__close-action').value, 1);
    });
    const input = doc.getElementById('__close-query');
    input.addEventListener('compositionstart', () => { input.__composing = true; });
    input.addEventListener('compositionend', () => { input.__composing = false; });
    input.addEventListener('keydown', event => { if (event.key === 'Enter' && (event.isComposing || input.__composing || event.keyCode === 229)) event.preventDefault(); });
    doc.getElementById('__close-prev').addEventListener('click', () => { capture(); controller.setView(state.query, state.action, Math.max(1, state.page - 1)); });
    doc.getElementById('__close-next').addEventListener('click', () => { capture(); controller.setView(state.query, state.action, state.page + 1); });
    return true;
  }
  function render() {
    if (!ensureHost()) return;
    capture();
    const snapshot = state.snapshot, universe = snapshot && snapshot.universe;
    doc.getElementById('__close-coverage').textContent = snapshot ? '最近合格收盘评分：' + snapshot.score_date + ' · 生成 ' + snapshot.built_at
      + ' · 市场 ' + universe.market_total + '只，粗筛通过 ' + universe.eligible_total + '只，尝试 ' + universe.attempted + '只，已评分 ' + universe.scored + '只，失败 ' + universe.failed + '只，粗筛排除 ' + universe.excluded + '只'
      + (state.status === 'delayed' ? ' · 更新延迟，保留历史收盘结果' : '') : '尚无合格收盘评分，等待收盘数据发布。';
    const matches = controller.matching(), codes = controller.viewCodes();
    const nextSignature = (snapshot && snapshot.built_at || '') + '|' + codes.join(',');
    if (signature !== nextSignature) {
      while (body.firstChild) body.removeChild(body.firstChild);
      codes.forEach(code => {
        const item = state.index.find(value => value.code === code), entry = state.entries[code] || {};
        let row = rows.get(code);
        if (!row) {
          row = doc.createElement('tr'); row.setAttribute('data-pick', code); row.setAttribute('data-name', item.name); row.setAttribute('data-close-pick', '');
          row.innerHTML = '<td><input type="checkbox" aria-label="选择 ' + closeEscape(item.name) + '"></td><td>' + closeEscape(code) + '</td><td>' + closeEscape(item.name) + '</td>'
            + '<td></td><td></td><td></td><td></td><td></td><td></td><td class="small" data-pick-financial="debt_equity"></td><td class="small" data-pick-financial="capex_to_profit"></td><td class="small" data-pick-buy-checks></td><td><input type="text" class="ip" aria-label="' + closeEscape(code) + ' 买入价"></td><td><input type="text" class="is" aria-label="' + closeEscape(code) + ' 股数"></td>';
          const draft = state.drafts[code] || { checked: false, price: entry.price == null ? '' : Number(entry.price).toFixed(3), shares: '100' };
          row.querySelector('input[type=checkbox]').checked = draft.checked;
          row.querySelector('input.ip').value = draft.price; row.querySelector('input.is').value = draft.shares;
          ['change','input'].forEach(event => row.addEventListener(event, () => { controller.remember(row); row.className = row.querySelector('input[type=checkbox]').checked ? 'picked' : ''; renderSelected(); }));
          rows.set(code, row);
        }
        body.appendChild(row);
      });
      signature = nextSignature;
    }
    codes.forEach(code => {
      const row = rows.get(code), item = state.index.find(value => value.code === code), entry = state.entries[code];
      const cells = row.querySelectorAll('td');
      row.setAttribute('data-name', item.name); cells[2].textContent = item.name;
      cells[3].textContent = item.action;
      [['total',4],['price',5],['pe',6],['pb',7],['dy',8]].forEach(([key, column]) => { cells[column].textContent = entry && typeof entry[key] === 'number' ? entry[key].toFixed(key === 'price' ? 3 : 2) : key === 'total' && typeof item.total === 'number' ? item.total.toFixed(2) : '—'; });
      cells[4].title = '固定收盘评分 · ' + (snapshot && snapshot.score_date || '未知日期');
      const checks = entry && entry.buy_checks || [];
      cells[11].textContent = checks.length ? checks.filter(check => !check.known || !check.hit).map(check => check.label + (check.note ? '（' + check.note + '）' : '')).join('；') || '收盘买入条件通过（含适用财务条件）' : '收盘条件明细待加载';
      row.className = row.querySelector('input[type=checkbox]').checked ? 'picked' : '';
    });
    doc.getElementById('__close-page-state').textContent = matches.length ? '共 ' + matches.length + '只，第 ' + state.page + '/' + Math.ceil(matches.length / state.pageSize) + '页（每页' + state.pageSize + '只）' + (state.error ? ' · ' + state.error : '') : state.error || (snapshot ? '没有匹配股票。' : '正在等待合格收盘评分。');
    doc.getElementById('__close-prev').disabled = state.page <= 1;
    doc.getElementById('__close-next').disabled = state.page * state.pageSize >= matches.length;
    // 只缓存少量离屏行；表单草稿独立按代码保存，避免浏览全市场后累积十万级DOM节点。
    if (rows.size > 300) {
      for (const [code] of rows) {
        if (rows.size <= 300) break;
        if (!codes.includes(code)) rows.delete(code);
      }
    }
    renderSelected();
    if (options.onRendered) options.onRendered();
  }
  function renderSelected() { const target = doc.getElementById('__close-selected'); if (target) target.textContent = '跨页已勾选 ' + controller.selected().length + '只；价格、股数按代码保留'; }
  function selectAll(which) {
    capture();
    // 明确只全选当前页，避免一次误加入数千笔；其他页已有勾选保留。
    if (which !== 'ready' && which !== 'near') {
      Object.values(state.drafts).forEach(draft => { draft.checked = false; });
      rows.forEach(row => { row.querySelector('input[type=checkbox]').checked = false; });
    } else rows.forEach((row, code) => {
      if (!controller.viewCodes().includes(code)) return;
      const item = state.index.find(value => value.code === code);
      row.querySelector('input[type=checkbox]').checked = item.action === (which === 'ready' ? '建仓' : '接近建仓'); controller.remember(row);
    });
    render();
  }
  return { render, capture, selectAll, selected() { capture(); return controller.selected(); }, visibleCodes: () => controller.viewCodes() };
}
