# Architecture

Fleet Manager is a narrow admin control plane for Home Assistant fleets.

```text
Browser
  -> FastAPI Fleet Manager
    -> SQLAlchemy database
    -> encrypted credential service
    -> HomeAssistantAdapter
      -> configured Home Assistant instances
```

It is not a generic Home Assistant proxy. The backend owns the Home Assistant API paths and the browser only receives sanitized data.

## Main components

| Component | Role |
|---|---|
| `fleet_manager/app.py` | FastAPI app, auth, UI/API routes |
| `fleet_manager/models.py` | SQLAlchemy models |
| `fleet_manager/services/credentials.py` | encrypted token storage |
| `fleet_manager/services/ha_adapter.py` | Home Assistant REST/WebSocket/service calls |
| `fleet_manager/services/automation.py` | monitor, backup, update, skip, policy logic |
| `fleet_manager/static/index.html` | operator UI |
| `ha_update_dashboard/` | older read-only prototype kept for reference |

## Data model

The app stores users, sessions, instances, encrypted credentials, update records, approvals, notifications, operations, backups, job runs, audit events, policy settings, and schedules.

SQLite works for a small self-hosted install. A larger fleet should move to a managed database and migrations.

## Request flows

### Add instance

1. Admin enters the Home Assistant URL and long-lived token.
2. Backend validates the URL.
3. Backend calls Home Assistant with the supplied token.
4. If validation works, Fleet Manager encrypts and stores the token.
5. Existing tokens are never shown back to the browser.

### Update inventory

1. UI requests instances, updates, and repairs.
2. Backend normalizes `update.*` entities.
3. Rows where current and available versions match are hidden.
4. The UI shows the remaining actionable updates.

### Update or skip

1. Admin confirms the action in the UI.
2. Backend checks the update entity and target version.
3. For update, Fleet Manager tries a native Home Assistant backup path when available, then calls `update.install`.
4. For skip, Fleet Manager calls `update.skip` for the entity.
5. Operation and audit records capture the result.

### Backup and restart

Instance backup uses Home Assistant backup services in the safest known order for the instance. Restart and repair reboot actions are audited and mark the instance as restarting so the UI does not look idle while Home Assistant is going down.

## Security invariants

- No generic proxy endpoint.
- No stored Home Assistant token is sent to the browser.
- Tokens are encrypted at rest.
- The master key comes from the runtime environment or secret manager.
- Mutating API calls require CSRF protection.
- Sensitive actions are confirmed and audited.
- The adapter does not follow redirects with credentials.
