const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const lines = fs.readFileSync(path.join(__dirname,'../fleet_manager/static/index.html'),'utf8').split('\n');
const wanted = ['function lastBulkResult(', 'function bulkIsActive(', 'function bulkOutcomeHtml(', 'let bulkPollInFlight=', 'async function pollBulkJob(', 'function summaryText('];
const code = wanted.map(start => lines.find(line=>line.startsWith(start))).join('\n');
function fixture(api) {
  const storage = new Map(); const timers=[]; let loaded=0, rendered=0;
  const ctx=vm.createContext({data:{user:{id:1},updates:[],instances:[]},FleetRefresh:require('../fleet_manager/static/fleet-refresh.js'),sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},api,
    activePage:'Updates',load:async()=>{loaded++},updates:()=>{rendered++},renderOperationDock:()=>{},
    setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length},clearTimeout:()=>{},esc:x=>String(x)});
  vm.runInContext(code,ctx);
  const save=x=>storage.set('hafm-last-bulk-result',JSON.stringify(x));
  const read=()=>JSON.parse(storage.get('hafm-last-bulk-result'));
  return {ctx,save,read,timers,loaded:()=>loaded,rendered:()=>rendered};
}
test('runtime coalesces concurrent polls and acceptance remains unverified',async()=>{
  let resolve,calls=0;const f=fixture(async p=>{assert.equal(p,'/api/jobs');calls++;return new Promise(r=>resolve=r)});
  f.save({job_id:3,status:'queued',action:'update',queued:2,updated:0});
  const first=f.ctx.pollBulkJob();await f.ctx.pollBulkJob();assert.equal(calls,1);
  resolve([{id:3,status:'running',details:{action:'update',queued:1,running:1,pending:2,updated:0,results:[]}}]);
  await first;assert.equal(f.read().updated,0);assert.equal(f.read().status,'running');
  assert.match(f.ctx.summaryText(f.read()),/not yet verified/);assert.equal(f.timers.length,1);assert.equal(f.timers[0].delay,3000);
});
test('runtime terminal verification replaces stale snapshot, refreshes rows, and stops polling',async()=>{
  const f=fixture(async()=>[{id:3,status:'succeeded',details:{action:'update',selected:2,updated:2,pending:0,results:[]}}]);
  f.save({job_id:3,status:'running',action:'update',updated:0});await f.ctx.pollBulkJob();
  assert.equal(f.read().updated,2);assert.equal(f.read().status,'succeeded');assert.equal(f.loaded(),1);assert.equal(f.timers.length,0);
  assert.match(f.ctx.summaryText(f.read()),/Verified 2 updates/);
});
test('runtime failed progress read preserves prior outcomes and explicitly marks stale view',async()=>{
  const f=fixture(async()=>{throw Error('synthetic unavailable')});f.save({job_id:3,status:'running',action:'update',updated:1,results:[{update:4,status:'succeeded'}]});
  await f.ctx.pollBulkJob();assert.equal(f.read().updated,1);assert.equal(f.read().results[0].status,'succeeded');
  assert.match(f.read().progress_error,/showing last saved result/);assert.match(f.ctx.bulkOutcomeHtml(),/role="status"/);assert.equal(f.timers.length,1);
});
test('runtime late response cannot replace a newer selected bulk job',async()=>{
  let resolve;const f=fixture(async()=>new Promise(r=>resolve=r));f.save({job_id:3,status:'running',action:'update'});
  const first=f.ctx.pollBulkJob();f.save({job_id:4,status:'queued',action:'skip',queued:2});
  resolve([{id:3,status:'succeeded',details:{action:'update',updated:13}}]);await first;
  assert.equal(f.read().job_id,4);assert.equal(f.read().action,'skip');assert.equal(f.read().status,'queued');assert.equal(f.timers.length,1);
});
