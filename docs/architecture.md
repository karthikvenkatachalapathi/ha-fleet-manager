# Architecture notes

The repo started as a read-only dashboard under `ha_update_dashboard/`. That prototype is still here for reference. The active app is under `fleet_manager/`.

## Home Assistant API surface

Fleet Manager uses these Home Assistant surfaces:

- `GET /api/config` for auth, reachability, version, and location metadata
- `GET /api/states` for update entity inventory
- `POST /api/services/update/install` for update installs
- `POST /api/services/update/skip` for skipped updates
- Home Assistant backup services where available
- restart/reboot services where available
- WebSocket `repairs/list_issues` for Repairs

Update entities usually expose:

- `installed_version`
- `latest_version`
- `release_url`
- `title`
- `friendly_name`
- `in_progress`
- `skipped_version`

Release notes are often just links. Fleet Manager does not promise automatic breaking-change detection or rollback.

## Trust boundary

```text
Browser
  -> Fleet Manager backend
    -> encrypted credential service
    -> Home Assistant adapter
      -> configured Home Assistant instance
```

The browser never receives stored Home Assistant tokens. It only submits a token while adding or replacing an instance credential.

## Credentials

`CredentialService` encrypts Home Assistant tokens with AES-256-GCM. `MASTER_ENCRYPTION_KEY` must decode to 32 bytes and stays outside the database. Ciphertext is bound to the instance ID.

Credential rows include provider fields so a future version can use Vault, 1Password, cloud secret managers, or Kubernetes secrets.

## Authentication and authorization

Current app behavior:

- password login with Argon2 hashing
- optional OIDC login
- server-side sessions
- HttpOnly SameSite cookies
- CSRF token for mutating calls
- role/permission checks
- audit rows for auth and sensitive actions

Production should run behind HTTPS.

## Credential leak controls

- only HTTP(S) instance URLs are accepted
- URLs with embedded credentials are rejected
- API paths are backend-owned
- no generic proxy endpoint exists
- redirects are not followed with credentials
- UI responses use sanitized models

## Update policy

The current UI lets an admin update or skip any visible pending update. The assumption is that the admin has reviewed release notes and owns the decision.

Fleet Manager still records risk/category metadata for visibility and future policy controls. Rows where current and available versions match are not shown in the main update table.

## Operations

Update, skip, backup, restart, repair reboot, sync, and OIDC/settings changes are written to audit or operation history. Instance restart/reboot paths mark the instance as restarting so the UI does not look idle while Home Assistant goes down.

## Future work

- real migrations instead of `create_all`
- optional Postgres support
- richer backup capability detection
- metrics export
- stronger certificate policy controls per instance
- better release-note adapters
- UI-managed automation policy
