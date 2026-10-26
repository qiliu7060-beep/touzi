import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { browserTestRuntimes } from './browser-test-runtime.mjs';

// 仅虚拟企业样本；不连接账户、持仓或公开行情源。
const now = Date.parse('2026-10-23T10:00:00+08:00');
function sample(debt = .799999, capex = .249999) {
  return {name:'虚拟企业',industry:'制造业',financial_capability:true,action:'建仓',total:88,pe:10,pb:1,dy:3,
    price:10,quote_ts:'2026-10-23T10:00:00+08:00',__live_model:'touzi-live-v2',
    financial_analysis:{status:'ok',fetched_at:'2026-10-23T09:00:00+08:00',window:{start_year:2021,end_year:2025},
      years:[],metrics:{debt_equity:{value:debt,status:'ok',excellent:debt<.8,report_date:'2026-06-30'},
        capex_to_profit:{value:capex,status:'ok',excellent:capex<.25}}}};
}

class TextNode {
  constructor(value) {this.nodeType=3;this.nodeValue=value;}
  get textContent(){return this.nodeValue;}
}
class Element {
  constructor(tag, attrs={}) {this.nodeType=1;this.tagName=tag.toUpperCase();this.attrs={...attrs};this.childNodes=[];this.style={};this._html='';}
  get children(){return this.childNodes.filter(x=>x.nodeType===1);}
  get textContent(){return this._html?this._html.replace(/<[^>]*>/g,''):this.childNodes.map(x=>x.textContent).join('');}
  set textContent(value){this._html='';this.childNodes=[new TextNode(String(value))];}
  get innerHTML(){return this._html||this.textContent;}
  set innerHTML(value){this._html=String(value);this.childNodes=[];}
  setAttribute(key,value){this.attrs[key]=String(value);}
  getAttribute(key){return Object.hasOwn(this.attrs,key)?this.attrs[key]:null;}
  hasAttribute(key){return Object.hasOwn(this.attrs,key);}
  appendChild(node){node.parentNode=this;this.childNodes.push(node);return node;}
  insertBefore(node,before){node.parentNode=this;const index=this.childNodes.indexOf(before);if(index<0)this.childNodes.push(node);else this.childNodes.splice(index,0,node);return node;}
  matches(selector){
    if(selector.startsWith('.'))return String(this.className||this.attrs.class||'').split(' ').includes(selector.slice(1));
    const match=selector.match(/^(\w+)?(?:\[([^=\]]+)(?:="([^"]*)")?\])?$/);
    return !!match&&(!match[1]||this.tagName===match[1].toUpperCase())&&(!match[2]||(this.hasAttribute(match[2])&&(match[3]===undefined||this.getAttribute(match[2])===match[3])));
  }
  querySelectorAll(selector){return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)]);}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  closest(selector){return this.matches(selector)?this:this.parentNode?this.parentNode.closest(selector):null;}
}
function harness(file,cloud,modern) {
  const calendar=JSON.parse(readFileSync(file,'utf8').match(/const MARKET_CALENDAR = (.*);/)[1]);
  const entries={'000001':sample(),'000002':sample(.8,.25),'000003':sample()};
  entries['000002'].action='接近建仓';entries['000003'].action='观望';entries['000003'].industry='';
  entries['000003'].financial_analysis.metrics.debt_equity.status='missing';
  const head=new Element('head'),body=new Element('main'), rows=[], saved=[];
  for(const code of Object.keys(entries)) {
    const card=body.appendChild(new Element('section',{class:'card'}));
    const scroll=card.appendChild(new Element('div',{class:'scroll'}));
    const table=scroll.appendChild(new Element('table'));
    const header=table.appendChild(new Element('tr'));
    for(const name of ['选','代码','名称','动作','总分','现价','PE','PB','股息率%','还差什么','买入价','股数']) {const cell=header.appendChild(new Element('th'));cell.textContent=name;}
    const row=table.appendChild(new Element('tr',{'data-pick':code}));rows.push(row);
    for(let i=0;i<12;i++){const cell=row.appendChild(new Element('td'));cell.textContent=String(i);}
    const cells=row.querySelectorAll('td');cells[3].textContent=entries[code].action;
    const checkbox=new Element('input');checkbox.checked=true;cells[0].appendChild(checkbox);
    cells[9].textContent='(全部通过) ';const warning=cells[9].appendChild(new Element('span'));warning.textContent='原有预警';
    const price=cells[10].appendChild(new Element('input'));price.className='ip';price.value='77.77';
    const shares=cells[11].appendChild(new Element('input'));shares.className='is';shares.value='567';
    saved.push({row,checkbox,price,shares,warning,cells,header,card});
    if(modern)for(const key of ['debt_equity','capex_to_profit']){
      header.insertBefore(new Element('th',{'data-pick-financial-header':key}),header.children[9+(key==='capex_to_profit'?1:0)]);
      row.insertBefore(new Element('td',{'data-pick-financial':key}),cells[9]);
    }
  }
  let current=now;
  class Clock extends Date {static now(){return current;}}
  const box={exports:{}},doc={head,readyState:'loading',addEventListener(){},hidden:false,
    querySelectorAll(selector){return selector==='tr[data-pick]'?rows:[];},querySelector(selector){return head.querySelector(selector);},getElementById(){return null;},createElement(tag){return new Element(tag);}};
  const context={module:box,window:{__SITE__:{entries,trading_calendar:calendar,built_at:new Date(now).toISOString(),as_of:'2026-10-23',quote_at:'2026-10-23 10:00',quote_coverage:1}},document:doc,Date:Clock};
  runInNewContext(readFileSync(file,'utf8'),context);
  const api=box.exports;
  if(cloud)Object.assign(api.monitorState,{attempted:true,status:'ok',error:'',computed:new Date(now).toISOString(),model:'touzi-live-v2',
    built:new Date(now).toISOString(),baseQuote:'2026-10-23T10:00:00+08:00',expiry:new Date(now+36*3600000).toISOString()});
  return {api,entries,saved,head,setNow(value){current=value;}};
}

// 选中态正文及辅助信息均需保持可读，避免深绿底与深色文字再次组合。
function contrast(first,second) {
  function luminance(hex) {
    return hex.match(/\w\w/g).map(part=>parseInt(part,16)/255)
      .map(value=>value<=.04045?value/12.92:((value+.055)/1.055)**2.4)
      .reduce((total,value,index)=>total+value*[.2126,.7152,.0722][index],0);
  }
  const values=[luminance(first),luminance(second)].sort((a,b)=>b-a);
  return (values[0]+.05)/(values[1]+.05);
}

for(const {file,cloud} of browserTestRuntimes(import.meta.url)) {
  const pure=harness(file,cloud,false).api;
  let view=pure.pickFinancialMetric(sample(),'debt_equity','verified');
  assert.equal(view.value,'0.80 倍');assert.equal(view.status,'优良');assert.match(view.reason,/原始值严格低于0.8/);
  view=pure.pickFinancialMetric(sample(),'capex_to_profit','verified');assert.equal(view.value,'25.00%');assert.equal(view.status,'达标');
  assert.equal(pure.pickFinancialMetric(sample(.8,.25),'debt_equity','verified').status,'未达优良标准');
  assert.equal(pure.pickFinancialMetric(sample(.8,.25),'capex_to_profit','verified').status,'未达标');
  assert.equal(pure.pickFinancialMetric(sample(0,0),'capex_to_profit','verified').value,'0.00%');
  const exempt=sample(9.69,.08);exempt.financial_analysis.metrics.debt_equity.status='not_applicable';
  assert.equal(pure.pickFinancialMetric(exempt,'debt_equity','verified').status,'行业不适用');
  const unknown=sample();unknown.industry='';unknown.financial_analysis.metrics.debt_equity.status='missing';
  assert.equal(pure.pickFinancialMetric(unknown,'debt_equity','verified').status,'行业待核验');
  assert.equal(pure.pickFinancialMetric(unknown,'debt_equity','verified').qualifier,'本轮数据来源已核验');
  const invalid=sample();Object.assign(invalid.financial_analysis.metrics.capex_to_profit,{value:null,status:'invalid',net_profit_sum:0});
  assert.match(pure.pickFinancialMetric(invalid,'capex_to_profit','verified').reason,/累计净利润非正/);
  Object.assign(invalid.financial_analysis.metrics.debt_equity,{value:null,status:'invalid',total_equity:0});
  assert.match(pure.pickFinancialMetric(invalid,'debt_equity','verified').reason,/股东权益非正/);
  invalid.financial_analysis.metrics.debt_equity.status='stale';assert.equal(pure.pickFinancialMetric(invalid,'debt_equity','verified').status,'旧数据');
  const old=sample();old.financial_capability=false;assert.equal(pure.pickFinancialMetric(old,'debt_equity','verified').value,'—');
  assert.equal(pure.pickFinancialMetric({},'debt_equity','verified').value,'—');
  assert.equal(pure.pickFinancialMetric(sample(),'debt_equity','pending').status,'待核验');
  assert.equal(pure.pickFinancialMetric(sample(),'debt_equity','snapshot').qualifier,'发布快照');
  for(const modern of [false,true]) {
    const h=harness(file,cloud,modern);
    h.api.renderPickFinancials();h.api.renderPickFinancials();
    const styles=h.head.querySelectorAll('style[data-pick-selection-style]');
    assert.equal(styles.length,1);
    const selected=styles[0].textContent.match(/tr\[data-pick\]\.picked\{background:#([\da-f]{6});color:#([\da-f]{6})\}/);
    const muted=styles[0].textContent.match(/\.muted\{color:#([\da-f]{6})\}/);
    assert.ok(selected&&muted,'旧HTML兼容选中样式必须包含背景、正文和辅助文字颜色');
    assert.ok(contrast(selected[1],selected[2])>=4.5,'选中行正文对比不足');
    assert.ok(contrast(selected[1],muted[1])>=4.5,'选中行辅助信息对比不足');
    for(const item of h.saved) {
      assert.equal(item.header.querySelectorAll('th').length,14);assert.equal(item.row.querySelectorAll('td').length,14);
      assert.equal(item.card.querySelectorAll('[data-pick-financial-note]').length,1);
      assert.match(item.cells[9].textContent,/原买入门槛通过/);assert.equal(item.cells[9].children[0],item.warning);
      assert.equal(item.row.children[0].children[0],item.checkbox);assert.equal(item.checkbox.checked,true);
      assert.equal(item.row.children[12].children[0],item.price);assert.equal(item.row.children[13].children[0],item.shares);
      assert.equal(item.price.value,'77.77');assert.equal(item.shares.value,'567');
      assert.equal(item.row.children[3].textContent,h.entries[item.row.getAttribute('data-pick')].action);
    }
    assert.match(h.saved[0].row.querySelector('[data-pick-financial="debt_equity"]').innerHTML,/<b>优良<\/b>/);
    assert.match(h.saved[1].row.querySelector('[data-pick-financial="capex_to_profit"]').innerHTML,/未达标/);
    assert.match(h.saved[2].row.querySelector('[data-pick-financial="debt_equity"]').innerHTML,/行业待核验/);
    if(cloud){h.api.renderScores();assert.equal(h.saved[0].row.children[4].textContent,'88.00');assert.equal(h.saved[0].row.children[6].textContent,'10.00');}
    h.setNow(Date.parse('2027-01-01T10:00:00+08:00'));h.api.renderPickFinancials();
    assert.match(h.saved[0].row.querySelector('[data-pick-financial="debt_equity"]').innerHTML,/<b>待核验<\/b>/);
    assert.doesNotMatch(h.saved[0].row.querySelector('[data-pick-financial="debt_equity"]').innerHTML,/<b>优良<\/b>/);
  }
  console.log(`${file}: pick columns, strict raw precision/status, old/new HTML, repeat updates and original input-node preservation passed`);
}
