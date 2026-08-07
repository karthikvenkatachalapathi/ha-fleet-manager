# Home Assistant Fleet Manager

Production-oriented self-hosted control plane for managing a fleet of Home Assistant instances.

The original read-only dashboard remains in `ha_update_dashboard/`. The new control-plane implementation lives in `fleet_manager/`.

## Current Phase 1 capabilities

- Authenticated Fleet Manager UI/API.
- Argon2 password hashing and server-side sessions.
- HttpOnly SameSite cookies and CSRF protection.
- Admin / Operator / Read Only permission boundaries.
- AES-256-GCM encrypted Home Assistant credential storage.
- Runtime-injected `MASTER_ENCRYPTION_KEY`; encryption key is not stored in the DB.
- Server-side Home Assistant connectivity validation.
- Instance onboarding and credential replacement without revealing existing credentials.
- Home Assistant update discovery from `update.*` entities.
- Unified update center.
- Deterministic critical/risk classification.
- Deployment Plan preview with exact target-version binding and blockers.
- Audit log.
- Compact admin UI.

Phase 1 is visibility/planning only. It does **not** execute updates yet.

## Development quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local
# edit .env.local with real local-only secrets
set -a && . ./.env.local && set +a
uvicorn fleet_manager.app:app --host 0.0.0.0 --port 8799
```

Open:

```text
http://127.0.0.1:8799
http://<server-lan-ip>:8799
```

## Required secrets

```text
MASTER_ENCRYPTION_KEY   urlsafe base64 encoded 32-byte key
SESSION_SECRET          long random string
FLEET_ADMIN_EMAIL       initial admin email
FLEET_ADMIN_PASSWORD    initial admin password, only used to bootstrap first user
```

Never commit `.env.local`, `data/`, `.venv/`, or real Home Assistant credentials.

Generate a local master key:

```bash
python - <<'PY'
import base64, os
print(base64.urlsafe_b64encode(os.urandom(32)).decode())
PY
```

## Docker Compose

```bash
cp .env.example .env
# edit .env with production secrets or inject via Docker secrets/environment
compose up -d --build
```

## API endpoints

- `/health/live`
- `/health/ready`
- `/api/auth/login`
- `/api/auth/logout`
- `/api/session`
- `/api/dashboard`
- `/api/instances`
- `/api/updates`
- `/api/deployments`
- `/api/audit`

There is intentionally no generic `/api/proxy` endpoint.

## Security model

Browser:

- never receives stored HA tokens
- never calls Home Assistant directly
- receives sanitized Fleet Manager domain models only

Backend:

- stores HA tokens encrypted with AES-256-GCM
- sends HA credentials only to configured instance URLs
- rejects non-http(s) instance URLs
- rejects credentials embedded in URLs
- does not follow redirects for HA API calls
- disables ambient proxy use for HA API calls

## Tests

```bash
. .venv/bin/activate
pytest -q
```

Expected current result:

```text
8 passed
```

## Architecture

See:

```text
docs/architecture.md
```

## Legacy read-only dashboard

The old dashboard can still be run with:

```bash
python3 -m ha_update_dashboard.server --host 0.0.0.0 --port 8799
```

Do not run it on the same port as Fleet Manager.
