# Architecture

## System shape

```text
Browser UI
  -> FastAPI Fleet Manager backend
    -> SQLAlchemy database
    -> CredentialService
    -> HomeAssistantAdapter
      -> configured Home Assistant instance APIs
```

Fleet Manager is a narrow control plane. It is not a generic reverse proxy and does not expose arbitrary Home Assistant API forwarding.

## Main components

| Component | Role |
|---|---|
| `fleet_manager/app.py` | FastAPI app, auth, UI/API routes |
| `fleet_manager/models.py` | SQLAlchemy data model |
| `fleet_manager/services/credentials.py` | encrypted token custody |
| `fleet_manager/services/ha_adapter.py` | Home Assistant REST/WebSocket/service interactions |
| `fleet_manager/services/automation.py` | deterministic monitor/automation worker |
| `fleet_manager/static/index.html` | single-file operator console |
| `ha_update_dashboard/` | legacy read-only dashboard prototype retained for lineage |

## Data model

Core tables include users, sessions, instances, instance credentials, update records, approvals, notifications, operations, backups, job runs, audit events, and policy/application settings.

## Request flow: update inventory

1. Operator opens **Updates**.
2. UI fetches core data in parallel: instances, updates, repairs.
3. Backend returns sanitized domain models.
4. UI filters out current/same-version updates and shows pending count badge.

## Request flow: add instance

1. Operator enters Home Assistant URL and long-lived access token.
2. Backend validates URL shape.
3. Backend calls Home Assistant server-side.
4. If valid, token is encrypted and stored.
5. Browser never sees the stored token again.

## Request flow: update execution

1. Operator confirms update.
2. Backend preflights exact entity/target version.
3. Backend calls `update.install` with safe payload.
4. Backend polls after transient failures when the update may restart an add-on/tunnel.
5. Operation and audit records capture outcome.
6. UI refreshes normalized update state.

## Request flow: repair reboot

1. Backend reads HA repair issues through WebSocket repair issue listing.
2. UI shows only known safe native action when Fleet Manager recognizes it.
3. Reboot action fires a HA shutdown event first, waits briefly, then calls Supervisor host reboot.
4. If no known native action exists, UI links to Home Assistant repairs instead of guessing.

## Performance model

Initial load fetches only instances, updates, and repairs. Recent activity and Settings lazy-load their own secondary data.

## Security invariants

- No generic proxy endpoint.
- Browser receives sanitized models only.
- Tokens are encrypted at rest.
- Runtime master key is injected through environment/secrets.
- High-risk update classes default to manual-required.
- Sensitive actions are confirmation-gated and audited.
