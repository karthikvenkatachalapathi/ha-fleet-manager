# Home Assistant Fleet Manager

A small self-hosted admin console for managing updates, repairs, backups, and restarts across multiple Home Assistant instances.

Fleet Manager is built for trusted operators. It keeps Home Assistant tokens on the server, shows a clean update queue, and records what happened after someone clicks update, skip, backup, or restart.

## What it does

- Tracks multiple Home Assistant instances from one UI.
- Shows pending `update.*` entities and hides rows where current and available versions match.
- Lets an admin run or skip any visible update from the UI.
- Uses Home Assistant's native backup flow when an update or operator action supports it.
- Shows Repairs when Home Assistant reports repair issues.
- Supports instance backup, restart, sync, edit, and delete actions.
- Keeps audit, operation, backup, job, and notification history.
- Supports password login and optional OIDC login.
- Works as a native Python service or a Docker container.

The browser does not receive stored Home Assistant tokens. The backend owns all Home Assistant calls.

## Quick start

```bash
git clone https://github.com/<owner>/ha-fleet-manager.git
cd ha-fleet-manager
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local
```

Generate secrets:

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

Run it:

```bash
set -a && . ./.env.local && set +a
uvicorn fleet_manager.app:app --host 127.0.0.1 --port 8799
```

Open `http://127.0.0.1:8799` and sign in with the bootstrap admin account. Change the password in Settings after first login.

## Normal setup

1. Go to **Settings → Instance config**.
2. Click **Add Instance**.
3. Enter the instance name, URL, environment, and Home Assistant long-lived token.
4. Fleet Manager validates the token before saving it.
5. Review updates, repairs, backups, and recent activity from the UI.

Use the UI for day-to-day administration. Do not edit the SQLite database directly unless you are doing recovery work.

## Update behavior

Fleet Manager shows all actionable update rows and lets an admin decide what to do. It no longer blocks updates only because they were previously marked manual-review. That makes the UI simpler: if an update is visible, an admin can update it or skip it.

Before installing an update, Fleet Manager checks the entity and target version. If Home Assistant exposes a native backup path, Fleet Manager tries that backup flow before the install. The action is logged either way.

## Main docs

- [SETUP.md](SETUP.md) - local install and first login
- [DOCKER.md](DOCKER.md) - Docker and Compose
- [DEPLOYMENT.md](DEPLOYMENT.md) - systemd, reverse proxy, upgrades, rollback
- [SECURITY.md](SECURITY.md) - token custody and safe deployment notes
- [ARCHITECTURE.md](ARCHITECTURE.md) - current app shape

## Useful commands

```bash
. .venv/bin/activate
pytest -q
python -m compileall fleet_manager ha_update_dashboard tests
curl -fsS http://127.0.0.1:8799/health/ready
```

Automation worker:

```bash
python -m fleet_manager.automation_cli --dry-run
python -m fleet_manager.automation_cli --execute
```

Schedule the worker with systemd timers, cron, Kubernetes CronJob, or your platform scheduler. Keep scheduler logs and local runtime files out of the repo.

## Production notes

Recommended shape:

```text
browser -> HTTPS reverse proxy -> Fleet Manager -> Home Assistant instances
```

Use HTTPS, strong admin auth or OIDC, and regular backups. Back up both the database and the `MASTER_ENCRYPTION_KEY`; one without the other is not enough to restore encrypted Home Assistant tokens.

Do not commit real `.env` files, SQLite databases, logs, Home Assistant URLs, tokens, OAuth secrets, personal domains, or screenshots from a live environment.

## Repository layout

```text
fleet_manager/        FastAPI app and single-file operator UI
ha_update_dashboard/  legacy read-only prototype kept for reference
deploy/               systemd and reverse proxy examples
docs/                 supporting docs
tests/                pytest suite
```

## Status

This branch is intended for operator testing before merging to `main`. Publish to `dev`, verify tests and privacy checks, then promote once the runtime behavior is stable.
