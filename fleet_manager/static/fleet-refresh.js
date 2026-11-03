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

  function parseTime(value) {
    if (value == null || value === '') return NaN;
    if (typeof value === 'number') return value;
    let text = String(value);
    if (/^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?$/.test(text)) text = text.replace(' ', 'T') + 'Z';
    return Date.parse(text);
  }
  function validateSnapshotMeta(meta) {
    if(!meta||typeof meta!=='object'||!Array.isArray(meta.instances))throw new Error('Invalid Home Assistant snapshot metadata');
    const seen=new Set();
    for(const row of meta.instances){
      if(!row||typeof row!=='object'||!Number.isInteger(Number(row.instance_id))||Number(row.instance_id)<1||seen.has(String(row.instance_id)))throw new Error('Invalid Home Assistant snapshot instances');
      seen.add(String(row.instance_id));
      if(row.refreshing!==undefined&&typeof row.refreshing!=='boolean')throw new Error('Invalid Home Assistant observation state');
      if(row.error!=null&&typeof row.error!=='string')throw new Error('Invalid Home Assistant observation error');
      if(row.last_checked_at!=null&&!Number.isFinite(parseTime(row.last_checked_at)))throw new Error('Invalid Home Assistant observation time');
    }
    return meta;
  }
  function snapshotHealth(meta, instances, time = Date.now()) {
    if(meta!=null){try{validateSnapshotMeta(meta)}catch(e){return {phase:'failed',lastCheckedAt:null,ageSeconds:null}}}
    const rows = meta!=null ? meta.instances : (instances || []).map(i => ({instance_id:i.id,last_checked_at:i.last_update_scan}));
    const times = rows.map(r=>parseTime(r.last_checked_at)).filter(Number.isFinite);
    const last = times.length ? Math.min(...times) : null;
    const age = last===null ? null : (time-last)/1000;
    const error = rows.some(r=>r.error), refreshing=rows.some(r=>r.refreshing);
    return {phase:error?'failed':refreshing?'refreshing':!rows.length||times.length!==rows.length?'unknown':age<0||age>90?'stale':'current',lastCheckedAt:last,ageSeconds:age};
  }
  function describeBulk(job) {
    const rows=job?.results||[], wait=rows.filter(r=>r.status==='waiting_verification'&&String(r.reason||'').startsWith('waiting_for_restart:')).length;
    const parts=[];
    if(job?.updated)parts.push(`Verified ${job.updated} update${job.updated===1?'':'s'}`);
    if(job?.skipped)parts.push(`Skipped ${job.skipped}`);
    if(job?.action_required)parts.push(`Installed; restart required: ${job.action_required}`);
    if(wait)parts.push(`Waiting for restart: ${wait}`);
    const queued=Math.max(0,Number(job?.queued||0)-wait);
    if(queued)parts.push(`Queued: ${queued}`);
    if(job?.running)parts.push(`Awaiting verification: ${job.running}`);
    if(job?.failed?.length)parts.push(`Failed: ${job.failed.length}`);
    if(job?.blocked?.length)parts.push(`Blocked: ${job.blocked.length}`);
    if(job?.reviewed)parts.push(`Reviewed: ${job.reviewed}`);
    if(Number(job?.pending||0)>0)parts.push('Remaining work not yet verified');
    return parts.join(' · ')||`${job?.action||'Batch'}: ${job?.status||'unknown'}`;
  }
  function bulkItemView(item,updates,instances,action='update') {
    const row=(updates||[]).find(u=>String(u.id)===String(item.update));
    const home=(instances||[]).find(i=>String(i.id)===String(item.instance));
    let status=item.status||'unknown', reason=item.reason||'';
    if(status==='waiting_verification'&&String(reason).startsWith('waiting_for_restart:')){status='Waiting for restart';reason='Another completed install needs a restart before this home can continue';}
    else if(status==='waiting_verification'){status='Waiting';reason='Waiting for the previous operation to finish verification';}
    else if(status==='succeeded'){status=action==='skip'?'Skipped':'Verified updated';reason=action==='skip'?'Skip recorded; no installation was performed':'Target version was verified';}
    else if(status==='action_required'){status='Installed — restart required';reason='Installation finished; restart remains a separate action';}
    else if(status==='queued'){status='Queued — not installing';reason='No install has started for this item';}
    return {name:row?.component||`Update #${item.update}`,home:home?.friendly_name||`Instance #${item.instance}`,status,reason:String(reason).replaceAll('_',' ')};
  }

  function createFleetRefreshController(options) {
    const opts = options || {};
    const fetchJson = opts.fetchJson || (path => fetch(path).then(response => { if (!response.ok) throw new Error(`${response.status} ${response.statusText}`); return response.json(); }));
    const now = opts.now || (() => Date.now());
    const intervalMs = opts.intervalMs || REFRESH_INTERVAL_MS;
    const setIntervalFn = opts.setIntervalFn || ((fn, ms) => setInterval(fn, ms));
    const clearIntervalFn = opts.clearIntervalFn || (id => clearInterval(id));
    const setTimeoutFn = opts.setTimeoutFn || ((fn, ms) => { const handle = setTimeout(fn, ms); handle.unref?.(); return handle; });
    let generation = 0, timer = null, staleTimer = null, followupTimer = null, refreshPromise = null;
    const state = { status: 'initial', snapshotMeta: null, loadedKeys: [], instances: [], updates: [], operations: [], repairs: [], activity: [], lastSuccessfulRefreshAt: null, partialFailures: [], syncSummary: null, error: null, manualInFlight: false, refreshInFlight: false, repairsPending: false, repairsLoaded: false };
    const emit = () => opts.onChange && opts.onChange(getState());
    const getState = () => ({ ...state, partialFailures: [...state.partialFailures], instances: [...state.instances], updates: [...state.updates], operations: [...state.operations], repairs: [...state.repairs], activity: [...state.activity] });
    const endpointPaths = opts.paths || ['/api/instances?refresh=1', '/api/updates?refresh=1', '/api/operations?refresh=1', ...(opts.extraPaths || [])];
    const keys = opts.keys || ['instances', 'updates', 'operations', 'repairs', 'activity'];
    const nonBlockingKeys = new Set(opts.nonBlockingKeys || []);
    const reads = new Map();
    function read(path, force) {
      if (!force && reads.has(path)) return reads.get(path);
      const promise = Promise.resolve().then(() => fetchJson(path)).then(value => {
        if(!Array.isArray(value)||value.some(row=>!row||typeof row!=='object'||Array.isArray(row)))throw new Error('Invalid list response — showing last known data');
        if(value.snapshotMeta!=null)validateSnapshotMeta(value.snapshotMeta);
        return value;
      }).finally(() => { if (reads.get(path) === promise) reads.delete(path); });
      reads.set(path, promise); return promise;
    }

    function scheduleStaleClock() {
      if (staleTimer !== null) return;
      staleTimer = setTimeoutFn(() => { staleTimer = null; markStale(); }, intervalMs * 2);
    }
    function refresh(forceFresh) {
      if(refreshPromise&&!forceFresh)return refreshPromise;
      const pending=performRefresh(forceFresh);refreshPromise=pending;
      pending.finally(()=>{if(refreshPromise===pending)refreshPromise=null;}).catch(()=>{});
      return pending;
    }
    async function performRefresh(forceFresh) {
      const requestGeneration = ++generation;
      state.refreshInFlight = true;
      state.repairsPending = endpointPaths.some((path, index) => keys[index] === 'repairs' && nonBlockingKeys.has('repairs'));
      state.status = state.lastSuccessfulRefreshAt === null ? 'initial' : 'stale';
      state.error = null;
      emit();
      const blocking = [], background = [];
      endpointPaths.forEach((path, index) => {
        const target = nonBlockingKeys.has(keys[index]) ? background : blocking;
        target.push({ path, index, promise: read(path, !!forceFresh) });
      });
      const settle = async item => { try { return { value: await item.promise, path: item.path, index: item.index }; } catch (error) { return { error, path: item.path, index: item.index }; } };
      // Attach rejection handlers before awaiting core reads: a fast background failure
      // must never become an unhandled rejection while core endpoints are still pending.
      const backgroundSettled = background.map(item => settle(item));
      const results = await Promise.all(blocking.map(settle));
      if (requestGeneration !== generation) return false;
      const failures = results.filter(result => result.error).map(result => ({ endpoint: result.path, message: result.error.message || String(result.error) }));
      results.forEach(result => { if (!result.error) { state[keys[result.index]] = Array.isArray(result.value) ? result.value : (result.value || []); if(!state.loadedKeys.includes(keys[result.index]))state.loadedKeys.push(keys[result.index]); if(keys[result.index]==='updates')state.snapshotMeta=result.value?.snapshotMeta||null; } });
      state.partialFailures = failures;
      if (!failures.length) {
        state.lastSuccessfulRefreshAt = now();
        state.status = state.syncSummary?.sync_failed ? 'partial' : (background.length ? 'partial' : 'current');
        state.error = state.syncSummary?.sync_failed ? syncFailureText(state.syncSummary) : null;
        scheduleStaleClock();
      } else {
        state.error = failures.map(item => `${item.endpoint}: ${item.message}`).join('; ');
        if (state.syncSummary?.sync_failed) state.error = `${syncFailureText(state.syncSummary)}; ${state.error}`;
        state.status = state.lastSuccessfulRefreshAt ? 'failed' : (results.some(result => !result.error) ? 'partial' : 'unavailable');
      }
      state.refreshInFlight = false;
      state.repairsPending = background.some(item => keys[item.index] === 'repairs');
      emit();
      if(state.snapshotMeta?.instances?.some(i=>i.refreshing)&&followupTimer===null)followupTimer=setTimeoutFn(()=>{followupTimer=null;void refresh();},1500);
      background.forEach((item, backgroundIndex) => {
        backgroundSettled[backgroundIndex].then(result => {
        if (requestGeneration !== generation) return;
        if (result.error) {
          state.partialFailures = [...state.partialFailures.filter(f => f.endpoint !== item.path), { endpoint: item.path, message: result.error.message || String(result.error) }];
          state.error = [state.syncSummary?.sync_failed ? syncFailureText(state.syncSummary) : null, ...state.partialFailures.map(f => `${f.endpoint}: ${f.message}`)].filter(Boolean).join('; ');
          if (state.status !== 'failed' && state.status !== 'unavailable' && state.status !== 'stale') state.status = 'partial';
        } else { state[keys[item.index]] = Array.isArray(result.value) ? result.value : (result.value || []);if(!state.loadedKeys.includes(keys[item.index]))state.loadedKeys.push(keys[item.index]); }
        if (keys[item.index] === 'repairs') { state.repairsPending = false; if (!result.error) state.repairsLoaded = true; }
        if (!state.partialFailures.length && !state.repairsPending && !state.syncSummary?.sync_failed && state.status !== 'stale') { state.status = 'current'; state.error = null; }
        emit();
        });
      });
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
          if(!summary||!Number.isInteger(summary.instances)||!Number.isInteger(summary.sync_ok)||!Number.isInteger(summary.sync_failed)||summary.instances<0||summary.sync_ok<0||summary.sync_failed<0||summary.sync_ok+summary.sync_failed!==summary.instances||!Array.isArray(summary.results)||summary.results.length!==summary.instances||summary.results.some(r=>!r||!Number.isInteger(r.instance_id)||typeof r.ok!=='boolean')||new Set(summary.results.map(r=>r.instance_id)).size!==summary.instances||summary.results.filter(r=>r.ok).length!==summary.sync_ok)throw new Error('Invalid Home Assistant refresh result');
          state.syncSummary=summary;
        } catch (error) {
          state.syncSummary = { sync_failed: 1, results: [{ ok: false, error: error.message || String(error) }] };
        }
        try { await refresh(true); } finally { state.manualInFlight = false; emit(); }
        return true;
      })();
    }
    function observeRefresh() {
      if(state.manualInFlight)return false;
      state.manualInFlight=true;state.syncSummary=null;emit();
      return (async()=>{try{return await refresh(true);}finally{state.manualInFlight=false;emit();}})();
    }
    function startPolling() { if (timer === null) timer = setIntervalFn(() => refresh(), intervalMs); return intervalMs; }
    function stopPolling() { generation++;if(followupTimer!==null){clearTimeout(followupTimer);followupTimer=null;} if (timer !== null) { clearIntervalFn(timer); timer = null; } if (staleTimer !== null) { clearIntervalFn(staleTimer); staleTimer = null; } }
    function markStale() { if (state.lastSuccessfulRefreshAt !== null && now() - state.lastSuccessfulRefreshAt >= intervalMs * 2) { state.status = 'stale'; emit(); } }
    return { refresh, manualRefresh, observeRefresh, restore: refresh, startPolling, stopPolling, markStale, getState, deriveUpdateView };
  }
  return { REFRESH_INTERVAL_MS, createFleetRefreshController, deriveUpdateView, operationRecoveryHtml, parseTime, snapshotHealth, validateSnapshotMeta, describeBulk, bulkItemView };
});
