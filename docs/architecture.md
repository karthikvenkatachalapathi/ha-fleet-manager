# Home Assistant Fleet Manager — Architecture

## Current repository

The previous repository was a small read-only Python dashboard:

- `ha_update_dashboard/scanner.py` — synchronous update scan from configured HA instances.
- `ha_update_dashboard/server.py` — static page + sanitized JSON endpoint.
- `config.local.json` — local, gitignored instance config.

This implementation keeps that prototype intact and adds a production-oriented app under `fleet_manager/`.

## Validated Home Assistant API surface

Validated against the live fleet using the official REST API pattern already in use by the existing monitor:

- `GET /api/config` — authentication, API reachability, Home Assistant Core version, location metadata.
- `GET /api/states` — state inventory; update entities are exposed as `update.*` entities.
- Update entities expose useful attributes in practice:
  - `installed_version`
  - `latest_version`
  - `release_url`
  - `title`
  - `friendly_name`
  - `in_progress`
  - `skipped_version`
- Update installation is expected to use Home Assistant service calls, e.g. `POST /api/services/update/install`, but Phase 1 intentionally does **not** execute updates.

Live limitations / safety notes:

- Release notes may be only a URL, not structured release content.
- Breaking-change detection cannot be guaranteed from entity metadata alone.
- Home Assistant Core / HAOS updates remain manual-notify for Karthik's fleet policy.
- Backups/restore capabilities vary by installation type and Supervisor availability; they must be capability-detected before use.
- Automatic rollback is not promised. Restore must remain explicit human action.

## Trust boundary

```text
Browser
  -> Fleet Manager API/backend
    -> encrypted credential service
    -> Home Assistant Adapter
      -> configured Home Assistant instance only
```

The browser never receives stored Home Assistant tokens. It submits a token only during add/replace credential. Existing tokens are never shown.

## Credential architecture

- `CredentialService` owns encryption/decryption.
- AES-256-GCM via `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
- `MASTER_ENCRYPTION_KEY` is runtime-injected and must decode to 32 bytes.
- Encrypted rows store nonce/ciphertext/tag; the key is not stored in the database.
- AAD binds ciphertext to `instance:{id}`.
- Provider abstraction is explicit through credential row fields (`provider`, `key_id`) for future Vault/1Password/AWS/GCP/Azure/Kubernetes providers.

## Authentication / authorization

Phase 1 includes:

- Argon2 password hashing.
- Server-side sessions.
- HttpOnly SameSite cookies.
- CSRF token header for mutating API calls.
- Role/permission map with Admin, Operator, Read Only boundaries.
- Audit records for login/failure/logout and sensitive actions.

Production HTTPS should set secure cookies at the reverse proxy boundary.

## SSRF / credential-leak controls

Implemented baseline:

- Only `http` and `https` base URLs accepted.
- Credentials embedded in URLs rejected.
- API paths are backend-owned, not caller-provided.
- No generic proxy endpoint exists.
- HA credentials are sent only by `HomeAssistantAdapter` to the configured instance base URL.
- Redirects are not followed, so credentials cannot leak to redirected hosts.
- Browser sees sanitized resource models only.

Future hardening:

- Persist DNS resolution/cert fingerprints where appropriate.
- Private CA bundle support per instance.
- Per-instance explicit self-signed certificate policy if ever required.

## Domain model

Implemented Phase 1 tables:

- `users`
- `sessions`
- `instances`
- `instance_credentials`
- `update_records`
- `deployment_plans` (draft/preview only)
- `operations` (state-machine placeholder)
- `audit_events`

Designed future tables:

- approvals
- critical_rules
- update_policies
- skipped_updates
- snoozed_updates
- maintenance_holds
- backups
- schedules
- jobs/job_runs
- notifications
- application_settings

## Normalized update model

`UpdateRecord` normalizes Home Assistant update entities into:

- provider
- entity/component/category
- installed/available version
- release URL / notes
- risk level
- critical flag
- breaking flag
- restart required
- manual action required
- approval state
- installation state
- policy decision and explanation

## Policy model

Phase 1 deterministic default:

- Core / OS / Supervisor / firmware / router / Zigbee / Z-Wave / Matter / Thread => high risk and approval required.
- Breaking/action-required release content => critical and approval required.
- Other updates => eligible for manual deployment planning.

Policy decisions are explainable through `policy_explanation`.

## Approval model

Phase 1 surfaces approval requirements but does not yet implement approval records.

Required Phase 3 invariant:

- Approval binds to exact instance + update + target version.
- If target version changes, approval does not carry forward.

## Deployment Plan model

Phase 1 supports deployment plan preview creation only:

- selected update IDs
- affected instances
- exact target versions at plan creation
- blocker list
- status: `Ready` or `Awaiting Approval`

Execution is intentionally not implemented in Phase 1.

## Operation state machine

Reserved states:

- Queued
- Preflight
- Backing Up
- Waiting for Backup
- Installing
- Waiting for Restart
- Waiting for API
- Validating
- Succeeded
- Failed
- Blocked
- Manual Intervention Required

## Implemented scope

### Phase 1

- Authenticated app shell.
- Encrypted credential storage.
- Instance onboarding with server-side HA connectivity validation.
- Credential replacement without reveal.
- Fleet dashboard.
- Instance table.
- Update sync/discovery.
- Unified update table.
- Release detail drawer with sanitized content.
- Deterministic risk/critical classification.
- Deployment plan preview.
- Audit log.

### Phase 2+

- Approval records with exact target-version binding.
- Notification table and manual-required notifications.
- Job run records.
- Operation records for guarded update execution.
- Schedule records.
- Backup record table scaffold.
- Deterministic automation worker CLI.
- Hermes cron wrapper scheduled every 6 hours.
- Low-risk update execution path using `update.install` only after release-note review.
- Automatic exclusion for Core, HAOS, Supervisor, firmware, router, Zigbee, Z-Wave, Matter, and Thread classes.
- Vault documentation append for each successful install.

Remaining future hardening:

- Full backup capability detection/execution per HA installation type.
- Production HTTPS deployment and secure cookies.
- Rich UI controls for acknowledging notifications and editing schedules/policies.
- Postgres migrations instead of `create_all`.
- More precise release-note source adapters per ecosystem.
