(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.FleetAccess = api;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this), function () {
  'use strict';
  function escapeHTML(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
  }
  function createController({api, onChange = function () {}, timeoutMs = 10000}) {
    if (typeof api !== 'function') throw new TypeError('api must be a function');
    let state = {identities: [], secret: '', busy: false, loading: false, error: '', secretTokenId: null};
    let generation = 0;
    function emit() { onChange(getState()); }
    function getState() { return {...state, identities: state.identities.map(item => item && typeof item === 'object' ? {...item} : item)}; }
    function update(values) { state = {...state, ...values}; emit(); }
    function errorMessage(error) { return error && error.message ? error.message : String(error || 'Request failed'); }
    async function request(path, options = {}) {
      const controller = new AbortController();
      let timer;
      try {
        return await Promise.race([
          Promise.resolve().then(() => api(path, {...options, signal: controller.signal})),
          new Promise((_, reject) => { timer = setTimeout(() => { controller.abort(); reject(new Error('Request timed out')); }, timeoutMs); })
        ]);
      } finally { clearTimeout(timer); }
    }
    async function loadInternal(expected, explicit) {
      if (expected !== generation) return;
      update({loading: true, error: ''});
      try {
        const result = await request('/api/access/identities');
        if (expected === generation) update({identities: Array.isArray(result && result.identities) ? result.identities : []});
      } catch (error) {
        if (expected === generation) update({error: errorMessage(error)});
        if (explicit) throw error;
      } finally { if (expected === generation) update({loading: false}); }
    }
    function load() { return loadInternal(generation, true); }
    async function readback(expected) { if (expected === generation) await loadInternal(expected, false); }
    function startMutation() {
      if (state.busy) return false;
      update({busy: true, error: ''});
      return true;
    }
    function endMutation() { if (state.busy) update({busy: false}); }
    function ambiguous(error) { return `${errorMessage(error)}. If the result is unclear, refresh the list and revoke any unexpected token; do not repeat issuance.`; }
    async function reauth(password) {
      if (!password) throw new Error('Password is required for reauthentication');
      await request('/api/access/reauth', {method: 'POST', body: {password}});
    }
    async function issue(path, body, password, rotate) {
      if (!startMutation()) throw new Error('A request is already in progress');
      const expected = generation;
      update({secret: '', secretTokenId: null});
      let writeSucceeded = false;
      try {
        await reauth(password);
        if (expected !== generation) return {};
        const result = await request(path, {method: 'POST', body});
        writeSucceeded = true;
        if (expected === generation) update({secret: result && result.token || '', secretTokenId: result && result.token_id != null ? result.token_id : null});
        await readback(expected);
        return {token_id: result && result.token_id, expires_at: result && result.expires_at};
      } catch (error) {
        if (expected === generation) update({error: writeSucceeded ? errorMessage(error) : ambiguous(error)});
        if (writeSucceeded) return {token_id: undefined};
        throw error;
      } finally { endMutation(); }
    }
    function create(payload, password) { return issue('/api/access/identities', payload, password, false); }
    async function change(id, payload, password) {
      if (!startMutation()) throw new Error('A request is already in progress');
      const expected = generation;
      try {
        if (password !== undefined && password !== null && password !== '') await reauth(password);
        if (expected !== generation) return {};
        const result = await request(`/api/access/identities/${encodeURIComponent(id)}`, {method: 'PATCH', body: payload});
        await readback(expected);
        return {identity: result && result.identity};
      } catch (error) { if (expected === generation) update({error: errorMessage(error)}); throw error; }
      finally { endMutation(); }
    }
    async function revoke(id) {
      if (!startMutation()) throw new Error('A request is already in progress');
      const expected = generation;
      try {
        await request(`/api/access/tokens/${encodeURIComponent(id)}/revoke`, {method: 'POST', body: {}});
        await readback(expected);
        return {};
      } catch (error) { if (expected === generation) update({error: errorMessage(error)}); throw error; }
      finally { endMutation(); }
    }
    function rotate(id, days, password) { return issue(`/api/access/tokens/${encodeURIComponent(id)}/rotate`, {expires_in_days: days}, password, true); }
    function clearSecret() { update({secret: '', secretTokenId: null}); }
    function deactivate() { generation++; state = {identities: [], secret: '', busy: false, loading: false, error: '', secretTokenId: null}; emit(); }
    return {getState, load, create, change, revoke, rotate, clearSecret, deactivate};
  }
  return {createController, escapeHTML};
});
