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
    const status=doc.getElementById('__investment-status'),result=doc.getElementById('__investment-result'),reason=doc.getElementById('__investment-reason');
    if(!saved){status.textContent=error||'投资环境待更新；暂不生成自动投入建议。';result.textContent='股票：待核验　债基：待核验　预留现金：待计算';return;}
    let checked;try{checked=validateInvestment(saved,options.now(),options.calendar);}catch{status.textContent='历史数据待核验，暂停当前自动建议';result.textContent='股票：待核验　债基：待核验　现金：待计算';return;}
    const m=saved.macro,b=saved.bond,history=error||!checked.fresh;
    status.textContent=(history?'历史参考，暂停当前自动建议':'当前分配参考')+' · 宏观计算 '+m.computed_at+' · 国债观测 '+b.observation_date+' · 抓取 '+b.fetched_at+(error?' · '+error:'');
    const stage={1:'正常',0.75:'谨慎',0.5:'防守',0:'暂停'};
    reason.textContent='股票建议 '+(m.position*100).toFixed(1)+'%'+(m.score<=30?'，宏观分≤30，暂停新增股票投入':'')+'；国债10年收益率 '+b.yield_pct.toFixed(4)+'%，近20/60交易日 '+b.delta20_bp.toFixed(2)+' / '+b.delta60_bp.toFixed(2)+'基点；债基'+stage[b.factor]+'（投入系数 '+b.factor*100+'%）。收益率下降通常利好已有债券价格，不能等同基金未来回报。';
    const text=doc.getElementById('__investment-amount').value;
    if(!text){result.textContent='输入总额后计算股票、债基和预留现金';return;}
    try {const a=investmentAllocation(text,m.position,m.score,b.factor);const total=a.stock+a.bond+a.cash;const percent=k=>total?'（'+(a[k]/total*100).toFixed(2)+'%）':'（0.00%）';result.textContent=(history?'历史示例：':'')+'股票 '+(a.stock/100).toFixed(2)+'元'+percent('stock')+'　债基 '+(a.bond/100).toFixed(2)+'元'+percent('bond')+'　预留现金 '+(a.cash/100).toFixed(2)+'元'+percent('cash');}
    catch(e){result.textContent=e.message;}
  }
  async function refresh(force=false) {
    if(!mount()||busy)return;
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
  return {refresh,render};
}
