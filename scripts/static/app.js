/* ===========================================================================
 * 公网版（纯静态）网站的前端引擎。
 *
 * 本机版（scripts/serve.py）的实时能力来自后台线程 + /api/quotes。静态版没有
 * 后端，所以这一层把「服务器原来干的事」搬到浏览器里：
 *
 *   1. 行情：浏览器**直连腾讯** https://qt.gtimg.cn（实测返回
 *      `Access-Control-Allow-Origin: *`，所以任何域名的页面都能直接 fetch）。
 *   2. 持仓台账：存在**浏览器本地** localStorage，不上传任何地方。
 *      公共财务与宏观条件由定时发布计算；PE/PB 历史分位按有效报价重算；个人止损/止盈按报价和
 *      本地买入价现算。两者任一触发就给出减仓或清仓，未覆盖的股票不报平安。
 *   3. 钱（现价 / 市值 / 浮动盈亏 / 扣费后净盈利）用与 `touzi/portfolio.py`
 *      **逐行对齐**的公式现算 —— 费率、最低佣金、印花税、过户费、卖出比例、
 *      向下取整到 100 股，全部照抄，见下面 fee* / oneRow()。
 *
 * 页面上的价格类单元格都带 `data-q="代码.字段"`。apply() 与服务器版
 * scripts/webui.py 的 live_js() 是同一套约定（同一批字段名），
 * 所以同一份 HTML 在本机和公网两种模式下都能被正确改写。
 * =========================================================================== */
(function () {
  'use strict';
  // MARKET_CALENDAR_START (generated)
// 日历常量由 scripts/sync_live_runtime.py 从经核验公告数据生成。
const MARKET_CALENDAR = {"version":"cn-a-2026-v1","start":"2026-01-01","end":"2026-12-31","exchanges":["SSE","SZSE","BSE"],"sources":["https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml","https://www.szse.cn/disclosure/notice/general/t20251222_618087.html","https://www.bse.cn/important_news/200027428.html"],"closures":[["2026-01-01","2026-01-04"],["2026-02-15","2026-02-23"],["2026-04-04","2026-04-06"],["2026-05-01","2026-05-05"],["2026-06-19","2026-06-21"],["2026-09-25","2026-09-27"],["2026-10-01","2026-10-07"]]};
const DAY_MS = 86400000;
const OFFSET_MS = 28800000;
/** @param {unknown} value */
function validCalendar(value) {
  return JSON.stringify(value) === JSON.stringify(MARKET_CALENDAR);
}
/** @param {string} day */
function tradingDay(day) {
  if (day < MARKET_CALENDAR.start || day > MARKET_CALENDAR.end) return null;
  const weekday = new Date(day + 'T12:00:00+08:00').getUTCDay();
  return weekday !== 0 && weekday !== 6 && !MARKET_CALENDAR.closures.some(([a, b]) => day >= a && day <= b);
}
/** @param {string} day @param {number} direction */
function adjacentTradingDay(day, direction) {
  let stamp = Date.parse(day + 'T00:00:00Z');
  // 公告日历外及超过20个自然日均拒绝，不推测新年度或临时停市。
  for (let i = 0; i < 20; i++) {
    stamp += direction * DAY_MS;
    const candidate = new Date(stamp).toISOString().slice(0, 10);
    const open = tradingDay(candidate);
    if (open === null) return null;
    if (open) return candidate;
  }
  return null;
}
/** @param {number} now @param {unknown} calendar */
function marketClock(now, calendar) {
  if (!Number.isFinite(now) || !validCalendar(calendar)) return null;
  const local = new Date(now + OFFSET_MS), day = local.toISOString().slice(0, 10);
  const open = tradingDay(day);
  if (open === null) return null;
  const minute = local.getUTCHours() * 60 + local.getUTCMinutes();
  const phase = !open || minute < 570 || minute >= 900 ? 'closed'
    : minute >= 690 && minute < 780 ? 'break' : 'trading';
  const quoteDay = open && minute >= 570 ? day : adjacentTradingDay(day, -1);
  return quoteDay ? { day, phase, quoteDay, open } : null;
}
/** @param {{price:number,quote_ts:string}} quote @param {number} now @param {unknown} calendar */
function calendarQuoteFresh(quote, now, calendar) {
  const clock = marketClock(now, calendar), stamp = Date.parse(quote.quote_ts);
  if (!clock || !Number.isFinite(stamp) || !(quote.price > 0) || stamp > now + 60000) return null;
  const local = new Date(stamp + OFFSET_MS), day = local.toISOString().slice(0, 10);
  const minute = local.getUTCHours() * 60 + local.getUTCMinutes();
  if (day !== clock.quoteDay || !tradingDay(day)) return null;
  if (clock.phase === 'trading') return now - stamp <= 120000 ? 'trading' : null;
  if (clock.phase === 'break') return minute >= 688 && minute <= 690 ? 'break' : null;
  return minute >= 900 && minute < 1080 ? 'closed' : null;
}
/** @param {number} built @param {string} quoteAt @param {unknown} calendar */
function calendarModelExpiry(built, quoteAt, calendar) {
  if (!validCalendar(calendar) || !marketClock(built, calendar)) return null;
  // 只有生成时已核验的完整收盘基线才能跨休市；盘中模型仍保留36小时上限。
  if (calendarQuoteFresh({price:1,quote_ts:quoteAt}, built, calendar) !== 'closed') return built + 36 * 3600000;
  const day = new Date(Date.parse(quoteAt) + OFFSET_MS).toISOString().slice(0, 10);
  const next = adjacentTradingDay(day, 1);
  return next ? Date.parse(next + 'T09:30:00+08:00') : null;
}
/** @param {number} built @param {string} quoteAt @param {number} now @param {unknown} calendar */
function closedBaseline(built, quoteAt, now, calendar) {
  const expiry = calendarModelExpiry(built, quoteAt, calendar);
  return expiry !== null && built <= now + 60000 && now < expiry
    && now - built <= 20 * DAY_MS
    && calendarQuoteFresh({price:1,quote_ts:quoteAt}, built, calendar) === 'closed'
    && calendarQuoteFresh({price:1,quote_ts:quoteAt}, now, calendar) === 'closed';
}

// 可用指标、行业适用性和展示字段完整性分别统计，不将行情覆盖冒充财报覆盖。
/** @param {Record<string, any>} entries */
function financialCoverage(entries) {
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

// 选股表只展示已有财务判断，不重新计算评分或增加买入否决条件。
function pickFinancialMetric(entry, key, mode) {
  const analysis = entry && entry.financial_analysis;
  const metric = analysis && entry.financial_capability !== false && analysis.metrics && analysis.metrics[key] || {};
  const debt = key === 'debt_equity';
  const finite = typeof metric.value === 'number' && Number.isFinite(metric.value);
  const value = finite ? (metric.value * (debt ? 1 : 100)).toFixed(2) + (debt ? ' 倍' : '%') : '—';
  const state = metric.status || 'missing';
  let status = '数据不足', reason = debt ? '缺少有效负债或合并股东权益' : '五年缺少完整年度或必要金额';
  if (!analysis || entry.financial_capability === false) {
    reason = entry && entry.financial_capability === false ? '旧模型未提供这项财务分析' : '当前评分未覆盖这项财务数据';
  } else if (state === 'not_applicable') {
    status = '行业不适用'; reason = '本行业不机械套用该通用标准';
  } else if (state === 'stale') {
    status = '旧数据'; reason = '财报获取时间或报告期已过期';
  } else if (state === 'invalid') {
    status = '无法计算';
    reason = debt && typeof metric.total_equity === 'number' && metric.total_equity <= 0
      ? '合并股东权益非正，比例无法计算'
      : !debt && typeof metric.net_profit_sum === 'number' && metric.net_profit_sum <= 0
        ? '五年累计净利润非正，比例无法计算' : '原始金额无效，比例无法核验';
  } else if (state === 'ok' && finite && typeof metric.excellent === 'boolean') {
    status = metric.excellent ? (debt ? '优良' : '达标') : (debt ? '未达优良标准' : '未达标');
    reason = '原始值' + (metric.excellent ? '严格低于' : '未严格低于') + (debt ? '0.8' : '25%') + '，不按显示舍入值判断';
  } else if (state === 'missing' && !String(entry && entry.industry || '').trim()) {
    status = '行业待核验'; reason = '行业归属未知，无法判断标准适用性';
  }
  const window = analysis && analysis.window || {};
  const period = debt ? '负债报告期 ' + (metric.report_date || '未知')
    : '五年窗口 ' + (window.start_year && window.end_year ? window.start_year + '—' + window.end_year : '未知');
  const source = period + ' · 获取 ' + (analysis && analysis.fetched_at || '未知');
  const qualifier = mode === 'verified' ? '本轮数据来源已核验' : mode === 'snapshot' ? '发布快照' : '待核验（上次数据）';
  if (mode !== 'verified' && mode !== 'snapshot') {
    reason = '当前数据未通过核验；上次状态：' + status + '。' + reason;
    status = '待核验';
  }
  return { value, status, reason, source, qualifier, raw: finite ? String(metric.value) : '未知' };
}

function pickFinancialEscape(value) {
  return String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

function qualifyPickBuyText(node) {
  // 只改文字节点，保留既有预警span以及其它DOM引用。
  for (const child of Array.from(node.childNodes || [])) {
    if (child.nodeType === 3) child.nodeValue = child.nodeValue.replace(/全部通过/g, '原买入门槛通过');
    else qualifyPickBuyText(child);
  }
}

function updatePickFinancialColumns(doc, entries, modeFor) {
  const columns = [['debt_equity', '债务股权比率（优良<0.8）'],
    ['capex_to_profit', '五年资本开支÷净利润（达标<25%）']];
  const rows = Array.from(doc.querySelectorAll('tr[data-pick]'));
  // 兼容旧发布页的深色选中背景；只补样式，不改勾选行为和表单节点。
  if (rows.length && doc.head && !doc.querySelector('style[data-pick-selection-style]')) {
    const style = doc.createElement('style'); style.id = '__pick-selection-style'; style.setAttribute('data-pick-selection-style', '');
    style.textContent = 'tr[data-pick].picked{background:#e5f6ec;color:#1f2937}tr[data-pick].picked .muted{color:#526275}';
    doc.head.appendChild(style);
  }
  let updated = 0;
  for (const row of rows) {
    if (!row.closest || !row.querySelector || !row.insertBefore) continue;
    const table = row.closest('table'), header = table && table.querySelector('tr');
    if (!header) continue;
    const originalHeaders = Array.from(header.querySelectorAll('th')).filter(cell => !cell.hasAttribute('data-pick-financial-header'));
    const originalCells = Array.from(row.querySelectorAll('td')).filter(cell => !cell.hasAttribute('data-pick-financial'));
    const buyChecks = row.querySelector('[data-pick-buy-checks]') || originalCells[9];
    const buyHeader = header.querySelector('[data-pick-buy-checks]') || originalHeaders[9];
    if (!buyChecks || !buyHeader) continue;
    buyChecks.setAttribute('data-pick-buy-checks', '');
    buyHeader.setAttribute('data-pick-buy-checks', '');
    buyHeader.textContent = '原买入门槛（发布时）';
    qualifyPickBuyText(buyChecks);
    const entry = entries[row.getAttribute('data-pick')];
    for (const [key, label] of columns) {
      let heading = header.querySelector('[data-pick-financial-header="' + key + '"]');
      if (!heading) {
        heading = doc.createElement('th'); heading.setAttribute('data-pick-financial-header', key);
        header.insertBefore(heading, buyHeader);
      }
      heading.textContent = label;
      heading.style.whiteSpace = 'normal'; heading.style.minWidth = '180px'; heading.style.width = '210px';
      let cell = row.querySelector('[data-pick-financial="' + key + '"]');
      if (!cell) {
        cell = doc.createElement('td'); cell.setAttribute('data-pick-financial', key); cell.className = 'small';
        row.insertBefore(cell, buyChecks);
      }
      const view = pickFinancialMetric(entry, key, modeFor(entry));
      cell.innerHTML = '<strong>' + pickFinancialEscape(view.value) + '</strong><br><b>' + pickFinancialEscape(view.status)
        + '</b><br><span>' + pickFinancialEscape(view.reason) + '</span><br><span class="muted">'
        + pickFinancialEscape(view.qualifier + ' · ' + view.source) + '</span>';
      cell.title = '未舍入原值 ' + view.raw + '；' + view.source;
      cell.style.minWidth = '180px'; cell.style.width = '210px'; cell.style.maxWidth = '240px';
      cell.style.whiteSpace = 'normal'; cell.style.overflowWrap = 'anywhere';
    }
    const card = table.closest('.card');
    if (card && !card.querySelector('[data-pick-financial-note]')) {
      const note = doc.createElement('p'); note.className = 'small muted'; note.setAttribute('data-pick-financial-note', '');
      note.textContent = '两项财务指标参与既有质量评分和财务预警，未新增为买入硬门槛；原买入门槛通过不代表这两项均达标。窄屏可左右滑动查看两项指标。';
      const scroll = table.closest('.scroll');
      if (scroll && scroll.parentNode === card) card.insertBefore(note, scroll);
      else card.appendChild(note);
    }
    updated++;
  }
  return updated;
}
  // MARKET_CALENDAR_END

  // Node 里跑单元测试时没有 window / document，所以全部做存在性判断。
  var W = (typeof window !== 'undefined') ? window : {};
  var HAS_DOM = (typeof document !== 'undefined' && !!document.getElementById);
  var SITE = (typeof window !== 'undefined' && window.__SITE__) || {};
  var ENTRIES = SITE.entries || {};
  var FEES = SITE.fees || {};
  var PLAN = SITE.plan || {};
  var BENCHMARKS = SITE.benchmarks || { instruments: [] };
  var IV = (SITE.interval || 15) * 1000;
  var LEDGER_KEY = 'touzi.ledger.v1';

  /* ---------------------------------------------------------------- 行情 */

  // 与 touzi/data/stock.py 的 to_tx_symbol 一致
  function txSym(code) {
    var c = String(code || '').trim();
    if (/^(60|68|90|11|5)/.test(c)) { return 'sh' + c; }
    if (/^(4|8|92)/.test(c)) { return 'bj' + c; }
    return 'sz' + c;
  }

  // 腾讯 v_sh600519="1~贵州茅台~600519~1255.79~..." 的字段下标，
  // 与 touzi/data/stock.py 的 _TX_FIELDS 逐个对齐（88 个字段）。
  var F = {
    name: 1, code: 2, price: 3, prev_close: 4, open: 5,
    quote_time: 30, change: 31, change_pct: 32, high: 33, low: 34,
    volume_lots: 36, amount_wan: 37, turnover_rate: 38, pe_ttm: 39,
    amplitude: 43, float_mktcap_yi: 44, total_mktcap_yi: 45, pb: 46
  };
  var NUM = ['price', 'prev_close', 'open', 'change', 'change_pct', 'high', 'low',
    'volume_lots', 'amount_wan', 'turnover_rate', 'pe_ttm', 'amplitude',
    'float_mktcap_yi', 'total_mktcap_yi', 'pb'];

  function decodeGBK(buf) {
    try { return new TextDecoder('gbk').decode(buf); }
    catch (e) { return new TextDecoder('utf-8').decode(buf); }   // 名字会乱码，数字照样对
  }

  function parseQuoteText(text) {
    var out = {}, parts = String(text).split(';');
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i].trim();
      if (p.indexOf('=') < 0) { continue; }
      var body = p.slice(p.indexOf('=') + 1).replace(/"/g, '');
      var f = body.split('~');
      if (f.length <= F.pb) { continue; }
      var row = {};
      for (var k in F) { if (Object.prototype.hasOwnProperty.call(F, k)) { row[k] = f[F[k]]; } }
      if (!row.code) { continue; }
      for (var j = 0; j < NUM.length; j++) {
        var raw = String(row[NUM[j]] || '').trim();
        var v = raw ? Number(raw) : NaN;
        row[NUM[j]] = isFinite(v) ? v : null;
      }
      row.name = String(row.name || '').trim();
      row.code = String(row.code).trim();
      var t = String(row.quote_time || '');
      row.quote_ts = t.length >= 14 ? (t.slice(0, 4) + '-' + t.slice(4, 6) + '-' + t.slice(6, 8) + ' ' +
        t.slice(8, 10) + ':' + t.slice(10, 12) + ':' + t.slice(12, 14)) : '';
      out[row.code] = row;
    }
    return out;
  }

  function chunk(arr, n) {
    var out = [];
    for (var i = 0; i < arr.length; i += n) { out.push(arr.slice(i, i + n)); }
    return out;
  }

  function fetchQuotes(codes, cb) {
    var uniq = [], seen = {};
    for (var i = 0; i < codes.length; i++) {
      var c = String(codes[i] || '').trim();
      if (!/^\d{6}$/.test(c) || seen[c]) { continue; }
      seen[c] = 1; uniq.push(c);
    }
    if (!uniq.length) { cb({}, ''); return; }
    var batches = chunk(uniq, 60), got = {}, errs = [], left = batches.length;
    batches.forEach(function (b) {
      var url = 'https://qt.gtimg.cn/q=' + b.map(txSym).join(',');
      fetch(url, { cache: 'no-store' })
        .then(function (r) { return r.arrayBuffer(); })
        .then(function (buf) {
          var one = parseQuoteText(decodeGBK(buf));
          for (var k in one) { if (Object.prototype.hasOwnProperty.call(one, k)) { got[k] = one[k]; } }
        })
        .catch(function (e) { errs.push(String(e && e.message || e)); })
        .then(function () {
          left -= 1;
          if (left === 0) { cb(got, errs.join(' / ')); }
        });
    });
  }

  /* --------------------------------------------------------------- 费率 */
  // 逐行对齐 touzi/portfolio.py 的 FeeModel
  function commission(amount) {
    if (!(amount > 0)) { return 0; }
    var min = FEES.commission_min == null ? 5.0 : FEES.commission_min;
    var rate = FEES.commission_rate == null ? 0.00025 : FEES.commission_rate;
    return Math.max(amount * rate, min);
  }
  function buyFees(amount) {
    if (!(amount > 0)) { return 0; }
    return commission(amount) + amount * (FEES.transfer_fee_rate == null ? 0.00001 : FEES.transfer_fee_rate);
  }
  function sellFees(amount) {
    if (!(amount > 0)) { return 0; }
    return commission(amount)
      + amount * (FEES.stamp_duty_rate == null ? 0.0005 : FEES.stamp_duty_rate)
      + amount * (FEES.transfer_fee_rate == null ? 0.00001 : FEES.transfer_fee_rate);
  }
  function sellRatioFor(action) {
    var rr = PLAN.reduce_ratio == null ? 0.5 : Number(PLAN.reduce_ratio);
    if (!(rr >= 0)) { rr = 0.5; }
    rr = Math.min(1, Math.max(0, rr));
    if (action === '清仓') { return 1.0; }
    if (action === '减仓') { return rr; }
    return 0;
  }
  function planSellShares(shares, ratio, lot) {
    lot = lot || 100;
    var sh = Math.max(0, Math.floor(shares));
    if (sh <= 0 || ratio <= 0) { return 0; }
    if (ratio >= 1) { return sh; }
    var raw = Math.floor(sh * ratio);
    if (raw >= lot) { raw = Math.floor(raw / lot) * lot; }
    if (raw <= 0) { raw = Math.min(sh, lot); }
    return Math.max(1, Math.min(sh, raw));
  }

  function num(v) { return (typeof v === 'number' && isFinite(v)) ? v : NaN; }
  function r2(v) { return isFinite(v) ? Math.round(v * 100) / 100 : null; }

  function sourceTime(value) {
    var text = String(value || '');
    return Date.parse(text.includes('T') ? text : text.replace(' ', 'T') + '+08:00');
  }

  function publicScoreFresh(now) {
    if (SITE.trading_calendar !== undefined && !(typeof LIVE !== 'undefined' && LIVE.attempted)) {
      var built = sourceTime(SITE.built_at), quoteStamp = sourceTime(SITE.quote_at);
      var context = marketClock(now, SITE.trading_calendar);
      if (!context || !isFinite(built) || !isFinite(quoteStamp) || built > now + 60000
        || !(Number(SITE.quote_coverage) >= 0.9) || String(SITE.as_of) !== String(SITE.quote_at).slice(0, 10)) { return false; }
      var sourceISO = new Date(quoteStamp).toISOString();
      var expiry = calendarModelExpiry(built, sourceISO, SITE.trading_calendar);
      var ref = closedBaseline(built, sourceISO, now, SITE.trading_calendar) ? built : now;
      return expiry !== null && now < expiry && ref - built <= 36 * 3600000
        && ref - quoteStamp <= 36 * 3600000 && calendarQuoteFresh({price:1,quote_ts:sourceISO}, now, SITE.trading_calendar) !== null;
    }
    var age = Date.parse(String(SITE.as_of || '') + 'T00:00:00+08:00');
    var quoteAt = Date.parse(String(SITE.quote_at || '').replace(' ', 'T') + '+08:00');
    var coverage = Number(SITE.quote_coverage);
    if (!isFinite(age) || !isFinite(quoteAt) || now - age > 7 * 86400000
      || now - age < -86400000 || quoteAt > now + 5 * 60000
      || !isFinite(coverage) || coverage < 0.9) { return false; }
    var local = new Date(now + 8 * 3600000), quote = new Date(quoteAt + 8 * 3600000);
    var today = local.toISOString().slice(0, 10), day = quote.toISOString().slice(0, 10);
    var time = local.toISOString().slice(11, 16), quoteTime = quote.toISOString().slice(11, 16);
    if (String(SITE.as_of) !== day) { return false; }
    if (local.getUTCDay() >= 1 && local.getUTCDay() <= 5) {
      if (time >= '16:00' && (day !== today || quoteTime < '15:00')) { return false; }
      if (time >= '10:30' && time < '15:00'
        && (day !== today || quoteTime < '09:30')) { return false; }
    }
    return true;
  }

  // 估值必须来自同一只股票的可核验报价；交易中最多容忍十分钟延迟。
  // 非交易时段仅接受最近工作日收盘报价，节假日无法核验时保守标为未知。
  function valuationQuoteValid(quote, code, now) {
    if (SITE.trading_calendar !== undefined) {
      var quoteAt = quote && sourceTime(quote.quote_ts);
      return !!quote && quote.code === code && isFinite(quoteAt) && calendarQuoteFresh({price:quote.price,
        quote_ts:new Date(quoteAt).toISOString()}, now, SITE.trading_calendar) !== null;
    }
    if (!quote || quote.code !== code || !(num(quote.price) > 0)) { return false; }
    var text = String(quote.quote_ts || '');
    if (!/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(text)) { return false; }
    var at = Date.parse(text.replace(' ', 'T') + '+08:00');
    if (!isFinite(at) || at > now + 60000
      || new Date(at + 8 * 3600000).toISOString().slice(0, 19).replace('T', ' ') !== text) { return false; }
    var local = new Date(now + 8 * 3600000), day = local.getUTCDay();
    var today = local.toISOString().slice(0, 10), time = local.toISOString().slice(11, 19);
    var qt = text.slice(11), date = text.slice(0, 10);
    if (qt < '09:25:00' || qt > '15:05:00'
      || (qt > '11:35:00' && qt < '13:00:00')) { return false; }
    if (day >= 1 && day <= 5 && time >= '09:30:00' && time < '15:10:00') {
      if (date !== today) { return false; }
      if (time >= '11:30:00' && time < '13:00:00') { return qt >= '11:25:00'; }
      return now - at <= 10 * 60000;
    }
    if (day >= 1 && day <= 5 && time >= '15:10:00') {
      return date === today && qt >= '15:00:00';
    }
    do { local.setUTCDate(local.getUTCDate() - 1); }
    while (local.getUTCDay() === 0 || local.getUTCDay() === 6);
    return date === local.toISOString().slice(0, 10) && qt >= '15:00:00';
  }

  function sellDecision(h, e, quote) {
    var snap = e.sell_snapshot;
    var fresh = publicScoreFresh(Date.now());
    var exit = snap && fresh ? (snap.exit || []).slice() : [];
    var reduce = snap && fresh ? (snap.reduce || []).slice() : [];
    var warn = snap && fresh ? (snap.warn || []).slice() : [];
    if (snap && fresh && Array.isArray(snap.missing_warn)) {
      warn = warn.concat(snap.missing_warn.map(function (label) {
        return '预警数据待核验：' + label;
      }));
    }
    var missing = !snap ? ['本轮没有该股公共评分数据']
      : !fresh ? ['公开评分未通过最近一轮开盘/收盘行情核验']
        : Array.isArray(snap.missing) ? snap.missing.slice() : ['公开评分未提供数据覆盖清单'];
    var code = String(h['代码'] || ''), history = (SITE.thr || {})[code];
    var now = Date.now(), validQuote = valuationQuoteValid(quote, code, now);
    ['pe', 'pb'].forEach(function (metric) {
      var key = 'sell_' + metric + '_percentile';
      var label = snap && snap.percentile_labels && snap.percentile_labels[key];
      label = label || metric.toUpperCase() + ' 回到自身历史分位 ≥ '
        + (Number(PLAN[key]) * 100).toFixed(0) + '%';
      var item = history && history[metric];
      var age = history && Date.parse(history.as_of + 'T00:00:00+08:00');
      var value = num(quote && quote[metric === 'pe' ? 'pe_ttm' : 'pb']);
      var known = validQuote && history && history.source === '个股5年历史'
        && isFinite(age) && now - age >= 0 && now - age <= 7 * 86400000
        && item && item.known === true && num(item.samples) >= 5
        && num(item.boundary) >= 0 && num(item.percentile) === num(PLAN[key])
        && value > 0;
      if (known) {
        // 只替换这条分位规则；其他公共快照条件继续生效。
        exit = exit.filter(function (x) { return x !== label; });
        missing = missing.filter(function (x) { return x !== label; });
        if (value > item.boundary) { exit.push(label + '（实时报价）'); }
      } else {
        missing.push(metric.toUpperCase() + ' 历史分位实时判断无法完成：'
          + (!validQuote ? '报价缺失、异常或过期'
            : !(value > 0) ? '估值缺失或非正值' : '有效个股五年历史阈值缺失或过期')
          + '；公共快照仍按发布时间判断');
      }
    });
    var price = num(quote && quote.price);
    var buy = Number(h['买入价']);
    var hasPrice = isFinite(price) && price > 0;
    var hasCost = isFinite(buy) && buy > 0;
    if (hasPrice && hasCost) {
      var pnl = price / buy - 1;
      var stop = Number(PLAN.sell_stop_loss);
      var take = Number(PLAN.sell_take_profit);
      if (isFinite(stop) && pnl <= stop) {
        exit.push('相对买入价浮亏 ≤ ' + (stop * 100).toFixed(0) + '%（当前 '
          + (pnl * 100).toFixed(2) + '%）');
      }
      if (isFinite(take) && pnl >= take) {
        reduce.push('相对买入价浮盈 ≥ +' + (take * 100).toFixed(0) + '%（当前 +'
          + (pnl * 100).toFixed(2) + '%）');
      }
    }
    if (exit.length) { return { action: '清仓', triggers: exit.join('；'), warn: warn.concat(missing).join('；') }; }
    if (reduce.length) { return { action: '减仓', triggers: reduce.join('；'), warn: warn.concat(missing).join('；') }; }
    if (!hasPrice || !hasCost) {
      missing.push(!hasPrice ? '没有现价，无法判断个人止损/止盈' : '缺少买入价，无法判断个人止损/止盈');
    }
    if (missing.length) {
      return { action: '无法评估', triggers: '', warn: warn.concat(missing).join('；') };
    }
    return { action: '一切照旧', triggers: '', warn: warn.join('；') };
  }

  /* --------------------------------------------------- 单笔：与 _one() 对齐 */
  function oneRow(h, quote) {
    var shares = Number(h['股数']) || 0;
    var buy = Number(h['买入价']) || 0;
    var costAmount = buy * shares;
    var bf = buyFees(costAmount);
    var costWithFees = costAmount + bf;
    var e = ENTRIES[String(h['代码'])] || {};
    var price = num(quote && quote.price);
    var hasPrice = isFinite(price) && price > 0;
    var hasShares = shares > 0;

    var mv = (hasPrice && hasShares) ? price * shares : NaN;
    var unreal = isFinite(mv) ? mv - costAmount : NaN;
    var unrealPct = (isFinite(unreal) && costAmount > 0) ? unreal / costAmount * 100 : NaN;

    var decision = sellDecision(h, e, quote);
    var action = decision.action;
    var ratio = sellRatioFor(action);
    var sellSh = planSellShares(shares, ratio);
    var sellAmount = NaN, sellFee = NaN, netIn = NaN, netProfit = NaN, netPct = NaN;
    if (hasPrice && sellSh > 0) {
      sellAmount = price * sellSh;
      sellFee = sellFees(sellAmount);
      netIn = sellAmount - sellFee;
      var partCost = shares > 0 ? costWithFees * (sellSh / shares) : NaN;
      netProfit = isFinite(partCost) ? netIn - partCost : NaN;
      if (isFinite(netProfit) && shares > 0 && sellSh > 0 && costWithFees > 0) {
        netPct = netProfit / (costWithFees * (sellSh / shares)) * 100;
      }
    }

    var al = alertFor(action, ratio, sellSh, netProfit, netPct,
      decision.triggers, decision.warn, hasPrice, hasShares);

    return {
      code: String(h['代码']),
      name: String(h['名称'] || e.name || ''),
      buyDate: h['买入日期'] || '',
      buyPrice: isFinite(buy) ? buy : null,
      shares: hasShares ? shares : null,
      costAmount: hasShares ? costAmount : null,
      buyFee: hasShares ? bf : null,
      costWithFees: hasShares ? costWithFees : null,
      price: hasPrice ? price : null,
      mv: isFinite(mv) ? mv : null,
      pnl: isFinite(unreal) ? unreal : null,
      pnl_pct: isFinite(unrealPct) ? unrealPct : null,
      action: action || '无法评估',
      level: al[1],
      alert: al[0],
      sellPct: ratio > 0 ? Math.round(ratio * 1000) / 10 : 0,
      sellShares: sellSh,
      sellAmount: isFinite(sellAmount) ? sellAmount : null,
      sellFee: isFinite(sellFee) ? sellFee : null,
      netIn: isFinite(netIn) ? netIn : null,
      net_profit: isFinite(netProfit) ? netProfit : null,
      net_profit_pct: isFinite(netPct) ? netPct : null
    };
  }

  // 逐行对齐 touzi/portfolio.py 的 Portfolio._alert
  function alertFor(action, ratio, sellSh, netProfit, netPct, triggers, warn, hasPrice, hasShares) {
    var money = '';
    if (isFinite(netProfit)) {
      money = '。这部分扣费后净盈利 ¥' + fmtMoney(netProfit) + '（' + (netPct >= 0 ? '+' : '') + netPct.toFixed(2) + '%）';
    }
    if (action === '清仓') {
      return ['🚨 **清仓条件已触发**：' + (hasShares ? ('卖出全部 ' + sellSh + ' 股（100%）。') : '请补填股数。')
        + '触发：' + triggers + (hasPrice ? money : '。现价不可用，无法计算成交金额'), 'act'];
    }
    if (action === '减仓') {
      return ['⚠️ **减仓条件已触发**：' + (hasShares ? ('建议卖出 ' + sellSh + ' 股（' + Math.round(ratio * 100) + '%）。') : '请补填股数。')
        + '触发：' + triggers + (hasPrice ? money : '。现价不可用，无法计算成交金额'), 'act'];
    }
    if (action === '无法评估') { return ['无法评估：' + (warn || '本轮没有完整卖出判断所需的数据'), 'na']; }
    if (!hasShares) { return ['没填股数，无法计算卖出金额；请在台账里补上股数。', 'watch']; }
    if (action === '一切照旧') {
      return [warn ? ('一切照旧；但需留意：' + warn) : '一切照旧：当前已核验的卖出条件均未触发', warn ? 'watch' : 'hold'];
    }
    if (action === '加仓') { return ['买入门槛全部通过、无卖出条件触发，且未到止损止盈——可考虑加仓', 'watch']; }
    if (warn) { return ['继续持有。留意：' + warn, 'watch']; }
    if (action === '持有') { return ['继续持有，卖出条件均未触发', 'hold']; }
    return ['继续持有（规则给的动作是「' + action + '」）', 'hold'];
  }

  // 逐行对齐 _summarize：只对**有现价**的行求和
  function summarize(rows) {
    function sum(key) {
      var t = 0, n = 0;
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].mv === null) { continue; }
        var v = rows[i][key];
        if (typeof v === 'number' && isFinite(v)) { t += v; n += 1; }
      }
      return [t, n];
    }
    var c = sum('costWithFees'), m = sum('mv');
    var unreal = m[0] - c[0];
    var act = 0, watch = 0, na = 0;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].level === 'act') { act += 1; }
      else if (rows[i].level === 'watch') { watch += 1; }
      else if (rows[i].level === 'na') { na += 1; }
    }
    return {
      '持仓只数': rows.length,
      '有现价只数': m[1],
      '无现价只数': rows.length - m[1],
      '总成本(含费)': r2(c[0]),
      '总市值': r2(m[0]),
      '总浮动盈亏': r2(unreal),
      '总浮动盈亏%': c[0] > 0 ? r2(unreal / c[0] * 100) : null,
      '需要动手': act, '留意': watch, '无法评估': na
    };
  }

  /* ------------------------------------------------------- 台账（本地） */
  var LEDGER = [];

  function loadLedger() {
    try {
      var raw = localStorage.getItem(LEDGER_KEY);
      if (raw) { LEDGER = JSON.parse(raw) || []; return; }
    } catch (e) { }
    LEDGER = (SITE.seed_holdings || []).slice();
    if (LEDGER.length) { saveLedger().catch(function () { window.__toast('初始持仓未能保存到本地，请导出 CSV 备份。'); }); }
  }
  var HOLD_BUSY = false, HOLD_DIALOG = null;
  function saveLedger(rows) {
    rows = rows || LEDGER;
    try { localStorage.setItem(LEDGER_KEY, JSON.stringify(rows)); return Promise.resolve(); }
    catch (error) { return Promise.reject(error); }
  }
  function requireAccount() { return true; }
  // 候选台账只有持久化成功才替换当前可见数据，所有入口共享提交锁。
  function commitLedger(rows, success, message) {
    if (HOLD_BUSY) { window.__toast('正在保存，请稍后'); return Promise.resolve(false); }
    HOLD_BUSY = true;
    if (message) { message.textContent = '正在保存…'; }
    if (HOLD_DIALOG) { HOLD_DIALOG.querySelectorAll('button, input').forEach(function (e) { e.disabled = true; }); }
    return saveLedger(rows).then(function () {
      LEDGER = rows; HOLD_BUSY = false;
      // 持久化已成功，后续渲染故障不能伪装成写入失败或诱导重复提交。
      try { window.__renderAll(); success(); }
      catch {
        closeHoldingDialog();
        window.__toast('持仓已保存，但页面更新失败，请刷新查看。');
      }
      return true;
    }, function (error) {
      HOLD_BUSY = false;
      var text = '保存失败：' + String(error.message || error) + '。原持仓未变，请重试。';
      if (message) { message.textContent = text; }
      window.__toast(text); return false;
    }).then(function (ok) {
      if (HOLD_DIALOG) { HOLD_DIALOG.querySelectorAll('button, input').forEach(function (e) { e.disabled = false; }); }
      return ok;
    });
  }
  function cleanCode(v) {
    var s = String(v == null ? '' : v).trim().toUpperCase();
    s = s.replace(/^(SH|SZ|BJ)/, '').replace(/\.(SH|SZ|BJ)$/, '');
    return s;
  }
  // 与 serve.py 的 upsert 一致的覆盖键：代码 + 买入日期 + 买入价
  function upsert(row, rows) {
    for (var i = 0; i < rows.length; i++) {
      if (String(rows[i]['代码']) === String(row['代码'])
        && String(rows[i]['买入日期'] || '') === String(row['买入日期'] || '')
        && String(rows[i]['买入价'] || '') === String(row['买入价'] || '')) {
        rows[i] = row; return 'upsert';
      }
    }
    rows.push(row); return 'add';
  }

  /* ------------------------------------------------ 汇总成服务器版载荷形状 */
  var QUOTES = {};

  function wantedCodes() {
    var codes = [], seen = {};
    function push(c) {
      c = String(c || '').trim();
      if (/^\d{6}$/.test(c) && !seen[c]) { seen[c] = 1; codes.push(c); }
    }
    for (var i = 0; i < LEDGER.length; i++) { push(LEDGER[i]['代码']); }
    if (HAS_DOM) {
      var cells = document.querySelectorAll('[data-q]');
      for (var j = 0; j < cells.length; j++) {
        push(String(cells[j].getAttribute('data-q')).split('.')[0]);
      }
    }
    return codes;
  }

  function buildPayload(err) {
    var rows = [], holdings = {};
    for (var i = 0; i < LEDGER.length; i++) {
      var r = oneRow(LEDGER[i], QUOTES[String(LEDGER[i]['代码'])]);
      rows.push(r);
      holdings[r.code] = {
        price: r.price, mv: r.mv, pnl: r.pnl, pnl_pct: r.pnl_pct,
        net_profit: r.net_profit, net_profit_pct: r.net_profit_pct
      };
    }
    var s = summarize(rows);
    var codes = Object.keys(QUOTES);
    var qt = '';
    for (var k = 0; k < codes.length; k++) {
      var t = QUOTES[codes[k]].quote_ts || '';
      if (t > qt) { qt = t; }
    }
    return {
      rows: rows,
      quotes: QUOTES,
      holdings: holdings,
      summary: {},
      meta: {
        count: codes.length,
        quote_time: qt || '',
        error: err || null,
        source: 'tencent'
      },
      summaryRows: s
    };
  }

  // 服务器版 /api/quotes 的 summary 只在**有持仓**时给合计（空台账就是 {}），
  // 而且键名是**英文**（mv / pnl / pnl_pct / cost）——页面上写的是
  // data-q="summary.mv"。_summarize 出来的那份是中文键，必须在这里换名，
  // 否则「合计」那一行和 KPI 卡片会永远停在初值上（页面不会报任何错）。
  function withSummary(p) {
    if (p.meta.count && LEDGER.length) {
      var s = p.summaryRows || {};
      p.summary = {
        mv: s['总市值'], pnl: s['总浮动盈亏'], pnl_pct: s['总浮动盈亏%'],
        cost: s['总成本(含费)']
      };
    }
    return p;
  }

  /* ------------------------------------------------------ 数字格式化 */
  function fmtMoney(v) {
    var neg = v < 0, a = Math.abs(v);
    var s = a.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return (neg ? '-' : '') + s;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function mdInline(s) {
    var parts = String(s == null ? '' : s).split('**'), out = '';
    for (var i = 0; i < parts.length; i++) { out += (i % 2) ? '<b>' + esc(parts[i]) + '</b>' : esc(parts[i]); }
    return out;
  }
  function dash(v, nd) {
    if (v === null || v === undefined || (typeof v === 'number' && !isFinite(v))) { return '—'; }
    return fmtMoney(Number(v));
  }

  /* --------------------------------------------------------- apply() */
  // 与 scripts/webui.py 的 live_js() 同一套约定（data-q / data-nd / data-suffix / data-kind）
  function fmt(v, nd, suffix) {
    if (v === null || v === undefined || (typeof v === 'number' && isNaN(v))) { return null; }
    var s = Number(v).toLocaleString('zh-CN', { minimumFractionDigits: nd, maximumFractionDigits: nd });
    return s + (suffix || '');
  }
  function flash(el, up) {
    el.classList.remove('flash-up', 'flash-dn');
    void el.offsetWidth;
    el.classList.add(up ? 'flash-up' : 'flash-dn');
    setTimeout(function () { el.classList.remove('flash-up', 'flash-dn'); }, 1200);
  }
  function lookup(d, code, field) {
    var bag = (code === 'summary') ? d.summary
      : (code === 'meta') ? d.meta
        : (d.quotes && d.quotes[code] && (field in d.quotes[code])) ? d.quotes[code]
          : (d.holdings && d.holdings[code]) ? d.holdings[code] : null;
    if (!bag || !(field in bag)) { return undefined; }
    return bag[field];
  }
  function apply(d) {
    var cells = document.querySelectorAll('[data-q]');
    for (var i = 0; i < cells.length; i++) {
      var el = cells[i], spec = el.getAttribute('data-q').split('.');
      var v = lookup(d, spec[0], spec[1]);
      if (v === undefined) { continue; }
      if (el.getAttribute('data-kind') === 'text') {
        var t = (v === null || v === undefined) ? '—' : String(v);
        if (el.textContent !== t) { el.textContent = t; flash(el, true); }
        continue;
      }
      var nd = parseInt(el.getAttribute('data-nd') || '2', 10);
      var txt = fmt(v, nd, el.getAttribute('data-suffix') || '');
      if (txt === null) { continue; }
      var old = parseFloat((el.textContent || '').replace(/[,%]/g, ''));
      if (el.textContent !== txt) { el.textContent = txt; if (!isNaN(old)) { flash(el, Number(v) >= old); } }
    }
    var ts = document.getElementById('__livets');
    if (ts && d.meta && d.meta.quote_time) { ts.textContent = d.meta.quote_time; }
    var st = document.getElementById('__livestate');
    var dot = document.getElementById('__dot');
    if (st && d.meta) {
      if (d.meta.error) { st.textContent = '报价失败'; }
      else if (!d.meta.count) { st.textContent = '无实时标的'; }
      else { st.textContent = '实时 ' + d.meta.count + ' 只'; }
      if (dot) { dot.className = 'dot ' + (d.meta.error ? 'bad' : (d.meta.count ? 'ok' : 'wait')); }
    }
  }

  window.__toast = function (msg) {
    var t = document.getElementById('__toast');
    if (!t) { return; }
    t.textContent = msg; t.style.display = 'block';
    clearTimeout(t.__t); t.__t = setTimeout(function () { t.style.display = 'none'; }, 4000);
  };

  /* ------------------------------------------- 我的持仓：台账 + 实时盈亏 */
  var TONE = { '清仓': 'bad', '减仓': 'weak', '持有': 'good', '加仓': 'great',
    '一切照旧': 'good', '无法评估': 'mid' };
  var LEDGER_COLS = ['代码', '名称', '买入日期', '买入价', '股数', '备注'];

  function renderLedger() {
    var host = document.getElementById('__ledger');
    if (!host) { return; }
    if (!LEDGER.length) {
      host.innerHTML = '<tr><td colspan="8" class="muted">还没有任何持仓。'
        + '可以在上面的表单里录入，或去「选股 · 勾选建仓」打勾加入。</td></tr>';
      var n0 = document.getElementById('__ledgernote');
      if (n0) { n0.textContent = '共 0 行。这台浏览器还没有存过持仓。'; }
      return;
    }
    var h = ['<tr><th>#</th><th>代码</th>'];
    for (var c = 1; c < LEDGER_COLS.length; c++) { h.push('<th>' + esc(LEDGER_COLS[c]) + '</th>'); }
    h.push('<th>操作</th></tr>');
    var nOk = 0;
    for (var i = 0; i < LEDGER.length; i++) {
      var r = LEDGER[i], code = String(r['代码'] || '');
      var ok = /^\d{6}$/.test(code);
      if (ok) { nOk += 1; }
      h.push('<tr data-idx="' + i + '"><td>' + i + '</td><td><code>' + esc(code) + '</code>'
        + (ok ? '' : ' <span class="pill bad">代码非法，会被跳过</span>') + '</td>');
      for (var c2 = 1; c2 < LEDGER_COLS.length; c2++) { h.push('<td>' + esc(r[LEDGER_COLS[c2]] || '') + '</td>'); }
      h.push('<td><button class="b b2" type="button" onclick="__holdEdit(' + i + ')">编辑</button> '
        + '<button class="b b2" type="button" onclick="__holdDel(' + i + ')">删除</button>'
        + '</td></tr>');
    }
    host.innerHTML = h.join('');
    var n = document.getElementById('__ledgernote');
    if (n) {
      n.textContent = '共 ' + LEDGER.length + ' 行，其中 ' + nOk + ' 条会被跟踪。'
        + (nOk < LEDGER.length ? '代码不是 6 位数字的行会被跳过。' : '')
        + '台账只存在你这台浏览器的本地存储里，不会上传到任何地方。';
    }
  }

  function renderLive() {
    var host = document.getElementById('__live');
    if (!host) { return; }
    var p = withSummary(buildPayload());
    if (!LEDGER.length) {
      host.innerHTML = '<p class="muted small">台账为空，所以没什么可跟踪的。先在上面录入，'
        + '或去「选股 · 勾选建仓」打勾加入。</p>';
      return;
    }
    var h = ['<tr><th>代码</th><th>名称</th><th class="num">买入价</th><th class="num">股数</th>'
      + '<th class="num">含费成本</th><th class="num">现价</th><th class="num">市值</th>'
      + '<th class="num">浮动盈亏</th><th class="num">浮动盈亏%</th><th>建议动作</th>'
      + '<th class="num">建议卖出%</th><th class="num">建议卖出股数</th>'
      + '<th class="num">到账金额</th><th class="num">扣费后净盈利</th><th>提醒</th></tr>'];
    for (var i = 0; i < p.rows.length; i++) {
      var r = p.rows[i];
      var pcls = (r.pnl !== null && r.pnl >= 0) ? 'v-good' : 'v-bad';
      var ncls = (r.net_profit !== null && r.net_profit >= 0) ? 'v-good' : 'v-bad';
      h.push('<tr><td><code>' + esc(r.code) + '</code></td><td>' + esc(r.name) + '</td>'
        + '<td class="num">' + dash(r.buyPrice, 3) + '</td>'
        + '<td class="num">' + (r.shares === null ? '未填' : r.shares) + '</td>'
        + '<td class="num">' + dash(r.costWithFees, 2) + '</td>'
        + '<td class="num" data-q="' + esc(r.code) + '.price" data-nd="3">' + dash(r.price, 3) + '</td>'
        + '<td class="num" data-q="' + esc(r.code) + '.mv" data-nd="2">' + dash(r.mv, 2) + '</td>'
        + '<td class="num ' + pcls + '" data-q="' + esc(r.code) + '.pnl" data-nd="2">' + dash(r.pnl, 2) + '</td>'
        + '<td class="num ' + pcls + '" data-q="' + esc(r.code) + '.pnl_pct" data-nd="2" data-suffix="%">' + dash(r.pnl_pct, 2) + '</td>'
        + '<td><span class="pill ' + (TONE[r.action] || 'mid') + '">' + esc(r.action) + '</span></td>'
        + '<td class="num">' + dash(r.sellPct, 1) + '%</td>'
        + '<td class="num">' + r.sellShares + '</td>'
        + '<td class="num">' + dash(r.netIn, 2) + '</td>'
        + '<td class="num ' + ncls + '" data-q="' + esc(r.code) + '.net_profit" data-nd="2">' + dash(r.net_profit, 2) + '</td>'
        + '<td class="small">' + mdInline(r.alert) + '</td></tr>');
    }
    var s = p.summaryRows;
    h.push('<tfoot><tr><th colspan="6">合计（' + (s['有现价只数'] || 0) + ' 只有现价'
      + (s['无现价只数'] ? '，另有 ' + s['无现价只数'] + ' 只没现价已排除在合计外' : '') + '）</th>'
      + '<th class="num" data-q="summary.mv" data-nd="2">' + dash(s['总市值'], 2) + '</th>'
      + '<th class="num" data-q="summary.pnl" data-nd="2">' + dash(s['总浮动盈亏'], 2) + '</th>'
      + '<th class="num" data-q="summary.pnl_pct" data-nd="2" data-suffix="%">'
      + (s['总浮动盈亏%'] === null ? '—' : fmtMoney(s['总浮动盈亏%'])) + '</th>'
      + '<th colspan="6"></th></tr></tfoot>');
    host.innerHTML = h.join('');
  }

  // 台账变化（录入/删除/导入/打勾加入）→ 重画表格骨架，再让 apply() 填数字。
  // 顺手立刻催一次行情：不然刚录入完最多要等 15 秒才看到价格。
  window.__renderAll = function () {
    var p = withSummary(buildPayload());
    LAST_SIG = structSig(p);
    renderLedger(); renderLive(); renderKpi();
    apply(p);
    scheduleComparison();
    tickSoon();
  };

  /* --------------------------------------------------- KPI 卡片（/ 与 /my） */
  function kpiCard(label, value, note, qkey, cls, nd, suffix) {
    var attr = qkey ? (' data-q="' + qkey + '" data-nd="' + (nd == null ? 2 : nd) + '"'
      + (suffix ? ' data-suffix="' + suffix + '"' : '')) : '';
    return '<div class="card"><div class="kv"><b>' + esc(label) + '</b>'
      + '<span class="muted small">' + esc(note) + '</span></div>'
      + '<div class="big' + (cls || '') + '"' + attr + '>' + esc(value) + '</div></div>';
  }

  function renderKpi() {
    var host = document.getElementById('__kpi');
    if (!host) { return; }
    var p = withSummary(buildPayload());
    var s = p.summaryRows;
    var live = LEDGER.length > 0;
    var nPx = s['有现价只数'] || 0;
    var wait = nPx ? null : '待报价';
    function money(key, nd) {
      if (!live) { return '—'; }
      if (!nPx) { return wait; }
      return dash(s[key], nd);
    }
    var pnl = (live && nPx) ? s['总浮动盈亏'] : null;
    var pcls = (pnl !== null && pnl >= 0) ? ' v-good' : ' v-bad';
    var cards = [
      kpiCard('持仓只数', live ? String(s['持仓只数']) : '0', '', '', '', 0),
      kpiCard('总成本（含费）', money('总成本(含费)'), '', '', '', 2),
      kpiCard('总市值', money('总市值'), (live && nPx) ? '实时' : '', live && nPx ? 'summary.mv' : '', '', 2),
      kpiCard('浮动盈亏', money('总浮动盈亏'), '', live && nPx ? 'summary.pnl' : '', pcls, 2),
      kpiCard('浮动盈亏%', (live && nPx && s['总浮动盈亏%'] !== null) ? fmtMoney(s['总浮动盈亏%']) : '—', '',
        live && nPx ? 'summary.pnl_pct' : '', '', 2, '%'),
      kpiCard('需要动手', live ? String(s['需要动手'] || 0) : '—',
        live ? ('留意 ' + (s['留意'] || 0) + ' 只') : '', '', '', 0)
    ];
    host.innerHTML = cards.join('');
  }

  /* ----------------------------------------- 持仓 vs 四等分 ETF 收益对比 */
  var HISTORY = {};
  var COMPARE_SIG = '';
  var COMPARE_BUSY = false;

  function historyUrl(code) {
    return 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param='
      + encodeURIComponent(txSym(code) + ',week,,,1000,qfq');
  }

  function parseHistory(payload, code) {
    var bag = payload && payload.data && payload.data[txSym(code)];
    if (!bag) { return []; }
    var raw = bag.qfqweek || bag.week || bag.qfqday || bag.day || [];
    var out = [];
    for (var i = 0; i < raw.length; i++) {
      var px = Number(raw[i] && raw[i][2]);
      var dt = String(raw[i] && raw[i][0] || '').slice(0, 10);
      if (/^\d{4}-\d{2}-\d{2}$/.test(dt) && isFinite(px) && px > 0) { out.push([dt, px]); }
    }
    return out;
  }

  function fetchHistory(code) {
    if (HISTORY[code]) { return HISTORY[code]; }
    HISTORY[code] = fetch(historyUrl(code), { cache: 'no-store' })
      .then(function (r) { if (!r.ok) { throw new Error('HTTP ' + r.status); } return r.json(); })
      .then(function (o) { return parseHistory(o, code); })
      .catch(function () { return []; });
    return HISTORY[code];
  }

  function pointOnOrAfter(prices, date) {
    for (var i = 0; i < prices.length; i++) { if (prices[i][0] >= date) { return prices[i]; } }
    return null;
  }

  function etfBuy(state, price, lotSize) {
    if (!(price > 0) || !(state.cash > 0)) { return; }
    var shares = Math.floor(state.cash / price / lotSize) * lotSize;
    while (shares > 0 && shares * price + commission(shares * price) > state.cash + 0.00001) {
      shares -= lotSize;
    }
    if (shares <= 0) { return; }
    var amount = shares * price;
    state.cash -= amount + commission(amount);
    state.shares += shares;
  }

  function comparisonData(ledger, histories, benchmark) {
    var ins = (benchmark && benchmark.instruments) || [];
    if (ins.length !== 4) { return { error: '四只 ETF 的历史行情不完整，请重新发布网站后再试。' }; }
    var lots = [], total = 0;
    for (var i = 0; i < ledger.length; i++) {
      var h = ledger[i], buy = Number(h['买入价']), shares = Number(h['股数']);
      var date = String(h['买入日期'] || '').slice(0, 10), code = String(h['代码'] || '');
      if (!(buy > 0) || !(shares > 0) || !/^\d{4}-\d{2}-\d{2}$/.test(date)) { continue; }
      var cost = buy * shares + buyFees(buy * shares);
      lots.push({ code: code, date: date, buy: buy, shares: shares, cost: cost });
      total += cost;
    }
    if (!lots.length) { return { error: '至少需要一笔填了买入日期、买入价和股数的持仓。' }; }
    lots.sort(function (a, b) { return a.date < b.date ? -1 : (a.date > b.date ? 1 : 0); });

    var dateSet = {};
    for (var j = 0; j < ins.length; j++) {
      var bp = ins[j].prices || [];
      for (var k = 0; k < bp.length; k++) { if (bp[k][0] >= lots[0].date) { dateSet[bp[k][0]] = 1; } }
    }
    var dates = Object.keys(dateSet).sort();
    if (!dates.length) { return { error: '你的买入日期晚于基准行情日期，请更新网站数据。' }; }

    var personalLots = [], missed = 0;
    for (var l = 0; l < lots.length; l++) {
      var hp = histories[lots[l].code] || [];
      var anchor = pointOnOrAfter(hp, lots[l].date);
      if (!anchor) { missed += 1; continue; }
      personalLots.push({ lot: lots[l], prices: hp, anchorDate: anchor[0],
        anchor: anchor[1], cursor: -1, price: NaN });
    }

    var states = [], cursors = [];
    for (var s = 0; s < 4; s++) { states.push({ cash: 0, shares: 0, price: NaN }); cursors.push(-1); }
    var nextFlow = 0, contributed = 0, lotSize = Number(benchmark.lot_size) || 100;
    var series = [{ label: '我的持仓', color: '#e5e7eb', points: [] }];
    for (var z = 0; z < ins.length; z++) {
      series.push({ label: ins[z].label, color: ins[z].color, points: [] });
    }
    series.push({ label: '四等分组合', color: '#a78bfa', points: [] });

    for (var d = 0; d < dates.length; d++) {
      var day = dates[d];
      while (nextFlow < lots.length && lots[nextFlow].date <= day) {
        contributed += lots[nextFlow].cost;
        for (var a = 0; a < 4; a++) { states[a].cash += lots[nextFlow].cost / 4; }
        nextFlow += 1;
      }
      for (var x = 0; x < 4; x++) {
        var xp = ins[x].prices || [];
        while (cursors[x] + 1 < xp.length && xp[cursors[x] + 1][0] <= day) {
          cursors[x] += 1; states[x].price = Number(xp[cursors[x]][1]);
        }
        etfBuy(states[x], states[x].price, lotSize);
      }
      if (!(contributed > 0)) { continue; }

      var actualValue = 0, actualInvested = 0;
      for (var p = 0; p < personalLots.length; p++) {
        var pl = personalLots[p], pp = pl.prices;
        while (pl.cursor + 1 < pp.length && pp[pl.cursor + 1][0] <= day) {
          pl.cursor += 1; pl.price = Number(pp[pl.cursor][1]);
        }
        if (pl.anchorDate <= day && isFinite(pl.price) && pl.price > 0) {
          var gross = pl.lot.buy * pl.lot.shares * (pl.price / pl.anchor);
          actualValue += gross - sellFees(gross);
          actualInvested += pl.lot.cost;
        }
      }
      if (actualInvested > 0) {
        series[0].points.push([day, (actualValue / actualInvested - 1) * 100]);
      }

      var combined = 0;
      for (var q = 0; q < 4; q++) {
        var market = (states[q].shares > 0 && isFinite(states[q].price))
          ? states[q].shares * states[q].price : 0;
        var value = states[q].cash + market - (market > 0 ? commission(market) : 0);
        combined += value;
        series[q + 1].points.push([day, (value / (contributed / 4) - 1) * 100]);
      }
      series[5].points.push([day, (combined / contributed - 1) * 100]);
    }

    var summaries = [];
    for (var si = 0; si < series.length; si++) {
      var pts = series[si].points, last = pts.length ? pts[pts.length - 1][1] : null;
      summaries.push({ label: series[si].label, color: series[si].color, returnPct: last });
    }
    return { series: series, summaries: summaries, invested: total, missed: missed,
      start: dates[0], end: dates[dates.length - 1] };
  }

  function downsample(points, max) {
    if (points.length <= max) { return points; }
    var out = [], step = (points.length - 1) / (max - 1);
    for (var i = 0; i < max; i++) { out.push(points[Math.round(i * step)]); }
    return out;
  }

  function renderComparison(result) {
    var host = document.getElementById('__compare');
    if (!host) { return; }
    if (result.error) { host.innerHTML = '<p class="muted small">' + esc(result.error) + '</p>'; return; }
    var all = [], minT = Infinity, maxT = -Infinity, minY = 0, maxY = 0;
    for (var i = 0; i < result.series.length; i++) {
      var pts = downsample(result.series[i].points, 240), arr = [];
      for (var j = 0; j < pts.length; j++) {
        var t = Date.parse(pts[j][0] + 'T00:00:00Z'), y = Number(pts[j][1]);
        if (!isFinite(t) || !isFinite(y)) { continue; }
        arr.push([t, y]); minT = Math.min(minT, t); maxT = Math.max(maxT, t);
        minY = Math.min(minY, y); maxY = Math.max(maxY, y);
      }
      all.push(arr);
    }
    if (!(maxT > minT)) { host.innerHTML = '<p class="muted small">可用历史点不足，暂时无法画图。</p>'; return; }
    if (maxY - minY < 1) { maxY += 0.5; minY -= 0.5; }
    var w = 920, h = 390, left = 66, right = 18, top = 20, bottom = 44;
    function xx(t) { return left + (t - minT) / (maxT - minT) * (w - left - right); }
    function yy(v) { return top + (maxY - v) / (maxY - minY) * (h - top - bottom); }
    var svg = ['<svg class="compare-chart" viewBox="0 0 ' + w + ' ' + h + '" role="img" aria-label="收益率折线图">'];
    for (var gy = 0; gy <= 4; gy++) {
      var val = maxY - (maxY - minY) * gy / 4, py = yy(val);
      svg.push('<line x1="' + left + '" y1="' + py.toFixed(1) + '" x2="' + (w - right) + '" y2="' + py.toFixed(1) + '" stroke="#263244" stroke-width="1"/>');
      svg.push('<text x="' + (left - 8) + '" y="' + (py + 4).toFixed(1) + '" text-anchor="end" fill="#9ca3af" font-size="11">' + val.toFixed(1) + '%</text>');
    }
    for (var si = 0; si < all.length; si++) {
      var coords = all[si].map(function (p) { return xx(p[0]).toFixed(1) + ',' + yy(p[1]).toFixed(1); }).join(' ');
      svg.push('<polyline points="' + coords + '" fill="none" stroke="' + result.series[si].color
        + '" stroke-width="' + (si === 0 || si === 5 ? 2.8 : 1.7) + '" opacity="' + (si === 0 || si === 5 ? 1 : 0.78) + '"/>');
    }
    var zeroY = yy(0);
    if (zeroY >= top && zeroY <= h - bottom) {
      svg.push('<line x1="' + left + '" y1="' + zeroY.toFixed(1) + '" x2="' + (w - right) + '" y2="' + zeroY.toFixed(1) + '" stroke="#6b7280" stroke-dasharray="5 4"/>');
    }
    svg.push('<text x="' + left + '" y="' + (h - 12) + '" fill="#9ca3af" font-size="11">' + esc(result.start) + '</text>');
    svg.push('<text x="' + (w - right) + '" y="' + (h - 12) + '" text-anchor="end" fill="#9ca3af" font-size="11">' + esc(result.end) + '</text></svg>');
    var legend = ['<div class="compare-legend">'];
    for (var lg = 0; lg < result.series.length; lg++) {
      legend.push('<span><i style="background:' + result.series[lg].color + '"></i>' + esc(result.series[lg].label) + '</span>');
    }
    legend.push('</div>');
    var rows = ['<div class="scroll compare-summary"><table><tr><th>方案</th><th class="num">期末收益率</th></tr>'];
    for (var sm = 0; sm < result.summaries.length; sm++) {
      var rv = result.summaries[sm].returnPct;
      rows.push('<tr><td><span style="color:' + result.summaries[sm].color + '">●</span> '
        + esc(result.summaries[sm].label) + '</td><td class="num ' + (rv >= 0 ? 'v-good' : 'v-bad') + '">'
        + (rv === null ? '—' : ((rv >= 0 ? '+' : '') + rv.toFixed(2) + '%')) + '</td></tr>');
    }
    rows.push('</table></div>');
    var note = '<p class="compare-note">投入金额按你的台账合计：<b>¥' + fmtMoney(result.invested) + '</b>。'
      + '每笔现金流在对应日期同步四等分；ETF 按 100 份整手买入，零头保留现金。'
      + esc(BENCHMARKS.fee_note || '')
      + '“我的持仓”使用腾讯前复权周线按买入日锚定，并按当前股票费率模拟期末卖出。'
      + (result.missed ? ('有 ' + result.missed + ' 笔缺少历史行情，未纳入“我的持仓”曲线。') : '')
      + '标普500与纳指使用境内 ETF 代理，包含汇率、溢折价及跟踪误差，不等同于指数本身。</p>';
    host.innerHTML = legend.join('') + svg.join('') + rows.join('') + note;
  }

  function scheduleComparison() {
    var host = document.getElementById('__compare');
    if (!host || COMPARE_BUSY) { return; }
    var sig = JSON.stringify(LEDGER.map(function (r) {
      return [r['代码'], r['买入日期'], r['买入价'], r['股数']];
    }));
    if (sig === COMPARE_SIG) { return; }
    COMPARE_SIG = sig;
    if (!LEDGER.length) { renderComparison({ error: '录入持仓后，这里会按相同投入日期和金额自动生成对比。' }); return; }
    var codes = [], seen = {};
    for (var i = 0; i < LEDGER.length; i++) {
      var code = String(LEDGER[i]['代码'] || '');
      if (/^\d{6}$/.test(code) && !seen[code]) { seen[code] = 1; codes.push(code); }
    }
    host.innerHTML = '<p class="muted small">正在获取 ' + codes.length + ' 只持仓的历史周线并计算…</p>';
    COMPARE_BUSY = true;
    Promise.all(codes.map(function (c) { return fetchHistory(c).then(function (p) { return [c, p]; }); }))
      .then(function (pairs) {
        var histories = {};
        for (var j = 0; j < pairs.length; j++) { histories[pairs[j][0]] = pairs[j][1]; }
        renderComparison(comparisonData(LEDGER, histories, BENCHMARKS));
      })
      .catch(function (e) { renderComparison({ error: '历史行情获取失败：' + String(e && e.message || e) }); })
      .then(function () { COMPARE_BUSY = false; });
  }

  /* ------------------------------------------------------------- 表单 */
  function el(id) { return document.getElementById(id); }

  window.__holdClear = function () {
    ['f_idx', 'f_code', 'f_name', 'f_price', 'f_shares', 'f_note'].forEach(function (k) {
      if (el(k)) { el(k).value = ''; }
    });
    if (el('f_date')) { el('f_date').value = today(); }
    if (el('f_msg')) { el('f_msg').textContent = ''; }
  };

  function today() {
    var d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  function closeHoldingDialog() {
    if (HOLD_BUSY) { return; }
    if (HOLD_DIALOG) {
      var previous = HOLD_DIALOG._previousFocus;
      HOLD_DIALOG.remove(); HOLD_DIALOG = null;
      if (previous && previous.isConnected && previous.focus) { previous.focus(); }
      else if (el('f_code')) { el('f_code').focus(); }
    }
  }
  function holdingDialog(i, deleting) {
    if (!requireAccount() || HOLD_BUSY || !LEDGER[i]) { return; }
    closeHoldingDialog();
    var snapshot = JSON.stringify(LEDGER), row = LEDGER[i];
    var dialog = document.createElement('dialog'); HOLD_DIALOG = dialog;
    dialog._previousFocus = document.activeElement;
    dialog.setAttribute('aria-labelledby', '__holdtitle');
    dialog.style.cssText = 'max-width:520px;width:calc(100% - 40px);border:1px solid #ccd5df;border-radius:12px;padding:24px;color:#172b40';
    dialog.innerHTML = '<h2 id="__holdtitle">' + (deleting ? '确认删除持仓' : '编辑持仓') + '</h2><p>'
      + esc(row['代码']) + ' ' + esc(row['名称']) + ' · ' + esc(row['买入日期']) + ' · 买入价 ' + esc(row['买入价'])
      + ' · 股数 ' + esc(row['股数']) + '</p>';
    var inputs = {};
    if (!deleting) { LEDGER_COLS.forEach(function (key) {
      var label = document.createElement('label'), input = document.createElement('input');
      label.textContent = key + ' '; input.value = row[key] || '';
      input.setAttribute('aria-label', key); input.style.cssText = 'display:block;width:100%;margin:6px 0 12px;box-sizing:border-box';
      if (key === '买入日期') { input.type = 'date'; }
      label.appendChild(input); dialog.appendChild(label); inputs[key] = input;
    }); }
    var msg = document.createElement('p'); msg.setAttribute('role', 'status'); dialog.appendChild(msg);
    var cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = '取消'; cancel.onclick = closeHoldingDialog;
    var submit = document.createElement('button'); submit.type = 'button'; submit.textContent = deleting ? '确认删除' : '保存修改';
    submit.onclick = function () {
      if (HOLD_BUSY) { return; }
      if (JSON.stringify(LEDGER) !== snapshot) { msg.textContent = '持仓已发生变化，请取消并重新打开。'; return; }
      var rows = LEDGER.slice();
      if (deleting) { rows.splice(i, 1); }
      else {
        var draft = {}; LEDGER_COLS.forEach(function (k) { draft[k] = inputs[k].value.trim(); });
        draft['代码'] = cleanCode(draft['代码']);
        var error = holdingError(draft); if (error) { msg.textContent = error; return; }
        rows[i] = draft;
      }
      commitLedger(rows, function () { closeHoldingDialog(); window.__toast(deleting ? '已删除持仓' : '已保存修改'); }, msg);
    };
    dialog.appendChild(cancel); dialog.appendChild(submit); document.body.appendChild(dialog);
    dialog.addEventListener('cancel', function (event) { event.preventDefault(); closeHoldingDialog(); });
    dialog.showModal(); (deleting ? cancel : inputs['代码']).focus();
  }
  function holdingError(row) {
    if (!/^\d{6}$/.test(row['代码'])) { return '代码必须是 6 位数字。'; }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(row['买入日期'])) { return '请填写买入日期。'; }
    if (!isFinite(Number(row['买入价'])) || Number(row['买入价']) <= 0) { return '买入价必须大于零。'; }
    if (!Number.isSafeInteger(Number(row['股数'])) || Number(row['股数']) <= 0) { return '股数必须是正整数。'; }
    if (row['名称'].length > 100 || row['备注'].length > 1000) { return '名称或备注过长。'; }
    return '';
  }
  window.__holdEdit = function (i) { holdingDialog(i, false); };
  window.__holdDel = function (i) { holdingDialog(i, true); };
  window.__holdSave = function () {
    if (!requireAccount() || HOLD_BUSY) { return; }
    var code = cleanCode(el('f_code').value), row = {
      '代码': code, '名称': el('f_name').value.trim() || (ENTRIES[code] && ENTRIES[code].name) || '',
      '买入日期': el('f_date').value || '', '买入价': el('f_price').value.trim(),
      '股数': el('f_shares').value.trim(), '备注': el('f_note').value.trim()
    };
    var error = holdingError(row); if (error) { el('f_msg').textContent = error; return; }
    var rows = LEDGER.slice(), how = upsert(row, rows);
    return commitLedger(rows, function () {
      el('f_msg').textContent = how === 'upsert' ? '已覆盖同一笔（代码+日期+价都相同）。' : '已新增一笔。';
      window.__toast('已保存持仓');
    }, el('f_msg'));
  };

  /* ------------------------------------------- 导入 / 导出 / 清空（本地） */
  window.__holdExport = function () {
    var lines = ['代码,名称,买入日期,买入价,股数,备注'];
    for (var i = 0; i < LEDGER.length; i++) {
      lines.push(LEDGER_COLS.map(function (c) {
        var v = String(LEDGER[i][c] == null ? '' : LEDGER[i][c]);
        return /[",\n]/.test(v) ? ('"' + v.replace(/"/g, '""') + '"') : v;
      }).join(','));
    }
    var blob = new Blob(['\ufeff' + lines.join('\r\n') + '\r\n'], { type: 'text/csv;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'holdings.csv';
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 500);
    window.__toast('已导出 ' + LEDGER.length + ' 行，存到你电脑的下载文件夹');
  };

  function parseCsv(text) {
    text = String(text).replace(/^\ufeff/, '');
    var rows = [], row = [], cur = '', q = false;
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (q) {
        if (ch === '"') { if (text[i + 1] === '"') { cur += '"'; i += 1; } else { q = false; } }
        else { cur += ch; }
      } else if (ch === '"') { q = true; }
      else if (ch === ',') { row.push(cur); cur = ''; }
      else if (ch === '\n') { row.push(cur); rows.push(row); row = []; cur = ''; }
      else if (ch !== '\r') { cur += ch; }
    }
    if (cur !== '' || row.length) { row.push(cur); rows.push(row); }
    return rows.filter(function (r) { return r.some(function (x) { return String(x).trim() !== ''; }); });
  }

  window.__holdImport = function () {
    if (HOLD_BUSY) { return; }
    var candidate = LEDGER.slice();
    var box = el('f_import');
    if (!box) { return; }
    var rows = parseCsv(box.value || '');
    if (!rows.length) { window.__toast('没读到内容：把 holdings.csv 里的行粘进来'); return; }
    var head = rows[0].map(function (x) { return String(x).trim(); });
    var idx = {};
    LEDGER_COLS.forEach(function (c) { idx[c] = head.indexOf(c); });
    if (idx['代码'] < 0) { window.__toast('第一行得是表头，且要有「代码」这一列'); return; }
    var added = 0, bad = 0;
    for (var i = 1; i < rows.length; i++) {
      var r = rows[i], code = cleanCode(r[idx['代码']]);
      if (!/^\d{6}$/.test(code)) { bad += 1; continue; }
      var o = {};
      LEDGER_COLS.forEach(function (c) { o[c] = idx[c] >= 0 ? String(r[idx[c]] == null ? '' : r[idx[c]]).trim() : ''; });
      o['代码'] = code;
      upsert(o, candidate); added += 1;
    }
    return commitLedger(candidate, function () {
      box.value = ''; window.__toast('导入完成：' + added + ' 行，跳过 ' + bad + ' 行');
    });
  };

  window.__holdWipe = function () {
    if (HOLD_BUSY) { return; }
    if (!confirm('确认清空这台浏览器里存的全部持仓？（导出一份就不会丢）')) { return; }
    return commitLedger([], function () { window.__toast('已清空本地台账'); });
  };

  window.__pickAdd = function () {
    if (HOLD_BUSY) { return; }
    var candidate = LEDGER.slice();
    var trs = document.querySelectorAll('tr[data-pick]');
    var date = el('__pdate') ? el('__pdate').value : today();
    var note = el('__pnote') ? el('__pnote').value : '';
    var n = 0;
    for (var i = 0; i < trs.length; i++) {
      var cb = trs[i].querySelector('input[type=checkbox]');
      if (!cb || !cb.checked) { continue; }
      var code = trs[i].getAttribute('data-pick');
      var o = {
        '代码': cleanCode(code), '名称': trs[i].getAttribute('data-name') || '',
        '买入日期': date,
        '买入价': (trs[i].querySelector('input.ip').value || '').trim(),
        '股数': (trs[i].querySelector('input.is').value || '').trim(),
        '备注': note
      };
      if (!/^\d{6}$/.test(o['代码'])) { continue; }
      upsert(o, candidate); n += 1;
    }
    if (!n) { window.__toast('先勾选至少一只股票'); return; }
    return commitLedger(candidate, function () { window.__toast('已加入 ' + n + ' 笔'); location.href = 'my.html'; });
  };

  /* ------------------------------------------------------------- 主循环 */
  var BUSY = false;
  var TIMER = null;
  var LAST_SIG = '';

  // 只用一个定时器。原来 tick() 末尾无条件 setTimeout(tick)，再手动催一次就会
  // 变成两条自调度链，每个周期抓两次行情——所以统一走 schedule()。
  function schedule(ms) {
    if (TIMER) { clearTimeout(TIMER); }
    TIMER = setTimeout(tick, ms);
  }
  function tickSoon() { schedule(50); }

  // 「骨架」指纹：行数 / 每行的代码与股数 / 动作 / 有没有现价 / 有几行有现价。
  // 只要它没变，就**只调 apply() 就地改数字**，不重画表格——
  // 重画会把闪色动画和用户选中的文字都冲掉，而且白费力气。
  function structSig(p) {
    var parts = [LEDGER.length, (p.summaryRows && p.summaryRows['有现价只数']) || 0];
    for (var i = 0; i < p.rows.length; i++) {
      var r = p.rows[i];
      parts.push(r.code, r.shares, r.action, r.level, r.price === null ? 0 : 1);
    }
    return parts.join('|');
  }

  function tick() {
    TIMER = null;
    if (BUSY || document.hidden) { schedule(IV); return; }
    refreshBuildState();
    renderPickFinancials();
    BUSY = true;
    fetchQuotes(wantedCodes(), function (q, err) {
      QUOTES = q;
      var p = withSummary(buildPayload(err));
      var sig = structSig(p);
      apply(p);
      renderStaticFinancialAlerts();
      if (sig !== LAST_SIG) {          // 结构变了才重画（例如「待报价」变成有价）
        LAST_SIG = sig;
        renderLedger(); renderLive(); renderKpi();
      }
      BUSY = false;
      schedule(IV);
    });
  }

  function refreshBuildState() {
    var bs = document.getElementById('__buildstate');
    if (!bs) { return; }
    bs.textContent = (publicScoreFresh(Date.now()) ? '' : '⚠️ 评分待新行情核验；')
      + '评分生成于 ' + (SITE.built_at || '—')
      + '；行情报价 ' + (SITE.quote_at || '—') + '（北京时间）';
  }

  function checkPublishedScore() {
    var active = document.activeElement;
    if (document.hidden || (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName))) {
      setTimeout(checkPublishedScore, 60000);
      return;
    }
    var script = document.createElement('script');
    script.src = 'site.js?poll=' + Math.floor(Date.now() / 300000);
    script.onload = function () {
      script.remove();
      var latest = window.__SITE__ || {};
      if (latest.built_at && latest.built_at !== SITE.built_at) {
        var key = 'touzi_last_score_reload';
        try {
          if (sessionStorage.getItem(key) === latest.built_at) {
            var notice = document.getElementById('__buildstate');
            if (notice) { notice.textContent = '发现新评分，请手动刷新页面'; }
            return;
          }
          sessionStorage.setItem(key, latest.built_at);
        } catch (e) {
          var fallback = document.getElementById('__buildstate');
          if (fallback) { fallback.textContent = '发现新评分，请手动刷新页面'; }
          return;
        }
        window.location.reload();
        return;
      }
      window.__SITE__ = SITE;
      setTimeout(checkPublishedScore, 300000);
    };
    script.onerror = function () {
      script.remove();
      window.__SITE__ = SITE;
      setTimeout(checkPublishedScore, 300000);
    };
    document.head.appendChild(script);
  }

  /* ------------------------------ 公共企业财务详情：只展示后端判定 */
  // 静态站仅对本浏览器台账提醒；公开财报快照过期时保留旧提醒并标记待核验。
  var FINANCIAL_ALERTS = {};
  function renderStaticFinancialAlerts() {
    if (!HAS_DOM || document.hidden) { return; }
    var main = document.querySelector('main'); if (!main) { return; }
    var host = document.getElementById('__staticfinancialalerts');
    if (!host) { host = document.createElement('section'); host.id = '__staticfinancialalerts'; host.className = 'card'; main.appendChild(host); }
    var next = {}, changed = [], seen = {};
    LEDGER.forEach(function (h) {
      var code = h['代码']; if (seen[code]) { return; } seen[code] = true;
      var e = ENTRIES[code], snap = e && e.sell_snapshot, old = FINANCIAL_ALERTS[code];
      var valid = publicScoreFresh(Date.now()) && e && e.financial_capability && snap && Array.isArray(snap.financial_missing);
      var checks = valid ? (e.sell_checks || []).filter(function (c) {
        return /^(sell_debt_equity|sell_equity_nonpositive|sell_capex_to_profit|sell_annual_loss|sell_cumulative_loss)$/.test(c.key) && c.known && c.hit;
      }) : [];
      var complete = valid && !snap.financial_missing.length;
      var keys = checks.map(function (c) { return c.key; }).sort(), reasons = checks.map(function (c) { return c.label; });
      if (old && !complete) { keys = Array.from(new Set(keys.concat(old.keys))).sort(); reasons = Array.from(new Set(reasons.concat(old.reasons))); }
      if (reasons.length) {
        next[code] = { keys: keys, reasons: reasons, pending: !complete, name: h['名称'] || '' };
        if (checks.length && (!old || old.keys.join('|') !== keys.join('|'))) { changed.push(code + '：财务预警、提示复核'); }
      }
    });
    FINANCIAL_ALERTS = next;
    host.innerHTML = Object.keys(next).length ? '<h3>财务预警、提示复核</h3>' + Object.keys(next).map(function (code) {
      var a = next[code]; return '<p><strong>' + esc(code + ' ' + a.name) + (a.pending ? '（上次触发，当前待核验）' : '')
        + '</strong>：' + esc(a.reasons.join('；')) + '</p>';
    }).join('') + '<p class="small muted">财报为发布快照；仅网页可见时检查，关闭后不提醒。财务预警不直接给出减仓或清仓建议。</p>' : '';
    host.hidden = !Object.keys(next).length;
    if (changed.length && window.__toast) { window.__toast(changed.join('；')); }
  }

  function financialNumber(value, factor, suffix) {
    return typeof value === 'number' && isFinite(value) ? (value * factor).toFixed(2) + (suffix || '') : '暂无数据';
  }
  function financialCoverageHTML() {
    var coverage = financialCoverage(ENTRIES), labels = {debt_equity:'债务股权比率',capex_to_profit:'五年资本开支比例',annual_net_profit:'年度净利润',cumulative_net_profit:'五年累计净利润'};
    var html = '<details data-financial-detail="coverage"><summary>本页财报覆盖：' + coverage.total + '只股票（当前评分集合，并非全市场）</summary>';
    html += '<p>报告/指标总体状态：已核验 ' + (coverage.reports.ok || 0) + '只，部分数据 ' + (coverage.reports.partial || 0)
      + '只，数据不足 ' + (coverage.reports.missing || 0) + '只，旧数据 ' + (coverage.reports.stale || 0)
      + '只，异常或无效 ' + (coverage.reports.invalid || 0) + '只，行业不适用 ' + (coverage.reports.not_applicable || 0)
      + '只；质量子项全部可用 ' + coverage.quality.full + '只、部分可用 ' + coverage.quality.partial + '只、全部缺失 '
      + coverage.quality.missing + '只。已核验不代表所有展示字段完整。</p>';
    html += '<div class="table-wrap"><table><thead><tr><th>指标</th><th>可用</th><th>缺失/待核验</th><th>行业不适用</th><th>异常/不可计算</th><th>旧数据</th></tr></thead><tbody>';
    Object.keys(labels).forEach(function (key) { var item = coverage.metrics[key] || {};
      html += '<tr><th>' + labels[key] + '</th><td>' + (item.ok || 0) + '</td><td>' + ((item.missing || 0)+(item.partial || 0))
        + '</td><td>' + (item.not_applicable || 0) + '</td><td>' + (item.invalid || 0) + '</td><td>' + (item.stale || 0) + '</td></tr>'; });
    html += '</tbody></table></div><p>行业归属缺失 ' + coverage.reasons.industry_unknown + '只（可能已有财报金额，但通用阈值适用性无法确认）；年度基础金额不完整 '
      + coverage.reasons.amounts_missing + '只。扣非归母净利润缺失 ' + coverage.fields.deducted_parent_missing + '/' + coverage.fields.expected
      + '个年度单元格，当前适配器未覆盖可靠字段。缺失不补零，行业不适用不算取数失败。</p></details>';
    return html;
  }
  function updateFinancialBody(entry) {
    var host = document.getElementById('__financialbody'), expanded = {};
    // 定时刷新保留用户展开的统计/明细，不能每15秒把正在阅读的内容折叠。
    if (host.querySelectorAll) { Array.prototype.forEach.call(host.querySelectorAll('details[data-financial-detail]'), function (detail) {
      expanded[detail.getAttribute('data-financial-detail')] = detail.open;
    }); }
    host.innerHTML = financialCoverageHTML() + financialHTML(entry);
    if (host.querySelectorAll) { Array.prototype.forEach.call(host.querySelectorAll('details[data-financial-detail]'), function (detail) {
      var key = detail.getAttribute('data-financial-detail');
      if (Object.prototype.hasOwnProperty.call(expanded, key)) { detail.open = expanded[key]; }
    }); }
  }
  function financialHTML(entry) {
    var a = entry && entry.financial_analysis;
    if (!a) { return '<p>本版本尚无企业财务数据，无法判断。请等待财务数据更新。</p>'; }
    var states = { ok: '已核验', partial: '部分数据', missing: '数据不足', stale: '旧缓存/已过期',
      invalid: '财务异常或口径无效', not_applicable: '行业不适用' };
    function state(value) { return states[value] || '待核验'; }
    var metrics = a.metrics || {}, debt = metrics.debt_equity || {}, capex = metrics.capex_to_profit || {};
    function verdict(metric, good, bad) {
      if (metric.status !== 'ok') { return state(metric.status); }
      return metric.excellent === true ? good : metric.excellent === false ? bad : '无法判断';
    }
    var win = a.window || {}, rows = a.years || [], years = win.years || [];
    var quality = a.quality || {};
    var byYear = {}; rows.forEach(function (row) { byYear[row.fiscal_year] = row; });
    var html = '<p><strong>' + esc(state(a.status)) + '</strong> · 五年窗口：' + esc(win.start_year || '未知')
      + '—' + esc(win.end_year || '未知') + '（完整财年）</p>'
      + '<p>本股质量评分覆盖：' + financialNumber(quality.coverage, 100, '%') + '（可用子项权重；不代表财报字段完整率）</p>'
      + '<p>债务股权比率：<strong>' + financialNumber(debt.value, 1, ' 倍') + '</strong> · '
      + esc(verdict(debt, '优良', '未达优良标准')) + ' · 报告期 ' + esc(debt.report_date || '未知')
      + '<br>五年累计资本开支÷累计净利润：<strong>' + financialNumber(capex.value, 100, '%')
      + '</strong> · ' + esc(verdict(capex, '达标', '未达标')) + '</p>'
      + '<p class="small muted">总负债÷合并股东权益（含少数股东权益）严格低于0.8为优良；五年累计资本开支÷累计合并净利润严格低于25%为达标。'
      + '结论使用未舍入原值；显示值可能接近阈值。低资本开支只提供竞争力线索，不能单独证明持续竞争优势。</p>';
    html += '<div style="overflow-x:auto" tabindex="0" role="region" aria-label="五年企业财务表"><table><caption>金额：人民币亿元；缺失不补零，季度累计数据不相加</caption>'
      + '<thead><tr><th scope="col">财年/报告期</th><th scope="col">营业收入</th><th scope="col">营业利润</th><th scope="col">合并净利润</th>'
      + '<th scope="col">扣非归母净利润</th><th scope="col">净利率</th><th scope="col">经营现金流净额</th><th scope="col">资本开支</th>'
      + '<th scope="col">资本开支÷净利润</th></tr></thead><tbody>';
    years.forEach(function (year) {
      var r = byYear[year] || {};
      html += '<tr><th scope="row">' + esc(year) + '<br><span class="small muted">' + esc(r.report_date || '未取得年报') + '</span></th>';
      ['revenue', 'operating_profit', 'net_profit', 'deducted_parent_net_profit'].forEach(function (key) {
        html += '<td>' + financialNumber(r[key], 1e-8) + (key === 'net_profit' && r[key] < 0 ? '（亏损）' : '') + '</td>';
      });
      html += '<td>' + financialNumber(r.net_margin, 100, '%') + '</td><td>' + financialNumber(r.operating_cash_flow, 1e-8)
        + '</td><td>' + financialNumber(r.capex, 1e-8) + '</td><td>' + financialNumber(r.capex_to_profit, 100, '%') + '</td></tr>';
    });
    html += '</tbody></table></div><p class="small">五年累计净利润：' + financialNumber(capex.net_profit_sum, 1e-8, '亿元')
      + ' · 五年累计资本开支：' + financialNumber(capex.capex_sum, 1e-8, '亿元') + '。资本开支口径：购建固定资产、无形资产和其他长期资产支付的现金；净利润非正时比例无法判断。</p>';
    if ((a.loss_years || []).length) { html += '<p>亏损年份：' + esc(a.loss_years.join('、')) + '</p>'; }
    var q = a.quality || {}, labels = { roe: 'ROE', cashflow: '经营现金流÷净利润', debt: '债务股权比率', stability: '盈利稳定性', capex: '五年资本开支比例' };
    html += '<details data-financial-detail="quality"><summary>质量评分明细与覆盖率：' + financialNumber(q.coverage, 100, '%') + '</summary>'
      + '<p class="small">缺失或行业不适用项在可用项内重新归一化，覆盖率反映原权重中有数据参与的比例。</p>'
      + '<p class="small">总分折算列按配置的质量权重展示；总分其他模块缺失并归一化时，实际贡献会变化。</p>'
      + '<div style="overflow-x:auto"><table><thead><tr><th>子项</th><th>配置权重</th><th>子项分</th><th>质量分贡献</th><th>总分折算</th></tr></thead><tbody>';
    Object.keys(labels).forEach(function (key) {
      var c = (q.contributions || {})[key], weight = (SITE.stock_weights || {}).quality;
      html += '<tr><th scope="row">' + labels[key] + '</th><td>' + financialNumber((q.weights || {})[key], 100, '%')
        + '</td><td>' + financialNumber((q.sub_scores || {})[key], 1) + '</td><td>' + financialNumber(c, 1)
        + '</td><td>' + (typeof c === 'number' && typeof weight === 'number' ? financialNumber(c * weight, 1) : '暂无数据') + '</td></tr>';
    });
    html += '</tbody></table></div><p>缺项：' + esc((q.missing || []).map(function (key) { return labels[key] || key; }).join('、') || '无') + '</p></details>';
    var warnings = entry.sell_snapshot && entry.sell_snapshot.financial_warn || [];
    html += '<p>财务预警：' + esc(warnings.length ? warnings.join('；') : a.status === 'ok' ? '本轮新增财务条件未触发' : '数据不足或不适用，请核对指标状态')
      + '。财务预警提示复核；总分变化仍参与原有减仓、清仓规则。</p>';
    html += '<p class="small muted">来源：' + esc(a.source || '未知') + ' · 币种：' + esc(a.currency || '未知')
      + ' · 原始单位：' + esc(a.unit || '未知') + ' · 报表口径：' + esc(a.statement_scope === 'consolidated' ? '合并报表' : a.statement_scope || '未知')
      + '<br>实际获取：' + esc(a.fetched_at || '未知') + ' · 来源更新时间：' + esc(a.source_updated_at || '未知（不以获取时间代替披露日）')
      + '<br>财报随交易日数据任务检查更新，休市保留最近合格快照，行情刷新不会产生新财报。'
      + ((a.notes || []).length ? '<br>' + esc(a.notes.join('；')) : '') + '</p>';
    return html;
  }
  function financialSearch(confirm) {
    var input = document.getElementById('__financialsearch');
    var select = document.getElementById('__financialcode');
    var results = document.getElementById('__financialresults');
    var message = document.getElementById('__financialmessage');
    if (!input || input.__composing) { return; }
    var query = input.value.trim().toLowerCase();
    var matches = query ? Object.keys(ENTRIES).sort().filter(function (code) {
      return code.indexOf(query) !== -1 || String(ENTRIES[code].name || '').toLowerCase().indexOf(query) !== -1;
    }) : [];
    results.innerHTML = '';
    if (!query) { message.textContent = '可输入中文名称或股票代码搜索，也可直接下拉选择。'; return; }
    if (!matches.length) { message.textContent = '当前评分覆盖股票中没有匹配结果，请检查名称或代码。'; return; }
    if (confirm && matches.length === 1) {
      select.value = matches[0];
      updateFinancialBody(ENTRIES[matches[0]]);
      message.textContent = '已选择 ' + matches[0] + ' ' + (ENTRIES[matches[0]].name || '');
      return;
    }
    message.textContent = '找到 ' + matches.length + ' 只股票，请选择：';
    matches.forEach(function (code) {
      var button = document.createElement('button');
      button.type = 'button'; button.className = 'b b2';
      button.textContent = code + ' ' + (ENTRIES[code].name || '');
      button.style.margin = '4px';
      button.addEventListener('click', function () {
        select.value = code;
        updateFinancialBody(ENTRIES[code]);
        results.innerHTML = '';
        message.textContent = '已选择 ' + code + ' ' + (ENTRIES[code].name || '');
        input.focus();
      });
      results.appendChild(button);
    });
  }

  function renderPickFinancials() {
    if (!HAS_DOM || !document.querySelectorAll) { return; }
    updatePickFinancialColumns(document, ENTRIES, function (entry) {
      return publicScoreFresh(Date.now()) ? 'snapshot' : 'pending';
    });
  }

  function renderFinancials() {
    renderPickFinancials();
    if (!HAS_DOM) { return; }
    var main = document.querySelector('main'); if (!main) { return; }
    var host = document.getElementById('__financials');
    if (!host) {
      host = document.createElement('section'); host.id = '__financials'; host.className = 'card';
      host.innerHTML = '<h3>企业财务分析</h3>'
        + '<form id="__financialform" style="margin-bottom:12px"><label for="__financialsearch">搜索股票（中文名称或代码）</label> '
        + '<input id="__financialsearch" type="search" maxlength="100" placeholder="例如：平安银行 或 000001" autocomplete="off" aria-describedby="__financialmessage" style="max-width:100%"> '
        + '<button class="b b1" type="submit">确定</button></form>'
        + '<p id="__financialmessage" class="muted" role="status">可输入中文名称或股票代码搜索，也可直接下拉选择。</p>'
        + '<div id="__financialresults" role="group" aria-label="匹配股票" style="max-height:180px;overflow:auto"></div>'
        + '<label for="__financialcode">选择股票</label> '
        + '<select id="__financialcode" style="max-width:100%"></select>'
        + '<p class="muted">年度更新：每年5月1日，五年窗口向前滚动一年；交易日数据任务检查新财报及更正，休市保留最近合格快照，来源和任务延迟可能影响显示。财报不是固定不变的数据，行情刷新也不代表新财报已披露。</p>'
        + '<div id="__financialbody"></div>';
      main.appendChild(host);
      var input = document.getElementById('__financialsearch');
      input.addEventListener('compositionstart', function () { input.__composing = true; });
      input.addEventListener('compositionend', function () { input.__composing = false; financialSearch(false); });
      input.addEventListener('input', function () { financialSearch(false); });
      input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' && (event.isComposing || input.__composing || event.keyCode === 229)) { event.preventDefault(); }
      });
      document.getElementById('__financialform').addEventListener('submit', function (event) {
        event.preventDefault();
        if (!input.__composing) { financialSearch(true); }
      });
      document.getElementById('__financialcode').addEventListener('change', function () {
        updateFinancialBody(ENTRIES[this.value]);
        document.getElementById('__financialresults').innerHTML = '';
        document.getElementById('__financialmessage').textContent = '已选择 ' + this.value + ' ' + (ENTRIES[this.value].name || '');
      });
    }
    var select = document.getElementById('__financialcode'), selected = select.value;
    var codes = Object.keys(ENTRIES).sort();
    var options = codes.map(function (code) { return '<option value="' + esc(code) + '">' + esc(code + ' ' + (ENTRIES[code].name || '')) + '</option>'; }).join('');
    if (select.innerHTML !== options) {
      select.innerHTML = options;
      if (ENTRIES[selected]) { select.value = selected; }
      financialSearch(false);
    }
    updateFinancialBody(ENTRIES[select.value]);
  }

  function boot() {
    renderFinancials();
    loadLedger();
    refreshBuildState();
    if (document.getElementById('__ledger') || document.getElementById('__kpi')) { window.__renderAll(); }
    var d = el('f_date'); if (d && !d.value) { d.value = today(); }
    var pd = el('__pdate'); if (pd && !pd.value) { pd.value = today(); }
    tick();
    setTimeout(checkPublishedScore, 300000);
  }

  if (HAS_DOM) {
    // 切回这个标签页就立刻刷一次，别让用户对着旧价格发呆
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { tickSoon(); }
    });
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', boot); }
    else { boot(); }
  }

  // 给 Node 单元测试用
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { pickFinancialMetric: pickFinancialMetric, renderPickFinancials: renderPickFinancials, financialHTML: financialHTML, renderFinancials: renderFinancials, parseQuoteText: parseQuoteText, oneRow: oneRow, sellDecision: sellDecision,
      summarize: summarize, valuationQuoteValid: valuationQuoteValid,
      planSellShares: planSellShares, sellRatioFor: sellRatioFor, buyFees: buyFees,
      sellFees: sellFees, txSym: txSym, alertFor: alertFor, parseHistory: parseHistory,
      comparisonData: comparisonData };
  }
})();

