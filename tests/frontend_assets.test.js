const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const root=path.join(__dirname,'../fleet_manager/static');

test('screen logo uses a compact separately versioned WebP while original remains available',()=>{
 const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
 assert.match(html,/src="\/assets\/ha-fleet-logo-256.webp"/);
 assert.doesNotMatch(html,/src="\/assets\/ha-fleet-manager-icon-transparent.png"/);
 const asset=fs.readFileSync(path.join(root,'ha-fleet-logo-256.webp'));
 assert.ok(asset.length<50000);assert.equal(asset.toString('ascii',8,12),'WEBP');
 assert.ok(fs.existsSync(path.join(root,'ha-fleet-manager-icon-transparent.png')));
});

test('new controller URL and service worker cache invalidate stale script',()=>{
 const html=fs.readFileSync(path.join(root,'index.html'),'utf8');const sw=fs.readFileSync(path.join(root,'sw.js'),'utf8');
 assert.match(html,/fleet-refresh\.js\?v=30/);assert.match(sw,/fleet-refresh\.js\?v=30/);
 assert.match(sw,/ha-fleet-manager-v30/);assert.match(sw,/ha-fleet-logo-256.webp/);
});
