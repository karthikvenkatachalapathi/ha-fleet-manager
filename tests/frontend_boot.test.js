const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../fleet_manager/static/index.html'), 'utf8');
const bootSource = source.slice(source.indexOf('async function boot()'), source.indexOf('async function loadOidcConfig()'));
const oidcSource = source.slice(source.indexOf('async function loadOidcConfig()'), source.indexOf('function showLogin()'));

test('anonymous local login does not wait for pending SSO configuration', async () => {
  const events = [];
  const context = vm.createContext({loadOidcConfig: () => new Promise(() => {}), api: async () => {events.push('session'); throw Error('unauthorized');}, showLogin: () => events.push('login'), data:{}, $:()=>null});
  vm.runInContext(bootSource, context);
  context.boot();
  await new Promise(setImmediate);
  assert.deepEqual(events, ['session', 'login']);
});

test('authenticated session restores normally while SSO configuration remains pending', async () => {
  const events=[];
  const context=vm.createContext({loadOidcConfig:()=>new Promise(()=>{}),api:async()=>({csrf_token:'test',user:{id:1}}),data:{},showApp:()=>events.push('app'),savedPage:()=> 'Updates',setPage:()=>{},refreshInitialFleet:async()=>events.push('restore'),route:()=>events.push('route'),showLogin:()=>events.push('login')});
  vm.runInContext(bootSource,context); context.boot(); await new Promise(setImmediate);
  assert.deepEqual(events,['app','route','restore']);
});

test('SSO configuration request is bounded and failure keeps SSO hidden', async () => {
  let timeout; let cleared=false; let aborted=false;
  const button={textContent:'',classList:{add:()=>{},remove:()=>assert.fail('SSO must remain hidden')}};
  const context=vm.createContext({AbortController,setTimeout:(fn)=>{timeout=fn;return 1;},clearTimeout:()=>{cleared=true;},data:{},$:()=>button,fetch:(_url,options)=>new Promise((_resolve,reject)=>{if(options?.signal)options.signal.addEventListener('abort',()=>{aborted=true;reject(Error('aborted'));});})});
  vm.runInContext(oidcSource,context); const result=context.loadOidcConfig();
  assert.equal(typeof timeout,'function'); timeout(); await result;
  assert.equal(aborted,true);assert.equal(cleared,true);assert.equal(context.data.oidcPublic.enabled,false);
});

test('background public SSO response never overwrites authenticated settings', async () => {
  const full={enabled:true,issuer_url:'https://identity.example',client_id:'test-client'};
  const context=vm.createContext({AbortController,setTimeout:()=>1,clearTimeout:()=>{},data:{oidc:full},$:()=>null,fetch:async()=>({ok:true,json:async()=>({enabled:true,button_label:'SSO'})})});
  vm.runInContext(oidcSource,context);await context.loadOidcConfig();assert.equal(context.data.oidc,full);
});

test('successful SSO configuration preserves the configured label', async () => {
  let visible=false;const button={textContent:'',classList:{add:()=>{},remove:()=>{visible=true;}}};
  const context=vm.createContext({AbortController,setTimeout:()=>1,clearTimeout:()=>{},data:{},$:()=>button,fetch:async()=>({ok:true,json:async()=>({enabled:true,button_label:'Company SSO'})})});
  vm.runInContext(oidcSource,context);await context.loadOidcConfig();assert.equal(visible,true);assert.equal(button.textContent,'Company SSO');
});
