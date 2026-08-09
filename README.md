# Home Assistant Fleet Manager

A small self-hosted web app for managing updates, repairs, backups, and restarts across multiple Home Assistant instances.

It gives one place to see what needs attention, run safe maintenance actions, and keep an audit trail. Home Assistant tokens stay on the server; the browser only sees sanitized data.

## What it does

- Tracks multiple Home Assistant instances.
- Shows pending `update.*` entities and hides rows that are already current.
- Lets an admin update or skip visible updates.
- Uses Home Assistant native backups when available.
- Shows Home Assistant Repairs.
- Supports instance backup, restart, sync, edit, and delete.
- Keeps audit, operation, backup, job, and notification history.
- Supports password login and optional OIDC login.
- Runs with Python or Docker.

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
python -c "import base64, os, secrets; print('MASTER_ENCRYPTION_KEY=' + base64.urlsafe_b64encode(os.urandom(32)).decode()); print('SESSION_SECRET=' + secrets.token_urlsafe(48))"
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

Open `http://127.0.0.1:8799`, sign in with the bootstrap admin account, then change the password in Settings.

## First setup

1. Open **Settings → Instance config**.
2. Click **Add Instance**.
3. Enter the instance name, URL, environment, and Home Assistant long-lived token.
4. Save. Fleet Manager validates the token before storing it.
5. Use **Fleet health**, **Repairs**, and **Recent activity** for day-to-day review.

Do not edit the SQLite database directly unless you are recovering a broken install.

## How updates work

If an update is visible, an admin can update or skip it. Fleet Manager checks the update entity and target version before installing. When Home Assistant exposes a native backup path, Fleet Manager tries that backup flow before the install. The result is logged either way.

## Docs

- [SETUP.md](SETUP.md) - local install and first login
- [DOCKER.md](DOCKER.md) - Docker and Compose
- [DEPLOYMENT.md](DEPLOYMENT.md) - systemd, reverse proxy, upgrades, rollback
- [SECURITY.md](SECURITY.md) - token handling and deployment notes
- [STANDARD_LINEAGE.md](STANDARD_LINEAGE.md) - project boundaries and public examples
- [docs/blog-post.md](docs/blog-post.md) - project story and use case
- [docs/demo/README.md](docs/demo/README.md) - static demo notes
- [docs/linkedin-blurb.md](docs/linkedin-blurb.md) - short launch blurb

## Useful commands

```bash
. .venv/bin/activate
pytest -q
python -m compileall fleet_manager ha_update_dashboard tests
curl -fsS http://127.0.0.1:8799/health/ready
```

Background worker:

```bash
python -m fleet_manager.automation_cli --dry-run
python -m fleet_manager.automation_cli --execute
```

Schedule the worker with systemd timers, cron, Kubernetes CronJob, or your platform scheduler. Keep logs and runtime files out of the repo.

## Production notes

```text
browser -> HTTPS reverse proxy -> Fleet Manager -> Home Assistant instances
```

Use HTTPS, strong admin auth or OIDC, and regular backups. Back up both the database and `MASTER_ENCRYPTION_KEY`; one without the other is not enough to restore encrypted Home Assistant tokens.

Do not commit real `.env` files, SQLite databases, logs, Home Assistant URLs, tokens, OAuth secrets, personal domains, or screenshots from a live environment.

## Repository layout

```text
fleet_manager/        FastAPI app and browser UI
ha_update_dashboard/  older read-only scanner/server code kept for compatibility
static/               legacy static prototype
deploy/               systemd and reverse proxy examples
tests/                pytest suite
```
