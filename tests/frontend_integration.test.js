const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const html = () => fs.readFileSync(path.join(root, 'fleet_manager/static/index.html'), 'utf8');
const sw = () => fs.readFileSync(path.join(root, 'fleet_manager/static/sw.js'), 'utf8');

test('static integration wires one boot, immediate restore, 30 second polling, and cached runtime script', () => {
  const source = html();
  const worker = sw();
  assert.equal((source.match(/\nboot\(\);/g) || []).length, 1);
  assert.match(source, /refreshInitialFleet\(\)/);
  assert.match(source, /Repairs.*Active ops|Active ops.*Repairs/);
  assert.match(source, /intervalMs:30000/);
  assert.match(worker, /'\/assets\/fleet-refresh\.js'/);
  assert.match(worker, /const CACHE = 'ha-fleet-manager-v23'/);
  assert.match(worker, /if \(url\.pathname\.startsWith\('\/api\/'\)\) return;/);
});

test('unavailable updates remain inspectable through an explicit status view', () => {
  const source = html();
  assert.match(source, /value="unavailable">Unavailable/);
  assert.match(source, /status==='unavailable'/);
  assert.match(source, /unavailable — inspect instance status/);
});

test('runtime callers preserve existing repairs and activity data on failed reads', () => {
  const source = html();
  assert.doesNotMatch(source, /data\.repairs\s*=\s*\[\]/);
  assert.doesNotMatch(source, /data\.(audit|notifications|operations|jobs|backups)\s*=\s*\[\]/);
  assert.match(source, /if\(r\.error\)errors\.push/);
});

test('navigation and operation context remain actionable without colliding with browser history', () => {
  const source = html();
  assert.doesNotMatch(source, /function history\(/);
  assert.doesNotMatch(source, /history\(\)/);
  assert.match(source, /function historyPage\(/);
  assert.match(source, /function instanceDetail\(/);
  assert.match(source, /function operationDetail\(/);
  assert.match(source, /function retryOperation\(/);
  assert.match(source, /id="operationDock"/);
  assert.match(source, /hafm-selected-updates/);
  assert.match(source, /hafm-last-bulk-result/);
});
