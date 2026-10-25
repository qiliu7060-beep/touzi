import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {browserTestRuntimes} from './browser-test-runtime.mjs';
const tick = () => new Promise(resolve => setImmediate(resolve));
for (const {file, cloud} of browserTestRuntimes(import.meta.url)) {
  let fail = false, pending = null, writes = 0;
  const elements = {}, toasts = [];
  function node(tag = 'div') { return {tagName:tag, value:'', children:[], innerHTML:'', style:{}, disabled:false,
    setAttribute(){}, appendChild(n){this.children.push(n);}, remove(){this.removed=true;}, focus(){this.focused=true;},
    showModal(){this.open=true;}, addEventListener(){},
    querySelectorAll(){return this.children.flatMap(c => [c,...c.children]);}}; }
  for (const k of ['f_code','f_name','f_date','f_price','f_shares','f_note','f_msg','f_import','__ledger']) elements[k]=node();
  const body=node(), window={__SITE__:{}};
  const ctx={window,module:{exports:{}},AbortSignal:{timeout(ms){assert.equal(ms,15000);return AbortSignal.timeout(ms);}},document:{readyState:'loading',addEventListener(){},body,
    getElementById(id){return elements[id]||null;},createElement:node},
    fetch(){writes++;return new Promise(resolve=>{pending=()=>fail?resolve({ok:false,status:400}):resolve({ok:true});});},
    localStorage:{setItem(){writes++;if(fail)throw new Error('quota');}},confirm(){return true;}};
  let source=readFileSync(file,'utf8');
  source=source.replace(/\}\)\(\);\s*$/, `window.test = {set(rows){LEDGER=rows;${cloud?'AUTH_USER={id:"test"};':''}}, get(){return LEDGER;}, render:renderLedger, load:loadLedger};})();`);
  runInNewContext(source,ctx);
  window.__renderAll=()=>{};window.__toast=text=>toasts.push(text);
  const a={'代码':'000708','名称':'虚拟甲','买入日期':'2026-01-01','买入价':'10','股数':'100','备注':''};
  const b={...a,'买入价':'12'};window.test.set([a,b]);
  const settle=async()=>{if(cloud)pending();await tick();};
  window.test.render();
  // 使用 HTML 解析后实际存在的 onclick 属性，不直接绕过按钮调用删除。
  const handlers=[...elements.__ledger.innerHTML.matchAll(/onclick="([^"]*)"/g)].map(m=>m[1]);
  handlers.forEach(code=>assert.doesNotThrow(()=>new Function(code)));
  runInNewContext(handlers[0],{__holdEdit:window.__holdEdit});
  let dialog=body.children.at(-1);assert.equal(dialog.open,true);
  const inputs=dialog.children.filter(c=>c.children.length).map(c=>c.children[0]);
  inputs[0].value='000001';inputs[2].value='2026-02-01';inputs[3].value='11';
  const submit=dialog.children.at(-1);submit.onclick();submit.onclick();
  // 所有变更入口共享锁；导入、清空、新增和选股加入不能并发覆盖提交。
  window.__holdSave();window.__holdImport();window.__holdWipe();window.__pickAdd();
  assert.equal(writes,1);assert.equal(window.test.get()[0]['代码'],'000708');
  await settle();assert.equal(window.test.get().length,2);assert.equal(window.test.get()[0]['代码'],'000001');
  assert.equal(JSON.stringify(window.test.get()[1]),JSON.stringify(b));
  assert.equal(elements.f_code.focused,true,'表格重绘移除原按钮后，焦点回到新增持仓代码输入框');
  // 取消删除不发起请求。
  window.__holdDel(0);dialog=body.children.at(-1);dialog.children.at(-2).onclick();assert.equal(writes,1);
  // 拒绝/断网保留原记录和弹窗，重试只在确认成功后移除。
  fail=true;window.__holdDel(0);dialog=body.children.at(-1);dialog.children.at(-1).onclick();await settle();
  assert.equal(window.test.get().length,2);assert.equal(dialog.removed,undefined);assert.match(toasts.at(-1),/保存失败/);
  fail=false;dialog.children.at(-1).onclick();await settle();assert.equal(window.test.get().length,1);
  // 打开后其他入口改变台账，过期索引不能覆盖剩余记录。
  window.__holdEdit(0);dialog=body.children.at(-1);window.test.set([{...b,'备注':'变化'}]);
  const before=writes;dialog.children.at(-1).onclick();assert.equal(writes,before);
  assert.match(dialog.children.at(-3).textContent,/发生变化/);
  // 写入成功但渲染异常时不可显示失败，也不能阻止后续正常写入。
  dialog.children.at(-2).onclick();window.test.set([a,b]);
  window.__renderAll=()=>{throw new Error('render failed');};
  window.__holdDel(0);dialog=body.children.at(-1);dialog.children.at(-1).onclick();await settle();
  assert.equal(window.test.get().length,1);assert.equal(dialog.removed,true);
  assert.match(toasts.at(-1),/持仓已保存.*页面更新失败/);
  window.__renderAll=()=>{};
  window.__holdDel(0);dialog=body.children.at(-1);dialog.children.at(-1).onclick();await settle();
  assert.equal(window.test.get().length,0,'渲染异常不会被误判成云端写入结果未知');
  if (cloud) {
    window.test.set([a,b]);window.__holdDel(0);dialog=body.children.at(-1);
    ctx.fetch=()=>{writes++;return Promise.reject(new Error('offline'));};
    dialog.children.at(-1).onclick();await tick();assert.match(toasts.at(-1),/云端结果未确认/);
    const count=writes;dialog.children.at(-1).onclick();await tick();assert.equal(writes,count);
  } else {
    window.__SITE__.seed_holdings=[a];fail=true;window.test.load();await tick();
    assert.match(toasts.at(-1),/初始持仓未能保存到本地/,'初始持仓存储配额错误有反馈且不产生未处理拒绝');
  }
  console.log(file+': edit replacement, cancel, stale guard, parsed handlers, failure/retry and duplicate submission passed');
}
