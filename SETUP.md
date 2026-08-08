# Setup

This is the local install path. Use [DOCKER.md](DOCKER.md) if you want to run it in a container.

## Requirements

- Python 3.12+
- Network access from Fleet Manager to each Home Assistant instance
- One Home Assistant long-lived access token per instance
- TLS reverse proxy for production use

## Install

```bash
git clone https://github.com/<owner>/ha-fleet-manager.git
cd ha-fleet-manager
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local
```

Generate the two required secrets:

```bash
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

## Run locally

```bash
set -a && . ./.env.local && set +a
uvicorn fleet_manager.app:app --host 127.0.0.1 --port 8799
```

Open `http://127.0.0.1:8799`.

## First login

Sign in with `FLEET_ADMIN_EMAIL` and `FLEET_ADMIN_PASSWORD`. Change the password in **Settings → Account**.

Optional OIDC login is configured in **Settings → OIDC**. Keep password login available until OIDC works end to end.

## Add instances

1. Go to **Settings → Instance config**.
2. Click **Add Instance**.
3. Enter a name, URL, environment, and long-lived Home Assistant token.
4. Save. Fleet Manager validates the token before storing it.

## Verify

```bash
curl -fsS http://127.0.0.1:8799/health/live
curl -fsS http://127.0.0.1:8799/health/ready
pytest -q
```

## Backup reminder

Back up the database and the secret source that contains `MASTER_ENCRYPTION_KEY`. You need both to restore encrypted instance tokens.
