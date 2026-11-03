const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../fleet_manager/static/fleet-access-ui.js'),'utf8');
test('ownership is automatic with no redundant input or submitted owner',()=>{
 assert.doesNotMatch(source,/accessOwner|owner:\s*|Owner\/contact|Responsible person or team/);
 assert.match(source,/name:\$\('accessName'\)/);
});
test('token guidance is storage-provider neutral',()=>{
 assert.doesNotMatch(source,/Agent Vault/);
 assert.match(source,/Copied\. Store it securely in your preferred password or secrets manager\./);
});
