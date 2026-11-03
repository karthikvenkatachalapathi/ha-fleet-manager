const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const F=require('../fleet_manager/static/fleet-refresh.js');
const html=fs.readFileSync(require('node:path').join(__dirname,'../fleet_manager/static/index.html'),'utf8');
const source=name=>html.split('\n').find(x=>x.startsWith('function '+name+'(')||x.startsWith('async function '+name+'('));
const tick=()=>new Promise(r=>setImmediate(r));
test('UTC naive backend timestamps never become six hours in the future',()=>{
 assert.equal(F.parseTime('2026-11-02T23:37:00'),Date.parse('2026-11-02T23:37:00Z'));
 assert.equal(F.parseTime('2026-11-02 23:37:00.000000'),Date.parse('2026-11-02T23:37:00Z'));
 assert.equal(F.parseTime('2026-11-02T17:37:00-06:00'),Date.parse('2026-11-02T23:37:00Z'));
 assert.ok(Number.isNaN(F.parseTime('not a date')));
});
test('HTTP read success is not fresh HA evidence; future/missing/error metadata are explicit',()=>{
 const time=Date.parse('2026-11-02T23:40:00Z');
 assert.equal(F.snapshotHealth(null,[{id:1,last_update_scan:'2026-11-02T22:40:00'}],time).phase,'stale');
 assert.equal(F.snapshotHealth({instances:[{instance_id:1,last_checked_at:'2026-11-02T23:39:50Z'}]},[],time).phase,'current');
 assert.equal(F.snapshotHealth({instances:[{instance_id:1,last_checked_at:'2026-11-03T00:00:00Z'}]},[],time).phase,'stale');
 assert.equal(F.snapshotHealth({instances:[{instance_id:1,refreshing:true}]},[],time).phase,'refreshing');
 assert.equal(F.snapshotHealth({instances:[{instance_id:1,error:'TimeoutError'}]},[],time).phase,'failed');
 assert.equal(F.snapshotHealth(null,[{id:1}],time).phase,'unknown');
});
test('batch summary separates verified, installed restart-needed, and genuinely waiting work',()=>{
 const j={updated:1,action_required:1,queued:9,pending:9,results:Array.from({length:9},()=>({status:'waiting_verification',reason:'waiting_for_restart:138'}))};
 const text=F.describeBulk(j);assert.match(text,/Verified 1 update/);assert.match(text,/restart required: 1/);assert.match(text,/Waiting for restart: 9/);assert.doesNotMatch(text,/installing|complete/i);
 const view=F.bulkItemView({update:70,instance:2,status:'succeeded'},[{id:70,component:'Music Assistant'}],[{id:2,friendly_name:'Richmond Home'}]);assert.equal(view.name,'Music Assistant');assert.equal(view.home,'Richmond Home');assert.equal(view.status,'Verified updated');
});
test('cached-status read coalesces repeated requests without a sync mutation',async()=>{
 const calls=[];let release;const c=F.createFleetRefreshController({paths:['/api/updates?refresh=1'],keys:['updates'],fetchJson:async(path,options)=>{calls.push({path,options});return new Promise(r=>release=r)}});
 const first=c.observeRefresh();assert.equal(c.observeRefresh(),false);await tick();assert.equal(calls.length,1);assert.equal(calls[0].options,undefined);release([]);await first;assert.equal(c.getState().manualInFlight,false);assert.ok(calls.every(x=>x.path!='/api/sync-all'));
});
test('slow operation history never blocks already loaded updates',async()=>{
 const c=F.createFleetRefreshController({paths:['updates','operations'],keys:['updates','operations'],nonBlockingKeys:['operations'],fetchJson:async p=>p==='updates'?[{id:1}]:new Promise(()=>{})});
 await c.refresh();assert.equal(c.getState().updates.length,1);assert.deepEqual(c.getState().loadedKeys,['updates']);
});
test('refresh-in-progress metadata schedules one bounded readback to get observed results',async()=>{
 const timers=[];let n=0;
 const c=F.createFleetRefreshController({paths:['updates'],keys:['updates'],setTimeoutFn:(fn,ms)=>{timers.push({fn,ms});return timers.length},fetchJson:async()=>{n++;let a=[{id:n}];Object.defineProperty(a,'snapshotMeta',{value:{instances:[{instance_id:1,refreshing:n===1,last_checked_at:new Date().toISOString()}]}});return a}});
 await c.refresh();assert.equal(c.getState().snapshotMeta.instances[0].refreshing,true);const follow=timers.filter(x=>x.ms===1500);assert.equal(follow.length,1);follow[0].fn();await tick();assert.equal(c.getState().updates[0].id,2);assert.equal(c.getState().snapshotMeta.instances[0].refreshing,false);
});
test('Updated filter actually includes current-version rows and excludes pending/unknown',()=>{
 const rows=[{id:1,installed_version:'2',available_version:'2',installation_state:'current'},{id:2,installed_version:'1',available_version:'2',installation_state:'available'},{id:3,installed_version:'2',available_version:'2',installation_state:'unavailable'}];
 const ctx=vm.createContext({data:{updates:rows},window:{},$:id=>id==='fStatus'?{value:'installed'}:null});
 vm.runInContext(source('sameVersion')+'\n'+source('updateIsVisible')+'\n'+source('updateIsPending')+'\n'+source('visibleUpdates'),ctx);assert.deepEqual(Array.from(ctx.visibleUpdates(),r=>r.id),[1]);
});
test('stale historical installing flag is never animated as live installation',()=>{
 const ctx=vm.createContext({data:{instances:[{id:1,last_update_scan:'2020-01-01T00:00:00'}],operations:[]},FleetRefresh:F,esc:x=>String(x)});
 vm.runInContext(source('sameVersion')+'\n'+source('rowStatus')+'\n'+source('progressWidget'),ctx);
 const u={instance_id:1,in_progress:true,installation_state:'available'};
 assert.match(ctx.progressWidget(u),/last observed installing — status stale/);assert.doesNotMatch(ctx.progressWidget(u),/indeterminate/);
 ctx.data.instances[0].last_update_scan=new Date().toISOString();u.last_discovered=new Date().toISOString();assert.match(ctx.progressWidget(u),/indeterminate/);
});
test('batch does not masquerade as one active HA operation',()=>{
 assert.match(source('renderOperationDock'),/let count=ops.length/);assert.match(source('renderOperationDock'),/Batch waiting for restart/);assert.doesNotMatch(source('renderOperationDock'),/ops.length\+\(/);
});
