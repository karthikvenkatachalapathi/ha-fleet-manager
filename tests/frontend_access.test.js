const test = require('node:test');
const assert = require('node:assert/strict');
const FleetAccess = require('../fleet_manager/static/fleet-access.js');

function setup(routes = {}, timeoutMs = 100) {
  const calls = [];
  const api = async (url, options = {}) => {
    calls.push({url, options});
    const route = routes[`${options.method || 'GET'} ${url}`] || routes[url];
    if (route instanceof Error) throw route;
    if (typeof route === 'function') return route(options);
    return route === undefined ? (url === '/api/access/identities' ? {identities: []} : {}) : route;
  };
  const changes = [];
  const controller = FleetAccess.createController({api, onChange: state => changes.push(state), timeoutMs});
  return {controller, calls, changes};
}
const payload = {name: '<x>', owner: 'agent', purpose: 'test', role: 'read', instance_ids: [], all_instances: false, include_future_instances: false, expires_in_days: 90};

test('exports DOM-independent controller and HTML escaping', () => {
  assert.equal(typeof FleetAccess.createController, 'function');
  assert.equal(FleetAccess.escapeHTML(`<a&"'>`), '&lt;a&amp;&quot;&#39;&gt;');
});

test('loads identities and returns copied state', async () => {
  const {controller} = setup({'GET /api/access/identities': {identities: [{id: 3}]}});
  await controller.load();
  const state = controller.getState();
  assert.deepEqual(state.identities, [{id: 3}]);
  state.identities.push({id: 4});
  assert.deepEqual(controller.getState().identities, [{id: 3}]);
});

test('create reauthenticates, issues once, retains ephemeral secret through readback', async () => {
  const {controller, calls} = setup({
    'POST /api/access/reauth': {}, 'POST /api/access/identities': {identity: {id: 1}, token: 'synthetic-secret', token_id: 9, expires_at: 'later'},
    'GET /api/access/identities': {identities: []}
  });
  const result = await controller.create(payload, 'synthetic-password');
  assert.equal(result.token_id, 9);
  assert.equal(controller.getState().secret, 'synthetic-secret');
  assert.equal(controller.getState().secretTokenId, 9);
  assert.deepEqual(calls.map(c => c.url), ['/api/access/reauth', '/api/access/identities', '/api/access/identities']);
  assert.deepEqual(calls[0].options.body, {password: 'synthetic-password'});
  assert.deepEqual(calls[1].options.body, payload);
  controller.clearSecret();
  assert.equal(controller.getState().secret, '');
  assert.equal(controller.getState().secretTokenId, null);
});

test('readback failure preserves issued secret and successful write is not rejected', async () => {
  const {controller, calls} = setup({'POST /api/access/reauth': {}, 'POST /api/access/identities': {token: 'once', token_id: 2}, 'GET /api/access/identities': new Error('readback down')});
  const result = await controller.create(payload, 'p');
  assert.equal(result.token_id, 2);
  assert.equal(controller.getState().secret, 'once');
  assert.match(controller.getState().error, /readback down/);
  assert.equal(calls.filter(c => c.options.method === 'POST' && c.url === '/api/access/identities').length, 1);
});

test('single mutating operation guard rejects concurrent issuance', async () => {
  let finish;
  const {controller, calls} = setup({'POST /api/access/reauth': {}, 'POST /api/access/identities': () => new Promise(resolve => {finish = resolve;})});
  const first = controller.create(payload, 'p');
  await new Promise(resolve => setImmediate(resolve));
  await assert.rejects(controller.create(payload, 'p'), /progress|busy/i);
  finish({token: 'x', token_id: 1});
  await first;
  assert.equal(calls.filter(c => c.options.method === 'POST' && c.url === '/api/access/identities').length, 1);
});

test('reauth failure prevents issuance; ambiguous write is not retried', async () => {
  const failAuth = setup({'POST /api/access/reauth': new Error('denied')});
  await assert.rejects(failAuth.controller.create(payload, 'wrong'));
  assert.equal(failAuth.calls.length, 1);
  const failWrite = setup({'POST /api/access/reauth': {}, 'POST /api/access/identities': new Error('network')});
  await assert.rejects(failWrite.controller.create(payload, 'p'), /network/);
  assert.equal(failWrite.calls.filter(c => c.url === '/api/access/identities' && c.options.method === 'POST').length, 1);
  assert.match(failWrite.controller.getState().error, /revoke|do not repeat|refresh/i);
});

test('deactivate prevents late issuance from restoring secret or view', async () => {
  let finish;
  const {controller} = setup({'POST /api/access/reauth': {}, 'POST /api/access/identities': () => new Promise(resolve => {finish = resolve;})});
  const pending = controller.create(payload, 'p');
  await new Promise(resolve => setImmediate(resolve));
  controller.deactivate();
  finish({token: 'late-secret', token_id: 3});
  await pending;
  assert.equal(controller.getState().secret, '');
  assert.deepEqual(controller.getState().identities, []);
});

test('change and revoke use the specified endpoints and optional reauth', async () => {
  const {controller, calls} = setup({'POST /api/access/reauth': {}, 'PATCH /api/access/identities/7': {identity: {id: 7}}, 'POST /api/access/tokens/4/revoke': {}});
  await controller.change(7, {enabled: false});
  await controller.change(7, {role: 'write'}, 'pw');
  await controller.revoke(4);
  assert.deepEqual(calls.map(c => `${c.options.method || 'GET'} ${c.url}`), ['PATCH /api/access/identities/7', 'GET /api/access/identities', 'POST /api/access/reauth', 'PATCH /api/access/identities/7', 'GET /api/access/identities', 'POST /api/access/tokens/4/revoke', 'GET /api/access/identities']);
});

test('rotation uses endpoint, requires reauth, stores secret', async () => {
  const {controller, calls} = setup({'POST /api/access/reauth': {}, 'POST /api/access/tokens/12/rotate': {token: 'rotated', token_id: 13}});
  const result = await controller.rotate(12, 30, 'pw');
  assert.equal(result.token_id, 13);
  assert.equal(controller.getState().secret, 'rotated');
  assert.deepEqual(calls[1].options.body, {expires_in_days: 30});
});

test('timeout bounds requests even when API ignores abort', async () => {
  const {controller} = setup({'GET /api/access/identities': () => new Promise(() => {})}, 5);
  await assert.rejects(controller.load(), /timed out/i);
  assert.match(controller.getState().error, /timed out/i);
});
