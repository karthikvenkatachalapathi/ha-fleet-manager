const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const F=require('../fleet_manager/static/fleet-refresh.js');
const html=fs.readFileSync(require('node:path').join(__dirname,'../fleet_manager/static/index.html'),'utf8');
const source=n=>html.split('\n').find(l=>l.startsWith('function '+n+'(')||l.startsWith('async function '+n+'('));
const tick=()=>new Promise(setImmediate);
test('successful skip item never claims installation or verification',()=>{
 const r=F.bulkItemView({status:'succeeded',update:1},[],[],'skip');assert.equal(r.status,'Skipped');assert.match(r.reason,/no installation/);assert.doesNotMatch(r.status,/updated|verified/i);
 assert.equal(F.bulkItemView({status:'succeeded',update:1},[],[],'update').status,'Verified updated');
});
for(const value of [{},null,'bad',[null]])test('malformed list cannot replace good state: '+JSON.stringify(value),async()=>{
 let fail=false;const c=F.createFleetRefreshController({paths:['updates'],keys:['updates'],fetchJson:async()=>fail?value:[{id:1}]});
 await c.refresh();fail=true;await c.refresh();assert.deepEqual(c.getState().updates,[{id:1}]);assert.match(c.getState().error,/Invalid list/);
});
for(const meta of [{},{instances:{}},{instances:[{}]},{instances:[{instance_id:1,refreshing:'false'}]},{instances:[{instance_id:1,last_checked_at:'invalid'}]}])test('malformed metadata fails closed: '+JSON.stringify(meta),()=>{
 assert.throws(()=>F.validateSnapshotMeta(meta));assert.equal(F.snapshotHealth(meta,[{id:1,last_update_scan:new Date().toISOString()}]).phase,'failed');
});
test('manual refresh waits for actual HA result then reads back cache, no pre-observation success',async()=>{
 const calls=[];let release;const c=F.createFleetRefreshController({paths:['updates'],keys:['updates'],fetchJson:async(p,o)=>{calls.push([p,o?.method]);if(p==='/api/sync-all')return new Promise(r=>release=r);return []}});
 let done=false;const pending=c.manualRefresh().then(()=>done=true);await tick();assert.equal(done,false);assert.equal(c.getState().manualInFlight,true);assert.deepEqual(calls,[['/api/sync-all','POST']]);assert.equal(c.manualRefresh(),false);
 release({instances:1,sync_ok:1,sync_failed:0,results:[{instance_id:1,ok:true}]});await pending;assert.equal(done,true);assert.equal(calls[1][0],'updates');assert.equal(c.getState().error,null);
 assert.match(source('syncAll'),/manualRefresh/);assert.doesNotMatch(source('syncAll'),/observeRefresh/);
});
test('inconsistent HA refresh summary cannot become success',async()=>{
 const c=F.createFleetRefreshController({paths:['updates'],keys:['updates'],fetchJson:async p=>p==='/api/sync-all'?{instances:1,sync_ok:1,sync_failed:0,results:[{instance_id:1,ok:false}]}:[]});
 await c.manualRefresh();assert.match(c.getState().error,/Invalid Home Assistant refresh result/);
});
test('installed version is distinct from restart clearance, stale/failed history and missing capped history',()=>{
 const u={id:1,instance_id:2,installed_version:'2',available_version:'2',installation_state:'current'};
 const ctx=vm.createContext({data:{instances:[],operationsLoaded:true,operations:[]},FleetRefresh:F,esc:String});vm.runInContext(source('sameVersion')+'\n'+source('rowStatus'),ctx);
 assert.equal(ctx.rowStatus(u),'version installed');ctx.data.operationsError='unavailable';assert.match(ctx.rowStatus(u),/history unavailable/);ctx.data.operationsError=null;
 ctx.data.operations=[{id:10,instance_id:2,status:'outcome_unknown',details:{update_id:1,target_version:'2',installed_version_verified:true,restart_clearance_verified:false}}];assert.match(ctx.rowStatus(u),/clearance unverified/);
 ctx.data.operations[0].status='action_required';assert.match(ctx.rowStatus(u),/restart required/);
});
test('recent instance scan does not make an old installing row live',()=>{
 const ctx=vm.createContext({data:{instances:[{id:1,last_update_scan:new Date().toISOString()}],operations:[]},FleetRefresh:F,esc:String});vm.runInContext(source('sameVersion')+'\n'+source('rowStatus')+'\n'+source('progressWidget'),ctx);
 assert.doesNotMatch(ctx.progressWidget({instance_id:1,in_progress:true,last_discovered:'2020-01-01T00:00:00'}),/indeterminate/);
});

test('mounted installing widget expires in place across 90 seconds without cached-data change',()=>{
 let clock=Date.parse('2026-11-03T00:00:00Z');const u={id:1,instance_id:2,in_progress:true,last_discovered:'2026-11-03T00:00:00Z'};
 const cell={dataset:{updateProgress:'1'},innerHTML:'original installing animation'},filter={value:'available'};const bar={closest:()=>cell};
 const ctx=vm.createContext({data:{instances:[{id:2,last_update_scan:'2026-11-03T00:00:00Z'}],updates:[u]},activePage:'Updates',fleetController:null,document:{querySelectorAll:()=>[bar]},FleetRefresh:{...F,snapshotHealth:()=>({phase:'current'})},Date:{now:()=>clock},esc:String});
 vm.runInContext(['sameVersion','rowStatus','progressWidget','refreshObservedProgress'].map(source).join('\n'),ctx);
 ctx.refreshObservedProgress();assert.equal(cell.innerHTML,'original installing animation');clock+=91_000;ctx.refreshObservedProgress();assert.match(cell.innerHTML,/status stale/);assert.doesNotMatch(cell.innerHTML,/indeterminate/);assert.equal(filter.value,'available');assert.equal(u.in_progress,true);
});
