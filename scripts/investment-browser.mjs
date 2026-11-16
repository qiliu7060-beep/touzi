// 同时用于静态站、云端与确定性金额测试；不读取个人台账。
export const INVESTMENT_SOURCE = 'https://raw.githubusercontent.com/qiliu7060-beep/touzi/close-data/investment-context.json';
export function investmentAllocation(text, position, score, factor) {
  if (typeof text !== 'string' || !/^[0-9]+(?:\.[0-9]{1,2})?$/.test(text) || Number(text) > 100000000) throw new Error('请输入0至100000000元，最多两位小数');
  if (![position,score,factor].every(Number.isFinite) || position < 0 || position > 1 || score < 0 || score > 100 || ![0,.5,.75,1].includes(factor)) throw new Error('分配参数待核验');
  const [whole, fraction = ''] = text.split('.');
  const cents = BigInt(whole)*BigInt(100)+BigInt((fraction+'00').slice(0,2));
  // 以百万分之一为合同精度；2/3单独保持精确有理数基准。
  let denominator = BigInt(1000000), numerator = BigInt(Math.round(position*1000000));
  if (Math.abs(position-2/3)<1e-12) { numerator=BigInt(2); denominator=BigInt(3); }
  if (score<=30) numerator=BigInt(0);
  let bn=denominator-numerator, bd=denominator;
  if (bn*BigInt(3)>bd) { bn=BigInt(1); bd=BigInt(3); }
  const stock=cents*numerator/denominator, bond=cents*bn*BigInt(factor*4)/(bd*BigInt(4));
  return {stock:Number(stock),bond:Number(bond),cash:Number(cents-stock-bond)};
}
export function validateInvestment(value, now, calendar) {
  const fail=()=>{throw new Error('投资环境数据未通过核验');};
  if (!value || value.schema_version!==1 || value.kind!=='touzi-investment-v1' || value.rule_version!=='yield-trend-v1' || JSON.stringify(value.trading_calendar)!==JSON.stringify(calendar)) fail();
  const keys=(x,list)=>x && Object.keys(x).sort().join('|')===list.split(',').sort().join('|');
  if(!keys(value,'schema_version,kind,rule_version,as_of,built_at,trading_calendar,macro,bond,errors') || !Array.isArray(value.errors) || value.errors.length) fail();
  const time=x=> typeof x==='string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(x) && Number.isFinite(Date.parse(x)) && Date.parse(x)<=now+60000;
  if (!time(value.built_at) || !/^\d{4}-\d{2}-\d{2}$/.test(value.as_of || '')) fail();
  const m=value.macro,b=value.bond;
  const date=x=>typeof x==='string' && /^\d{4}-\d{2}-\d{2}$/.test(x) && Number.isFinite(Date.parse(x+'T12:00:00Z')) && new Date(x+'T12:00:00Z').toISOString().slice(0,10)===x;
  if(!date(value.as_of) || !date(b?.observation_date)) fail();
  if (!keys(m,'score,position,computed_at,status') || !keys(b,'yield_pct,observation_date,fetched_at,delta20_bp,delta60_bp,status,factor,reasons') || !Array.isArray(b.reasons) || !b.reasons.length || b.reasons.some(x=>typeof x!=='string'||x.length>500)) fail();
  if (!m || !b || !time(m.computed_at) || !time(b.fetched_at) || Date.parse(m.computed_at)>Date.parse(value.built_at) || Date.parse(b.fetched_at)>Date.parse(value.built_at) || !/^\d{4}-\d{2}-\d{2}$/.test(b.observation_date || '') || Date.parse(b.observation_date+'T00:00:00+08:00')>Date.parse(b.fetched_at)) fail();
  if (m.status!=='ok' || b.status!=='ok' || ![m.score,m.position,b.yield_pct,b.delta20_bp,b.delta60_bp,b.factor].every(Number.isFinite) || m.score<0 || m.score>100 || m.position<0 || m.position>1 || b.yield_pct<0 || b.yield_pct>100 || ![0,.5,.75,1].includes(b.factor)) fail();
  const local=new Date(now+28800000), today=local.toISOString().slice(0,10);
  const trading=day=>day>=calendar.start && day<=calendar.end && ![0,6].includes(new Date(day+'T12:00:00Z').getUTCDay()) && !calendar.closures.some(([a,z])=>day>=a&&day<=z);
  if (today<calendar.start || today>calendar.end || b.observation_date<calendar.start || b.observation_date>calendar.end || !trading(b.observation_date)) fail();
  let end=new Date(today+'T12:00:00Z');
  if (local.toISOString().slice(11,16)<'15:00') end=new Date(end.getTime()-86400000);
  const age=day=>{let n=0;for(let d=new Date(day+'T12:00:00Z');d<=end;d=new Date(d.getTime()+86400000)) {const t=d.toISOString().slice(0,10);if(t>day&&trading(t)) n++;}return n;};
  const macroDay=new Date(Date.parse(m.computed_at)+28800000).toISOString().slice(0,10);
  if (value.as_of!==new Date(Date.parse(value.built_at)+28800000).toISOString().slice(0,10)) fail();
  if (macroDay<calendar.start || macroDay>today || value.as_of!==macroDay) fail();
  return {value, fresh:age(b.observation_date)<=2&&age(macroDay)<=2&&age(new Date(Date.parse(b.fetched_at)+28800000).toISOString().slice(0,10))<=2};
}
export function createInvestmentPanel(doc, options) {
  let host=doc.getElementById('__investment'), saved=null, error='', busy=false, attempted=0;
  function mount() {
    if(host) return true;
    const main=doc.querySelector('main');if(!main)return false;
    host=doc.createElement('section');host.id='__investment';host.className='card';
    host.innerHTML='<h2>本月投资分配</h2><label>本月计划投入总额（元） <input id="__investment-amount" type="text" inputmode="decimal" autocomplete="off" placeholder="例如 3000" maxlength="12"></label><p id="__investment-status" role="status"></p><div id="__investment-result" style="font-size:20px;font-weight:700;line-height:1.8"></div><p id="__investment-reason"></p><p class="small muted">债基：鹏华稳泰30天滚动持有债券A（012648）、西部利得祥逸债券C（675093）。仅分配新增资金，不自动交易。鹏华份额有30天滚动运作期，赎回以平台为准。趋势判断仅供参考，不代表未来收益。</p>';
    main.insertBefore(host,main.firstChild);
    doc.getElementById('__investment-amount').addEventListener('input',render);
    render();return true;
  }
  function render() {
    if(!host)return;
    if(options.context){const current=options.context();saved=current.value;error=current.error||'';}
    const status=doc.getElementById('__investment-status'),result=doc.getElementById('__investment-result'),reason=doc.getElementById('__investment-reason');
    if(!saved){status.textContent=error||'投资环境待更新；暂不生成自动投入建议。';result.textContent='股票：待核验　债基：待核验　预留现金：待计算';return;}
    let checked;try{checked=validateInvestment(saved,options.now(),options.calendar);}catch{status.textContent='历史数据待核验，暂停当前自动建议';result.textContent='股票：待核验　债基：待核验　现金：待计算';return;}
    const m=saved.macro,b=saved.bond,history=error||!checked.fresh;
    status.textContent=(history?'历史参考，暂停当前自动建议':'当前分配参考')+' · 宏观计算 '+m.computed_at+' · 国债观测 '+b.observation_date+' · 抓取 '+b.fetched_at+(error?' · '+error:'');
    const stage={1:'正常',0.75:'谨慎',0.5:'防守',0:'暂停'};
    reason.textContent='股票建议 '+(m.position*100).toFixed(1)+'%'+(m.score<=30?'，宏观分≤30，暂停新增股票投入':'')+'；国债10年收益率 '+b.yield_pct.toFixed(4)+'%，近20/60交易日 '+String(b.delta20_bp)+' / '+String(b.delta60_bp)+'基点；债基'+stage[b.factor]+'（投入系数 '+b.factor*100+'%）。'+b.reasons.join('；')+'。收益率下降通常利好已有债券价格，不能等同基金未来回报。';
    const text=doc.getElementById('__investment-amount').value;
    if(!text){result.textContent='输入总额后计算股票、债基和预留现金';return;}
    try {const a=investmentAllocation(text,m.position,m.score,b.factor);const total=a.stock+a.bond+a.cash;const percent=k=>total?'（'+(a[k]/total*100).toFixed(2)+'%）':'（0.00%）';result.textContent=(history?'历史示例：':'')+'股票 '+(a.stock/100).toFixed(2)+'元'+percent('stock')+'　债基 '+(a.bond/100).toFixed(2)+'元'+percent('bond')+'　预留现金 '+(a.cash/100).toFixed(2)+'元'+percent('cash');}
    catch(e){result.textContent=e.message;}
  }
  async function refresh(force=false) {
    if(!mount()||busy)return;
    if(options.refreshContext)return options.refreshContext(force);
    render();const now=options.now();if(!force&&attempted&&now-attempted<300000)return;
    attempted=now;busy=true;
    try {const response=await options.fetch(options.cloud?'/api/investment':INVESTMENT_SOURCE,{cache:'no-store',signal:AbortSignal.timeout(12000)});
      if(!response.ok)throw new Error('投资环境源暂不可用');
      const reader=response.body?.getReader();if(!reader)throw new Error('投资环境响应缺失');
      const chunks=[];let size=0;while(true){const part=await reader.read();if(part.done)break;size+=part.value.byteLength;if(size>65536){await reader.cancel();throw new Error('投资环境响应超限');}chunks.push(part.value);}
      const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}const text=new TextDecoder().decode(bytes);
      const next=JSON.parse(text);validateInvestment(next,options.now(),options.calendar);
      if(saved&&(Date.parse(next.built_at)<Date.parse(saved.built_at) || next.bond.observation_date<saved.bond.observation_date || Date.parse(next.macro.computed_at)<Date.parse(saved.macro.computed_at) || Date.parse(next.bond.fetched_at)<Date.parse(saved.bond.fetched_at)))throw new Error('投资环境源版本倒退');
      saved=next;error='';
    }catch{error='更新失败，保留原始日期的历史参考';}finally{busy=false;render();}
  }
  return {refresh,render,mount};
}

// v2独立资产为总览、宏观详情和金额面板提供同一版本；旧v1接口保持原样。
export const INVESTMENT_CONTEXT_SOURCE = 'https://raw.githubusercontent.com/qiliu7060-beep/touzi/close-data/investment-context-v2.json';
export const INVESTMENT_CONTEXT_MAX_BYTES = 65536;
const MACRO_CONTEXT_MODULES = ['pmi', 'ppi', 'fed', 'cpi', 'gdp'];
const MACRO_CONTEXT_LABELS = { pmi: 'PMI（制造业景气）', ppi: 'PPI（工业品价格/盈利周期）', fed: '美联储潮汐（全球流动性）', cpi: 'CPI（通胀速度）', gdp: 'GDP（经济增长）' };
const MACRO_CONTEXT_INPUT_LABELS = {
  pmi_manufacturing: ['制造业PMI', '新浪宏观数据'], pmi_non_manufacturing: ['非制造业PMI', '新浪宏观数据'],
  cpi: ['CPI（居民消费价格）', '新浪宏观数据'], ppi: ['PPI（工业生产者价格）', '新浪宏观数据'],
  gdp: ['GDP（经济增长）', '新浪宏观数据'], cn_bond_10y: ['中国10年期国债收益率', '新浪国债数据'],
  policy_rate: ['美国短期市场利率代理', 'Yahoo市场数据'], us_10y: ['美国10年期国债收益率', 'Yahoo市场数据'],
  dollar: ['美元指数', 'Yahoo市场数据'], balance_sheet: ['美联储总资产', 'FRED经济数据'],
};
const MACRO_CONTEXT_INPUTS = {
  pmi_manufacturing: ['pmi', 'sina:cn_pmi_sina', 'monthly'],
  pmi_non_manufacturing: ['pmi', 'sina:cn_pmi_sina', 'monthly'],
  cpi: ['cpi', 'sina:cn_cpi_sina', 'monthly'],
  ppi: ['ppi', 'sina:cn_ppi_sina', 'monthly'],
  gdp: ['gdp', 'sina:cn_gdp_sina', 'quarterly'],
  cn_bond_10y: ['fed', 'sina:cn_bond_10y', 'daily'],
  policy_rate: ['fed', 'yahoo:^IRX', 'daily'],
  us_10y: ['fed', 'yahoo:^TNX', 'daily'],
  dollar: ['fed', 'yahoo:DX-Y.NYB', 'daily'],
  balance_sheet: ['fed', 'fred:WALCL', 'weekly'],
};
function macroContextKeys(value, names) {
  return value && !Array.isArray(value) && typeof value === 'object'
    && Object.keys(value).sort().join('|') === names.slice().sort().join('|');
}
function macroContextFail() { throw new Error('宏观环境版本、来源或时间未通过核验'); }
function macroContextDate(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(value + 'T12:00:00Z'))
    && new Date(value + 'T12:00:00Z').toISOString().slice(0, 10) === value;
}
function macroContextTime(value, now) {
  // Date.parse会悄悄修正2月30日、24:00等输入，先验证其原始日历与时钟字段。
  const parts = typeof value === 'string' && value.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-](\d{2}):(\d{2}))$/);
  return !!parts && macroContextDate(parts[1]) && Number(parts[2]) < 24 && Number(parts[3]) < 60
    && Number(parts[4] || 0) < 60 && Number(parts[6] || 0) < 24 && Number(parts[7] || 0) < 60
    && Number.isFinite(Date.parse(value)) && Date.parse(value) <= now + 60000;
}
function macroContextText(value, maximum = 1500) {
  return typeof value === 'string' && value.length > 0 && value.length <= maximum && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value);
}
export function investmentContextProjection(value) {
  const macro = value.macro;
  return { schema_version: 1, kind: 'touzi-investment-v1', rule_version: value.rule_version,
    as_of: value.as_of, built_at: value.built_at, trading_calendar: value.trading_calendar,
    macro: { score: macro.score, position: macro.position, computed_at: macro.computed_at, status: macro.status },
    bond: value.bond, errors: value.errors };
}
// 结构/新鲜度检查可在每次渲染时执行；摘要仅在接受网络版本时检查一次。
export function inspectInvestmentContext(value, now, calendar) {
  if (!macroContextKeys(value, ['schema_version','kind','rule_version','formula_version','input_policy','snapshot_id','as_of','built_at','trading_calendar','macro','bond','errors'])
      || value.schema_version !== 2 || value.kind !== 'touzi-investment-v2'
      || value.formula_version !== 'touzi-macro-v1' || value.input_policy !== 'upstream-observed-live'
      || !/^[a-f0-9]{64}$/.test(value.snapshot_id || '')) macroContextFail();
  const macro = value.macro;
  if (!macroContextKeys(macro, ['score','position','computed_at','status','regime','weights','rules','inputs','freshness'])
      || macro.freshness !== 'source-checked' || !macroContextText(macro.regime, 100)
      || !macroContextKeys(macro.weights, MACRO_CONTEXT_MODULES) || !macroContextKeys(macro.rules, MACRO_CONTEXT_MODULES)) macroContextFail();
  const projection = investmentContextProjection(value);
  const checked = validateInvestment(projection, now, calendar);
  if (![value.built_at, macro.computed_at, value.bond.fetched_at].every(stamp => macroContextTime(stamp, now))) macroContextFail();
  let totalWeight = 0, totalScore = 0;
  for (const key of MACRO_CONTEXT_MODULES) {
    const rule = macro.rules[key], weight = macro.weights[key];
    if (!macroContextKeys(rule, ['rule','score','state','explain']) || !macroContextText(rule.rule, 100)
        || !macroContextText(rule.state, 100) || !macroContextText(rule.explain, 5000)
        || !Number.isFinite(rule.score) || rule.score < 0 || rule.score > 100
        || !Number.isFinite(weight) || weight <= 0 || weight > 1) macroContextFail();
    totalWeight += weight; totalScore += rule.score * weight;
  }
  if (Math.abs(totalWeight - 1) > 1e-8 || Math.abs(totalScore - macro.score) > 0.011) macroContextFail();
  if (!macro.inputs || typeof macro.inputs !== 'object' || Array.isArray(macro.inputs)
      || Object.keys(macro.inputs).length > 16) macroContextFail();
  for (const id of ['pmi_manufacturing','cpi','ppi','gdp','cn_bond_10y']) if (!Object.hasOwn(macro.inputs, id)) macroContextFail();
  if (!['policy_rate','us_10y','dollar','balance_sheet'].some(id => Object.hasOwn(macro.inputs, id))) macroContextFail();
  let fresh = checked.fresh;
  for (const [id, input] of Object.entries(macro.inputs)) {
    const expected = MACRO_CONTEXT_INPUTS[id];
    if (!expected || !macroContextKeys(input, ['module','source','frequency','observation_date','value','fetched_at','fetch_status','release_at','source_updated_at','availability_policy'])
        || input.module !== expected[0] || input.source !== expected[1] || input.frequency !== expected[2]
        || input.availability_policy !== 'upstream-observed-live' || input.fetch_status !== 'network'
        || !macroContextDate(input.observation_date) || !Number.isFinite(input.value) || Math.abs(input.value) > 2 ** 53
        || !macroContextTime(input.fetched_at, now) || Date.parse(input.fetched_at) > Date.parse(macro.computed_at)
        || input.observation_date > new Date(Date.parse(input.fetched_at) + 28800000).toISOString().slice(0, 10)) macroContextFail();
    for (const key of ['release_at','source_updated_at']) {
      if (input[key] !== null && (!macroContextTime(input[key], now) || Date.parse(input[key]) > Date.parse(input.fetched_at))) macroContextFail();
    }
    // 核验实际抓取日，而不是用新的计算时间复活旧输入；月度观测期保持其真实频率。
    const fetchedDay = new Date(Date.parse(input.fetched_at) + 28800000).toISOString().slice(0, 10);
    if (fetchedDay < calendar.start || fetchedDay > calendar.end) { fresh = false; continue; }
    // 国债观测日可能晚于另一来源的获取日；只使用交易日年龄，不冒充其网络因果顺序。
    const local = new Date(now + 28800000);
    let end = local.toISOString().slice(0, 10);
    if (local.toISOString().slice(11,16) < '15:00') end = new Date(Date.parse(end+'T12:00:00Z')-86400000).toISOString().slice(0,10);
    let age = 0;
    for (let day = fetchedDay; day <= end; day = new Date(Date.parse(day+'T12:00:00Z')+86400000).toISOString().slice(0,10)) {
      if (day > fetchedDay && ![0,6].includes(new Date(day+'T12:00:00Z').getUTCDay()) && !calendar.closures.some(([a,b])=>day>=a&&day<=b)) age++;
    }
    if (age > 2) fresh = false;
  }
  const bondInput = macro.inputs.cn_bond_10y;
  if (bondInput.observation_date !== value.bond.observation_date || bondInput.fetched_at !== value.bond.fetched_at || bondInput.value !== value.bond.yield_pct) macroContextFail();
  return { value, projection, fresh };
}
// 数字以binary64编码参与摘要，避免Python/JavaScript科学计数法的序列化差异。
export function canonicalInvestmentContext(value) {
  function transform(item) {
    if (typeof item === 'number') {
      if (!Number.isFinite(item) || Math.abs(item) > 2 ** 53) macroContextFail();
      const buffer = new ArrayBuffer(8), view = new DataView(buffer);
      view.setFloat64(0, Object.is(item, -0) ? 0 : item, false);
      return { $number: Array.from(new Uint8Array(buffer), byte => byte.toString(16).padStart(2, '0')).join('') };
    }
    if (Array.isArray(item)) return item.map(transform);
    if (item && typeof item === 'object') return Object.fromEntries(Object.keys(item).sort().map(key => [key, transform(item[key])]));
    return item;
  }
  const unsigned = Object.fromEntries(Object.entries(value).filter(([key]) => key !== 'snapshot_id'));
  return JSON.stringify(transform(unsigned));
}
export async function validateInvestmentContext(value, now, calendar, cryptoProvider = globalThis.crypto) {
  const checked = inspectInvestmentContext(value, now, calendar);
  if (!cryptoProvider?.subtle) macroContextFail();
  const digest = await cryptoProvider.subtle.digest('SHA-256', new TextEncoder().encode(canonicalInvestmentContext(value)));
  const actual = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  if (actual !== value.snapshot_id) macroContextFail();
  return checked;
}
export async function readInvestmentResponse(response) {
  if (!response.ok || Number(response.headers.get('content-length') || 0) > INVESTMENT_CONTEXT_MAX_BYTES) throw new Error('宏观环境来源暂不可用');
  const reader = response.body?.getReader();
  if (!reader) throw new Error('宏观环境响应缺失');
  const chunks = []; let size = 0;
  try {
    for (;;) {
      const item = await reader.read(); if (item.done) break;
      size += item.value.byteLength;
      if (size > INVESTMENT_CONTEXT_MAX_BYTES) throw new Error('宏观环境响应超限');
      chunks.push(item.value);
    }
  } finally { await reader.cancel().catch(() => {}); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
}
function macroContextRollback(previous, next) {
  if (Date.parse(next.built_at) < Date.parse(previous.built_at)
      || Date.parse(next.macro.computed_at) < Date.parse(previous.macro.computed_at)
      || next.bond.observation_date < previous.bond.observation_date
      || Date.parse(next.bond.fetched_at) < Date.parse(previous.bond.fetched_at)) return true;
  for (const [id, old] of Object.entries(previous.macro.inputs)) {
    const input = next.macro.inputs[id];
    if (input && (input.observation_date < old.observation_date || Date.parse(input.fetched_at) < Date.parse(old.fetched_at))) return true;
  }
  return next.built_at === previous.built_at && next.snapshot_id !== previous.snapshot_id;
}
function macroContextElement(doc, tag, className, text) {
  const node = doc.createElement(tag); if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
// 仅改宏观所属节点，保留持仓、金额输入、勾选草稿及details展开状态。
export function createMacroEnvironment(doc, options) {
  const state = { value: null, error: '', loading: false, lastAttempt: null, status: 'waiting' };
  const now = options.now || (() => Date.now());
  const overview = [], rules = new Map(), inputs = new Map();
  let detail = null, detailStatus = null, detailSummary = null, detailSources = null, forcePending = false;
  const panel = createInvestmentPanel(doc, { ...options, now,
    context: () => {
      if (!state.value) return { value: null, error: state.error };
      try {
        const checked = inspectInvestmentContext(state.value, now(), options.calendar);
        return { value: checked.projection, error: state.error || (checked.fresh ? '' : '宏观输入仅作历史参考') };
      } catch { return { value: investmentContextProjection(state.value), error: state.error || '历史宏观版本已超出可核验范围' }; }
    }, refreshContext: force => refresh(force) });
  function bind() {
    if (!overview.length && doc.querySelectorAll) {
      for (const card of doc.querySelectorAll('main .grid5 .card')) {
        const label = card.querySelector('.kv b');
        const key = card.getAttribute('data-macro-card') || ({ '宏观背景分': 'score', '目标仓位': 'position', '决策时点': 'computed_at', '宏观计算时间': 'computed_at' })[label?.textContent.trim()];
        if (!['score','position','computed_at'].includes(key)) continue;
        const value = card.querySelector('[data-macro-value="' + key + '"]') || card.querySelector('.big');
        const note = card.querySelector('[data-macro-note="' + key + '"]') || card.querySelector('.kv span');
        if (value) { card.setAttribute('data-macro-card', key); overview.push({ key, card, label, value, note }); }
      }
    }
    const path = options.path || doc.location?.pathname || '';
    // 旧首页仍可能带有“宏观不能更新”的生成时说明，仅接管精确标题下的对应段落。
    if (/(?:^|\/)(?:index(?:\.html)?)?\/?$/.test(path) && doc.querySelectorAll) {
      for (const heading of doc.querySelectorAll('main .card h3')) {
        if (heading.textContent.trim() !== '数据新鲜度') continue;
        const card = heading.closest('.card');
        for (const paragraph of card?.querySelectorAll('p') || []) {
          if (paragraph.hasAttribute('data-macro-freshness-copy') || !paragraph.textContent.includes('宏观')) continue;
          const hadLink = !!paragraph.querySelector('a');
          paragraph.textContent = '页面可见时每五分钟检查统一宏观源，总分、仓位与分项使用同一版本。PMI、CPI、PPI、GDP按各自公布周期变化；新数据发布后可重新评分，观测期、实际抓取与计算时间分别显示。历史报告保留原日期。';
          paragraph.setAttribute('data-macro-freshness-copy', 'true');
          if (hadLink) { const link = macroContextElement(doc, 'a', '', '查看数据新鲜度'); link.setAttribute('href', 'data.html'); paragraph.appendChild(link); }
        }
      }
    }
    const isDataPage = /(?:^|\/)data(?:\.html)?\/?$/.test(path);
    if (!detail && (isDataPage || /(?:^|\/)macro(?:\.html)?\/?$/.test(path))) {
      let root = doc.querySelector('[data-macro-current]') || doc.getElementById('sec-macro');
      if (isDataPage) {
        const main = doc.querySelector('main');
        if (main) {
          root = macroContextElement(doc, 'section', ''); root.setAttribute('data-macro-current', 'true'); main.insertBefore(root, main.firstChild);
          const old = doc.getElementById('sec-freshness');
          if (old) { const note = macroContextElement(doc, 'p', 'small muted', '以下原始新鲜度表为历史发布快照；当前宏观输入见上方独立版本与实际获取时间。'); old.insertBefore(note, old.firstChild); }
        }
      }
      if (root && !root.hasAttribute('data-macro-history') && !root.closest('[data-macro-history]')) {
        for (const node of Array.from(root.children || [])) node.hidden = true;
        detail = macroContextElement(doc, 'div', 'card'); detail.setAttribute('data-macro-live-details', 'true');
        detail.appendChild(macroContextElement(doc, 'h3', '', '当前宏观评分与输入来源'));
        detailStatus = macroContextElement(doc, 'p', 'small muted'); detailStatus.setAttribute('role', 'status'); detail.appendChild(detailStatus);
        detailSummary = macroContextElement(doc, 'p', 'big'); detail.appendChild(detailSummary);
        const grid = macroContextElement(doc, 'div', 'grid grid5'); grid.hidden = isDataPage; detail.appendChild(grid);
        for (const key of MACRO_CONTEXT_MODULES) {
          const card = macroContextElement(doc, 'section', 'card'); card.setAttribute('data-macro-rule', key);
          card.appendChild(macroContextElement(doc, 'h4', '', MACRO_CONTEXT_LABELS[key]));
          const score = macroContextElement(doc, 'p', 'big'); card.appendChild(score);
          const weight = macroContextElement(doc, 'p', 'small muted'); card.appendChild(weight);
          const status = macroContextElement(doc, 'p', 'small'); card.appendChild(status);
          const explain = macroContextElement(doc, 'p', 'small muted'); card.appendChild(explain);
          grid.appendChild(card); rules.set(key, { score, weight, status, explain });
        }
        const sourceBox = macroContextElement(doc, 'details', ''); sourceBox.setAttribute('data-macro-sources', 'true');
        sourceBox.appendChild(macroContextElement(doc, 'summary', '', '逐项输入：观测期、获取时间与披露时间'));
        detailSources = macroContextElement(doc, 'div', 'small'); sourceBox.appendChild(detailSources); detail.appendChild(sourceBox);
        detail.appendChild(macroContextElement(doc, 'p', 'small muted', '源已检查表示本次取得来源实际数据，不保证来源已包含全部官方最新发布。未知披露时间保留未知。下方历史检验、政策与下载保留各自原始日期。'));
        root.appendChild(detail);
      }
    }
  }
  function render() {
    bind(); panel.mount();
    let checked = null;
    if (state.value) {
      try { checked = inspectInvestmentContext(state.value, now(), options.calendar); }
      catch { state.error = '历史宏观版本已超出可核验范围'; }
    }
    const value = state.value, macro = value?.macro, history = !!(state.error || checked && !checked.fresh);
    state.status = !value ? state.error ? 'unavailable' : 'waiting' : history ? 'historical' : 'ok';
    const statusText = !value ? state.error || '宏观环境待核验，等待新版本'
      : (history ? '历史参考，暂停当前自动建议' : '源已检查') + ' · 宏观计算 ' + macro.computed_at
        + (state.error ? ' · ' + state.error : '');
    for (const item of overview) {
      if (item.key === 'computed_at' && item.label) item.label.textContent = '宏观计算时间';
      item.value.textContent = !value ? '待核验' : item.key === 'score' ? macro.score.toFixed(2)
        : item.key === 'position' ? (macro.position * 100).toFixed(1) + '%' : macro.computed_at;
      if (item.note) item.note.textContent = statusText + (item.key === 'score' && macro ? ' · ' + macro.regime : '');
      item.card.setAttribute('data-macro-status', state.status);
      if (value) item.card.setAttribute('data-macro-snapshot', value.snapshot_id);
    }
    if (detail) {
      detailStatus.textContent = statusText;
      detailSummary.textContent = !value ? '待核验' : macro.score.toFixed(2) + ' / 100 · 目标仓位 ' + (macro.position * 100).toFixed(1) + '% · ' + macro.regime;
      detail.setAttribute('data-macro-status', state.status);
      if (value) detail.setAttribute('data-macro-snapshot', value.snapshot_id);
      for (const [key, row] of rules) {
        const rule = macro?.rules[key];
        row.score.textContent = rule ? rule.score.toFixed(2) + ' / 100' : '待核验';
        row.weight.textContent = rule ? '权重 ' + (macro.weights[key] * 100).toFixed(1) + '%' : '';
        row.status.textContent = rule?.state || '等待完整宏观版本';
        row.explain.textContent = rule?.explain || '';
      }
      for (const [id, node] of inputs) node.hidden = !macro || !Object.hasOwn(macro.inputs, id);
      if (macro) for (const [id, input] of Object.entries(macro.inputs)) {
        let node = inputs.get(id);
        if (!node) { node = macroContextElement(doc, 'p', 'small muted'); node.setAttribute('data-macro-input', id); detailSources.appendChild(node); inputs.set(id, node); }
        node.hidden = false;
        const label = MACRO_CONTEXT_INPUT_LABELS[id]; node.setAttribute('data-macro-source', input.source);
        node.textContent = label[0] + ' · 来源 ' + label[1] + ' · 观测期 ' + input.observation_date + ' · 值 ' + input.value
          + ' · 实际获取 ' + input.fetched_at + ' · 官方披露 ' + (input.release_at || '未知')
          + ' · 来源更新时间 ' + (input.source_updated_at || '未知')
          + (id === 'policy_rate' ? ' · ^IRX为市场利率代理，并非官方联邦基金利率' : '');
      }
    }
    panel.render();
    if (options.onChange) options.onChange(state);
  }
  async function refresh(force = false) {
    render();
    if (doc.hidden) return false;
    if (state.loading) { if (force) forcePending = true; return false; }
    const clock = now();
    if (!force && state.lastAttempt !== null && clock >= state.lastAttempt && clock - state.lastAttempt < 300000) return false;
    state.lastAttempt = clock; state.loading = true; forcePending = false;
    try {
      const response = await options.fetch(options.cloud ? '/api/investment/context' : INVESTMENT_CONTEXT_SOURCE,
        { cache: 'no-store', redirect: 'manual', signal: AbortSignal.timeout(12000) });
      const next = await readInvestmentResponse(response);
      await validateInvestmentContext(next, now(), options.calendar, options.crypto || globalThis.crypto);
      if (state.value && macroContextRollback(state.value, next)) throw new Error('宏观环境版本倒退');
      state.value = JSON.parse(JSON.stringify(next)); state.error = ''; return true;
    } catch {
      state.error = '更新失败，保留原始日期的历史参考'; return false;
    } finally {
      state.loading = false; render();
      if (forcePending && !doc.hidden) { forcePending = false; state.lastAttempt = null; }
    }
  }
  function invalidate() { state.lastAttempt = null; if (state.loading) forcePending = true; }
  render();
  return { state, refresh, render, invalidate };
}
