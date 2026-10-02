const test = require('node:test');
const assert = require('node:assert/strict');
const { createFleetRefreshController, deriveUpdateView } = require('../fleet_manager/static/fleet-refresh.js');
const fs = require('node:fs');
const path = require('node:path');

function deferred() { let resolve, reject; const promise = new Promise((r, j) => { resolve = r; reject = j; }); return { promise, resolve, reject }; }
function payload(name, extra = {}) { return { instances: [{ id: 1, friendly_name: name, last_update_scan: '2026-10-02T10:00:00Z' }], updates: [{ id: 1, instance_id: 1, installation_state: 'available', skip_state: 'none', component: name }], operations: [], ...extra }; }

test('latest refresh response wins when responses arrive out of order', async () => {
  const first = deferred(); const second = deferred(); const calls = [];
  const c = createFleetRefreshController({ fetchJson: path => { calls.push(path); if (path === '/api/instances?refresh=1') return calls.filter(x => x === path).length === 1 ? first.promise : second.promise; return []; } });
  const older = c.refresh(); const newer = c.refresh();
  second.resolve(payload('new').instances); await newer;
  first.resolve(payload('old').instances); await older;
  assert.equal(c.getState().instances[0].friendly_name, 'new');
});

test('partial and unavailable instances are visible in status but unavailable updates are not pending by default', async () => {
  const c = createFleetRefreshController({ fetchJson: async path => {
    if (path === '/api/instances?refresh=1') return payload('online', { instances: [{ id: 1, friendly_name: 'online', last_update_scan: '2026-10-02T10:00:00Z' }, { id: 2, friendly_name: 'offline', connectivity_state: 'offline' }] }).instances;
    if (path === '/api/updates?refresh=1') throw new Error('updates endpoint unavailable');
    return [];
  }});
  await c.refresh();
  const state = c.getState();
  assert.equal(state.status, 'partial');
  assert.match(state.error, /updates/i);
  const rows = deriveUpdateView([{ id: 1, installation_state: 'available' }, { id: 2, installation_state: 'unavailable' }]);
  assert.deepEqual(rows.defaultPending.map(x => x.id), [1]);
  assert.deepEqual(rows.unavailable.map(x => x.id), [2]);
});

test('manual refresh is in flight guarded and submits only one sync-all', async () => {
  const sync = deferred(); let syncCalls = 0;
  const c = createFleetRefreshController({ fetchJson: async path => {
    if (path === '/api/sync-all') { syncCalls++; return sync.promise; }
    return path.includes('instances') ? payload('ok').instances : [];
  }});
  const one = c.manualRefresh(); const two = c.manualRefresh();
  assert.equal(two, false); sync.resolve({ sync_failed: 0 }); await one;
  assert.equal(syncCalls, 1); assert.equal(c.getState().manualInFlight, false);
});

test('failed refresh preserves previous data and recovers on next successful poll', async () => {
  let fail = false;
  const c = createFleetRefreshController({ fetchJson: async path => {
    if (path.includes('instances')) { if (fail) throw new Error('offline'); return payload('known').instances; }
    return [];
  }});
  await c.refresh(); fail = true; await c.refresh();
  assert.equal(c.getState().instances[0].friendly_name, 'known');
  assert.equal(c.getState().status, 'failed');
  fail = false; await c.refresh();
  assert.equal(c.getState().status, 'current');
  assert.equal(c.getState().error, null);
});

test('reload restores backend operations instead of local optimistic progress', async () => {
  const operation = { id: 42, kind: 'install', status: 'running', state: 'verifying' };
  const c = createFleetRefreshController({ fetchJson: async path => path.includes('operations') ? [operation] : path.includes('instances') ? payload('ok').instances : [] });
  await c.restore();
  assert.deepEqual(c.getState().operations, [operation]);
  assert.equal(c.getState().operations[0].status, 'running');
});

test('polling refreshes status and activity without calling sync-all', async () => {
  const paths = []; const timers = [];
  const c = createFleetRefreshController({ fetchJson: async path => { paths.push(path); return path.includes('instances') ? payload('ok').instances : []; }, setIntervalFn: fn => { timers.push(fn); return 1; }, clearIntervalFn: () => {} });
  c.startPolling(); await timers[0]();
  assert.equal(paths.includes('/api/sync-all'), false);
  assert.ok(paths.includes('/api/operations?refresh=1'));
});

test('manual refresh preserves sync-all partial failures after read-only restore succeeds', async () => {
  const c = createFleetRefreshController({ fetchJson: async path => {
    if (path === '/api/sync-all') return { sync_failed: 1, results: [{ instance_id: 2, ok: false, error: 'timeout' }] };
    return path.includes('instances') ? payload('ok').instances : [];
  }});
  await c.manualRefresh();
  const state = c.getState();
  assert.equal(state.status, 'partial');
  assert.equal(state.syncSummary.sync_failed, 1);
  assert.match(state.error, /timeout|sync/i);
});

test('stale clock transition is local and does not request the network', async () => {
  let clock = 0; const paths = [];
  const c = createFleetRefreshController({ now: () => clock, intervalMs: 30, fetchJson: async path => { paths.push(path); return []; } });
  await c.refresh(); paths.length = 0; clock = 61; c.markStale();
  assert.equal(c.getState().status, 'stale');
  assert.deepEqual(paths, []);
});

test('repairs and activity endpoint failures preserve prior data', async () => {
  let fail = false;
  const c = createFleetRefreshController({ extraPaths: ['/api/repairs?refresh=1', '/api/activity?refresh=1'], fetchJson: async path => {
    if (path.includes('repairs')) { if (fail) throw new Error('repairs down'); return [{ id: 7 }]; }
    if (path.includes('activity')) { if (fail) throw new Error('activity down'); return [{ id: 8 }]; }
    return [];
  }});
  await c.refresh(); fail = true; await c.refresh();
  assert.deepEqual(c.getState().repairs, [{ id: 7 }]);
  assert.deepEqual(c.getState().activity, [{ id: 8 }]);
  assert.match(c.getState().error, /repairs|activity/);
});

test('duplicate polling starts create only one timer', () => {
  let timers = 0;
  const c = createFleetRefreshController({ setIntervalFn: () => ++timers });
  c.startPolling(); c.startPolling();
  assert.equal(timers, 1);
});

test('operation recovery display helper exposes id, step, status, error, and recovery action', () => {
  const html = require('../fleet_manager/static/fleet-refresh.js').operationRecoveryHtml([{ id: 42, status: 'failed', state: 'installing', error_message: 'Home Assistant is unavailable', recovery: 'check connectivity, then retry' }]);
  assert.match(html, /42/); assert.match(html, /installing/); assert.match(html, /failed/); assert.match(html, /Home Assistant is unavailable/); assert.match(html, /check connectivity, then retry/i);
});
