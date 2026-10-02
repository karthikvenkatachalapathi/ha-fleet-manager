# Fleet Manager Reliability Investigation — 2026-10-02

## Scope and safety

- Source repository: `/home/hermes/ha-update-dashboard`
- Baseline commit: `23d13e3 fix: refresh fleet state and harden update operations`
- Baseline suite: `44 passed, 8 warnings`
- Live app observed on port `8799`; health endpoint returned HTTP 200.
- No live Home Assistant update, repair, reboot, restart, backup, or skip action was triggered.

## Architecture trace

`browser UI (fleet_manager/static/index.html)` → `FastAPI routes (fleet_manager/app.py)` → `SQLite persistence (models.py/db.py)` → `automation services (services/automation.py)` → `HomeAssistantAdapter (services/ha_adapter.py)` → Home Assistant HTTP/WebSocket APIs.

The browser reads normalized instance/update/operation records from the Fleet Manager database. Refresh requests synchronously contact every configured Home Assistant instance, update persisted snapshots, then return a per-instance summary.

## Diagnosis checklist

### Status trust

- [x] Initial load calls fleet sync before reading cached rows.
- [ ] General automatic browser refresh exists. Current polling only runs while an update reports `in_progress`.
- [ ] Manual refresh reports partial instance failures. Current UI reports `Refreshed` for HTTP 200 even when `sync_failed > 0`.
- [ ] Global last-successful-refresh timestamp is visible.
- [ ] Current, stale, unavailable, and failed data states are distinct.
- [ ] Older responses cannot overwrite newer data.
- [ ] Repairs/activity fetch failures preserve prior data and show failure instead of replacing data with empty arrays.

### Operation resilience

- [x] Update installs poll actual HA update state before declaring success.
- [ ] Restarts and repair-triggered restarts/reboots are persisted as operations.
- [ ] Restart/reconnect completion is verified against actual instance availability.
- [ ] Skip and backup completion are verified against actual HA state.
- [ ] Accepted-but-unverified installs are reconciled later.
- [ ] Interrupted running operations are reconciled on application startup.
- [ ] Terminal state is guaranteed by bounded deadlines.

### Retry and concurrency safety

- [ ] Duplicate submissions are rejected or return the existing operation.
- [ ] Incompatible operations on the same instance cannot overlap.
- [ ] A retry checks actual state before resubmitting.
- [ ] Non-idempotent HA service calls are not blindly repeated after ambiguous network failures.
- [ ] Bulk processing has bounded concurrency and independent per-instance outcomes.
- [ ] Duplicate IDs in bulk input cannot distort totals or repeat work.

### Persistence and diagnostics

- [x] Operation/audit records are persisted in SQLite.
- [ ] Bulk requests have durable parent/correlation IDs.
- [ ] Operation IDs are present in diagnostic records and UI recovery actions.
- [ ] Vault/log write failures cannot invalidate an already-executed HA action.
- [ ] Scheduler partial failures are represented as partial failure rather than success.
- [ ] Scheduler exceptions are persisted instead of silently swallowed.

## Evidence-backed root causes

1. Frontend refresh flows have no request-generation guard or cancellation; overlapping loads can commit out of order.
2. Browser auto-refresh is conditional on update progress rather than fleet freshness.
3. Cached rows remain visible after failed sync without a stale marker or global refresh timestamp.
4. Manual refresh ignores the backend’s `sync_failed` and per-instance result fields.
5. Operations execute synchronously inside request handlers with no durable queue, per-instance lock, or idempotency key.
6. Restart/repair paths only mutate instance status and audit; they do not create or reconcile operations.
7. Startup initializes tables/defaults but never reconciles interrupted operations or jobs.
8. Install retries can repeat an accepted non-idempotent request when response/state observation is ambiguous.
9. Scheduler exceptions are broadly suppressed, and discovery jobs report `succeeded` despite per-instance failures.
10. Existing frontend tests are static substring assertions, not behavioral race/failure tests.

## Implementation/verification tracking

- [x] Add regression tests that fail against the diagnosed behavior.
- [x] Implement trustworthy refresh state and latest-response-wins behavior.
- [x] Implement persistent/reconcilable operation lifecycle and safe deduplication.
- [x] Add bounded retry/backoff/concurrency behavior.
- [x] Add actionable operation diagnostics and recovery endpoints/UI.
- [x] Run focused tests, full suite, compile checks, and repeated mock end-to-end runs.
- [x] Verify isolated rendered desktop/mobile workflows and live read-only health routes.
- [x] Obtain explicit approval before deployment or any live HA mutation.

## Final source evidence

- Backend: 65 tests passed.
- Frontend: 15 tests passed.
- Inline browser JavaScript parse, Python compile, and `git diff --check`: passed.
- Browser validation: desktop and mobile rendered successfully with zero horizontal overflow.
- Safety: no Home Assistant update, repair, backup, reboot, restart, or skip was used during source validation.
