/* ===========================================================================
 * 公网版（纯静态）网站的前端引擎。
 *
 * 本机版（scripts/serve.py）的实时能力来自后台线程 + /api/quotes。静态版没有
 * 后端，所以这一层把「服务器原来干的事」搬到浏览器里：
 *
 *   1. 行情：浏览器**直连腾讯** https://qt.gtimg.cn（实测返回
 *      `Access-Control-Allow-Origin: *`，所以任何域名的页面都能直接 fetch）。
 *   2. 持仓台账：存在**浏览器本地** localStorage，不上传任何地方。
 *      「建议动作 / 触发的卖出条件 / 预警」这类结论取决于打分与规则，浏览器算不了，
 *      所以由发布脚本在 `site.json` 的 `entries` 里烘好，按代码取用。
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

  // Node 里跑单元测试时没有 window / document，所以全部做存在性判断。
  var W = (typeof window !== 'undefined') ? window : {};
  var HAS_DOM = (typeof document !== 'undefined' && !!document.getElementById);
  var SITE = (typeof window !== 'undefined' && window.__SITE__) || {};
  var ENTRIES = SITE.entries || {};
  var FEES = SITE.fees || {};
  var PLAN = SITE.plan || {};
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
        var v = parseFloat(row[NUM[j]]);
        row[NUM[j]] = isNaN(v) ? null : v;
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

    var action = String(e.action || '');
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
      String(e.triggers || ''), String(e.warn || ''), hasPrice, hasShares);

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
    if (!hasPrice) { return ['没有现价（不在本次打分范围内，或已停牌），无法计算盈亏', 'na']; }
    if (!hasShares) {
      return ['没填「股数」，只给买卖建议、不算金额。想看到「卖多少股 / 净赚多少」，请在台账里补上股数。', 'watch'];
    }
    if (action === '清仓') {
      return ['🚨 **清仓**：卖出全部 ' + sellSh + ' 股（100%）。触发：' + (triggers || '详见规则') + money, 'act'];
    }
    if (action === '减仓') {
      return ['⚠️ **减仓 ' + Math.round(ratio * 100) + '%**：卖出 ' + sellSh + ' 股。触发：' + (triggers || '详见规则') + money, 'act'];
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
    if (LEDGER.length) { saveLedger(); }
  }
  function saveLedger() {
    try { localStorage.setItem(LEDGER_KEY, JSON.stringify(LEDGER)); } catch (e) { }
  }
  function cleanCode(v) {
    var s = String(v == null ? '' : v).trim().toUpperCase();
    s = s.replace(/^(SH|SZ|BJ)/, '').replace(/\.(SH|SZ|BJ)$/, '');
    return s;
  }
  // 与 serve.py 的 upsert 一致的覆盖键：代码 + 买入日期 + 买入价
  function upsert(row) {
    for (var i = 0; i < LEDGER.length; i++) {
      if (String(LEDGER[i]['代码']) === String(row['代码'])
        && String(LEDGER[i]['买入日期'] || '') === String(row['买入日期'] || '')
        && String(LEDGER[i]['买入价'] || '') === String(row['买入价'] || '')) {
        LEDGER[i] = row; saveLedger(); return 'upsert';
      }
    }
    LEDGER.push(row); saveLedger(); return 'add';
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
        quote_time: qt ? qt.slice(11) : '',
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
  var TONE = { '清仓': 'bad', '减仓': 'weak', '持有': 'good', '加仓': 'great' };
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
        + '<button class="b b2" type="button" onclick="__holdDel(' + i + ',' + JSON.stringify(code) + ')">删除</button>'
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

  window.__holdEdit = function (i) {
    var r = LEDGER[i];
    if (!r) { return; }
    el('f_idx').value = String(i);
    ['代码', '名称', '买入日期', '买入价', '股数', '备注'].forEach(function (k, n) {
      var id = ['f_code', 'f_name', 'f_date', 'f_price', 'f_shares', 'f_note'][n];
      if (el(id)) { el(id).value = r[k] == null ? '' : r[k]; }
    });
    el('f_msg').textContent = '已把第 ' + i + ' 行填进表单：改完点「保存」。'
      + '键是「代码+买入日期+买入价」，三者都没改就覆盖原行；改了价格或日期则是新增一笔（分批买入）。';
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  window.__holdDel = function (i, code) {
    if (!confirm('确认删除 ' + code + ' 这一行？')) { return; }
    LEDGER.splice(i, 1); saveLedger();
    window.__renderAll();
    window.__toast('已删除 ' + code + '，盈亏已按新台账重算');
  };

  window.__holdSave = function () {
    var code = cleanCode(el('f_code').value);
    if (!/^\d{6}$/.test(code)) { el('f_msg').textContent = '代码必须是 6 位数字。'; return; }
    var row = {
      '代码': code,
      '名称': (el('f_name').value || '').trim() || (ENTRIES[code] && ENTRIES[code].name) || '',
      '买入日期': el('f_date').value || '',
      '买入价': (el('f_price').value || '').trim(),
      '股数': (el('f_shares').value || '').trim(),
      '备注': (el('f_note').value || '').trim()
    };
    var how = upsert(row);
    el('f_msg').textContent = (how === 'upsert' ? '已覆盖同一笔（代码+日期+价都相同）。' : '已新增一笔。')
      + '现在共 ' + LEDGER.length + ' 行，存在这台浏览器里。';
    window.__renderAll();
    window.__toast('已保存。价格 15 秒内刷新；「建议动作」要等下次发布更新。');
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
      upsert(o); added += 1;
    }
    box.value = '';
    window.__renderAll();
    window.__toast('导入完成：' + added + ' 行' + (bad ? ('，跳过 ' + bad + ' 行（代码不是 6 位数字）') : ''));
  };

  window.__holdWipe = function () {
    if (!confirm('确认清空这台浏览器里存的全部持仓？（导出一份就不会丢）')) { return; }
    LEDGER = []; saveLedger(); window.__renderAll();
    window.__toast('已清空本地台账');
  };

  window.__pickAdd = function () {
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
      upsert(o); n += 1;
    }
    if (!n) { window.__toast('先勾选至少一只股票'); return; }
    // 先把台账表刷出来再跳转：万一浏览器拦了这次跳转，页面也不会停在原地不动
    if (window.__renderAll) { window.__renderAll(); }
    window.__toast('已加入 ' + n + ' 笔，跳转到「我的持仓」');
    setTimeout(function () { location.href = 'my.html'; }, 800);
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
    BUSY = true;
    fetchQuotes(wantedCodes(), function (q, err) {
      QUOTES = q;
      var p = withSummary(buildPayload(err));
      var sig = structSig(p);
      apply(p);
      if (sig !== LAST_SIG) {          // 结构变了才重画（例如「待报价」变成有价）
        LAST_SIG = sig;
        renderLedger(); renderLive(); renderKpi();
      }
      BUSY = false;
      schedule(IV);
    });
  }

  function boot() {
    loadLedger();
    var bs = document.getElementById('__buildstate');
    if (bs) { bs.textContent = '发布于 ' + (SITE.built_at || '—'); }
    if (document.getElementById('__ledger') || document.getElementById('__kpi')) { window.__renderAll(); }
    var d = el('f_date'); if (d && !d.value) { d.value = today(); }
    var pd = el('__pdate'); if (pd && !pd.value) { pd.value = today(); }
    tick();
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
    module.exports = { parseQuoteText: parseQuoteText, oneRow: oneRow, summarize: summarize,
      planSellShares: planSellShares, sellRatioFor: sellRatioFor, buyFees: buyFees,
      sellFees: sellFees, txSym: txSym, alertFor: alertFor };
  }
})();
