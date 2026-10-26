// 选股表只展示已有财务判断，不重新计算评分或增加买入否决条件。
export function pickFinancialMetric(entry, key, mode) {
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

export function updatePickFinancialColumns(doc, entries, modeFor) {
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
