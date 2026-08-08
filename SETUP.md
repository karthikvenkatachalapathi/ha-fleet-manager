# Setup Guide

This guide separates one-time installation from day-2 UI administration.

## Requirements

- Python 3.12+
- Linux, macOS, or a container host
- Network access from Fleet Manager to each Home Assistant instance
- A Home Assistant long-lived access token per managed instance
- TLS reverse proxy for production browser access

## Clone

```bash
git clone https://github.com/<owner>/ha-fleet-manager.git
cd ha-fleet-manager
```

## Python environment

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Runtime secrets

```bash
cp .env.example .env.local
python - <<'PY'
import base64, os, secrets
print('MASTER_ENCRYPTION_KEY=' + base64.urlsafe_b64encode(os.urandom(32)).decode())
print('SESSION_SECRET=' + secrets.token_urlsafe(48))
PY
```

Edit `.env.local`:

```env
DATABASE_URL=sqlite:///data/fleet_manager.db
MASTER_ENCRYPTION_KEY=<generated-key>
SESSION_SECRET=<generated-secret>
FLEET_ADMIN_EMAIL=admin@example.local
FLEET_ADMIN_PASSWORD=<initial-password>
APP_TIMEZONE=UTC
```

## Start locally

```bash
set -a && . ./.env.local && set +a
uvicorn fleet_manager.app:app --host 127.0.0.1 --port 8799
```

Open `http://127.0.0.1:8799`.

## First login

Use `FLEET_ADMIN_EMAIL` and `FLEET_ADMIN_PASSWORD`, then change account settings in the UI.

## Add instances

1. Go to **Settings → Instance config**.
2. Click **Add Instance**.
3. Enter a friendly name, URL, environment, and long-lived Home Assistant token.
4. Fleet Manager validates the token and stores it encrypted.

## Day-2 operations

Use the UI for adding/editing/deleting instances, OIDC configuration, account settings, update review, repair review, backups/restarts, and notification acknowledgement.

Do not edit runtime SQLite data directly unless performing recovery.

## Verify

```bash
curl -fsS http://127.0.0.1:8799/health/live
curl -fsS http://127.0.0.1:8799/health/ready
pytest -q
```
