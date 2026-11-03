const test=require('node:test');const assert=require('node:assert/strict');
const {createFleetRefreshController}=require('../fleet_manager/static/fleet-refresh.js');
function deferred(){let resolve,reject;const promise=new Promise((r,j)=>{resolve=r;reject=j});return {promise,resolve,reject};}
const tick=()=>new Promise(setImmediate);
for(const outcome of ['success','failure'])for(const settleBeforeCore of [false,true])test(`poll overlapping manual repair ${outcome}, repair settles before core=${settleBeforeCore}`,async()=>{
 const repairs=deferred(),pollCore=deferred();let coreReads=0,repairReads=0;
 const c=createFleetRefreshController({paths:['/core','/repairs'],keys:['instances','repairs'],nonBlockingKeys:['repairs'],fetchJson:path=>{
  if(path==='/api/sync-all')return {sync_failed:0};if(path==='/core')return ++coreReads===1?[{id:1}]:pollCore.promise;
  repairReads++;return repairs.promise;
 }});
 await c.manualRefresh();assert.equal(c.getState().repairsPending,true);
 const poll=c.refresh();await tick();assert.equal(repairReads,1);assert.equal(c.getState().repairsPending,true);
 const finish=()=>outcome==='success'?repairs.resolve([{id:9}]):repairs.reject(Error('repair unavailable'));
 if(settleBeforeCore){finish();await tick();}pollCore.resolve([{id:2}]);await poll;
 if(!settleBeforeCore){assert.equal(c.getState().repairsPending,true);finish();}
 await tick();assert.equal(c.getState().repairsPending,false);
 assert.deepEqual(c.getState().instances,[{id:2}]);
 if(outcome==='success'){assert.deepEqual(c.getState().repairs,[{id:9}]);assert.equal(c.getState().status,'current');}
 else {assert.match(c.getState().error,/repair unavailable/);assert.equal(c.getState().status,'partial');}
});

test('early repair rejection is handled while core remains pending',async()=>{
 const core=deferred();const c=createFleetRefreshController({paths:['/core','/repairs'],keys:['instances','repairs'],nonBlockingKeys:['repairs'],fetchJson:path=>path==='/core'?core.promise:Promise.reject(Error('early failure'))});
 const run=c.refresh();await tick();core.resolve([]);await run;await tick();assert.match(c.getState().error,/early failure/);
});

test('failed background refresh preserves successfully loaded repairs',async()=>{
 let fail=false;const c=createFleetRefreshController({paths:['/core','/repairs'],keys:['instances','repairs'],nonBlockingKeys:['repairs'],fetchJson:path=>path==='/core'?[]:fail?Promise.reject(Error('unavailable')):[{id:5}]});
 await c.refresh();await tick();assert.deepEqual(c.getState().repairs,[{id:5}]);fail=true;await c.refresh();await tick();assert.deepEqual(c.getState().repairs,[{id:5}]);assert.equal(c.getState().repairsLoaded,true);assert.match(c.getState().error,/unavailable/);
});
