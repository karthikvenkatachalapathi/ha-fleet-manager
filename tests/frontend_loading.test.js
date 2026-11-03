const test=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../fleet_manager/static/index.html'),'utf8');
const source=name=>html.split('\n').find(s=>s.startsWith('async function '+name+'(')||s.startsWith('function '+name+'('));
test('authenticated boot routes before unresolved fleet reads and returns promptly',async()=>{
 const calls=[];const ctx={loadOidcConfig(){},api:async()=>({csrf_token:'test',user:{}}),data:{},showApp:()=>calls.push('app'),savedPage:()=> 'Settings',route:p=>calls.push(p),refreshInitialFleet:()=>new Promise(()=>{}),showLogin:()=>calls.push('login'),setPage(){},notify(){},$:()=>({})};
 vm.createContext(ctx);vm.runInContext(source('boot'),ctx);
 await Promise.race([ctx.boot(),new Promise((_,reject)=>setTimeout(()=>reject(new Error('boot blocked on fleet reads')),50))]);assert.deepEqual(calls,['app','Settings']);
});
test('GET reads have bounded timeout and abort but mutation requests keep original semantics',async()=>{
 let aborted=false,seenOptions;
 const ctx={csrf:null,activePage:'Updates',settingsTab:'Account',title:{textContent:'Updates'},showLogin(){},AbortController,setTimeout,clearTimeout,fetch:(p,o)=>{seenOptions=o;return new Promise((resolve,reject)=>{o.signal?.addEventListener('abort',()=>{aborted=true;reject(new Error('aborted'))});if(o.method==='POST')resolve({status:200,ok:true,json:async()=>({ok:true})})})}};
 vm.createContext(ctx);vm.runInContext(source('api'),ctx);
 await assert.rejects(()=>ctx.api('/api/updates',{timeoutMs:10}),/timed out/i);assert.equal(aborted,true);
 await ctx.api('/api/updates/1/install',{method:'POST'});assert.equal(seenOptions.signal,undefined);
});
test('fleet status distinguishes cached records from live Home Assistant checks',()=>{
 assert.match(source('renderFleetStatus'),/Saved status/);assert.match(source('renderFleetStatus'),/last_update_scan/);
});
