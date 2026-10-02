/* Trustworthy client-side refresh state. Kept dependency-free so browser and Node tests share it. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.FleetRefresh = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const REFRESH_INTERVAL_MS = 30_000;
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function deriveUpdateView(updates) {
    const rows = Array.isArray(updates) ? updates : [];
    const unavailable = rows.filter(row => row.installation_state === 'unavailable' || row.unavailable === true);
    const available = rows.filter(row => !unavailable.includes(row));
    return { defaultPending: available.filter(row => row.in_progress || (row.installation_state === 'available' && row.skip_state !== 'skipped')), unavailable, all: rows };
  }

  function operationRecoveryHtml(operations) {
    return (Array.isArray(operations) ? operations : []).map(operation => {
      const detail = operation.details || {};
      const error = operation.error_message || operation.error || detail.error || detail.message || '';
      const step = operation.step || operation.state || '—';
      const action = operation.recovery || operation.recovery_action || detail.recovery || detail.recovery_action || (operation.status === 'failed' ? 'retry or inspect logs' : 'monitor');
      return `<div class="operationRecovery" data-operation-id="${escapeHtml(operation.id)}"><b>Operation ${escapeHtml(operation.id)}</b> · <span>${escapeHtml(operation.status || 'unknown')}</span> · <span>${escapeHtml(step)}</span>${error ? ` · <span class="err">${escapeHtml(error)}</span>` : ''} · <span>Recovery: ${escapeHtml(action)}</span></div>`;
    }).join('');
  }

  function createFleetRefreshController(options) {
    const opts = options || {};
    const fetchJson = opts.fetchJson || (path => fetch(path).then(response => { if (!response.ok) throw new Error(`${response.status} ${response.statusText}`); return response.json(); }));
    const now = opts.now || (() => Date.now());
    const intervalMs = opts.intervalMs || REFRESH_INTERVAL_MS;
    const setIntervalFn = opts.setIntervalFn || ((fn, ms) => setInterval(fn, ms));
    const clearIntervalFn = opts.clearIntervalFn || (id => clearInterval(id));
    const setTimeoutFn = opts.setTimeoutFn || ((fn, ms) => { const handle = setTimeout(fn, ms); handle.unref?.(); return handle; });
    let generation = 0, timer = null, staleTimer = null;
    const state = { status: 'initial', instances: [], updates: [], operations: [], repairs: [], activity: [], lastSuccessfulRefreshAt: null, partialFailures: [], syncSummary: null, error: null, manualInFlight: false, refreshInFlight: false };
    const emit = () => opts.onChange && opts.onChange(getState());
    const getState = () => ({ ...state, partialFailures: [...state.partialFailures], instances: [...state.instances], updates: [...state.updates], operations: [...state.operations], repairs: [...state.repairs], activity: [...state.activity] });
    const endpointPaths = opts.paths || ['/api/instances?refresh=1', '/api/updates?refresh=1', '/api/operations?refresh=1', ...(opts.extraPaths || [])];
    const keys = opts.keys || ['instances', 'updates', 'operations', 'repairs', 'activity'];

    function scheduleStaleClock() {
      if (staleTimer !== null) return;
      staleTimer = setTimeoutFn(() => { staleTimer = null; markStale(); }, intervalMs * 2);
    }
    async function refresh() {
      const requestGeneration = ++generation;
      state.refreshInFlight = true;
      state.status = state.lastSuccessfulRefreshAt === null ? 'initial' : 'stale';
      state.error = null;
      emit();
      const results = await Promise.all(endpointPaths.map(path => Promise.resolve().then(() => fetchJson(path)).then(value => ({ value })).catch(error => ({ error, path }))));
      if (requestGeneration !== generation) return false;
      const failures = results.filter(result => result.error).map(result => ({ endpoint: result.path, message: result.error.message || String(result.error) }));
      results.forEach((result, index) => { if (!result.error) state[keys[index]] = Array.isArray(result.value) ? result.value : (result.value || []); });
      state.partialFailures = failures;
      if (!failures.length) {
        state.lastSuccessfulRefreshAt = now();
        state.status = state.syncSummary?.sync_failed ? 'partial' : 'current';
        state.error = state.syncSummary?.sync_failed ? syncFailureText(state.syncSummary) : null;
        scheduleStaleClock();
      } else {
        state.error = failures.map(item => `${item.endpoint}: ${item.message}`).join('; ');
        if (state.syncSummary?.sync_failed) state.error = `${syncFailureText(state.syncSummary)}; ${state.error}`;
        state.status = state.lastSuccessfulRefreshAt ? 'failed' : (results.some(result => !result.error) ? 'partial' : 'unavailable');
      }
      state.refreshInFlight = false;
      emit();
      return true;
    }
    function syncFailureText(summary) {
      const failed = (summary.results || []).filter(result => result.ok === false).map(result => result.error || `instance ${result.instance_id} failed`);
      return `Sync completed with ${summary.sync_failed} failure${summary.sync_failed === 1 ? '' : 's'}${failed.length ? `: ${failed.join(', ')}` : ''}`;
    }
    function manualRefresh() {
      if (state.manualInFlight) return false;
      state.manualInFlight = true; state.syncSummary = null; emit();
      return (async () => {
        try {
          const summary = await fetchJson('/api/sync-all', { method: 'POST' });
          state.syncSummary = summary && typeof summary === 'object' ? summary : null;
        } catch (error) {
          state.syncSummary = { sync_failed: 1, results: [{ ok: false, error: error.message || String(error) }] };
        }
        try { await refresh(); } finally { state.manualInFlight = false; emit(); }
        return true;
      })();
    }
    function startPolling() { if (timer === null) timer = setIntervalFn(() => refresh(), intervalMs); return intervalMs; }
    function stopPolling() { if (timer !== null) { clearIntervalFn(timer); timer = null; } if (staleTimer !== null) { clearIntervalFn(staleTimer); staleTimer = null; } }
    function markStale() { if (state.lastSuccessfulRefreshAt !== null && now() - state.lastSuccessfulRefreshAt >= intervalMs * 2) { state.status = 'stale'; emit(); } }
    return { refresh, manualRefresh, restore: refresh, startPolling, stopPolling, markStale, getState, deriveUpdateView };
  }
  return { REFRESH_INTERVAL_MS, createFleetRefreshController, deriveUpdateView, operationRecoveryHtml };
});
