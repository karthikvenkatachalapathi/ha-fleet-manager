<div align="center">

# Home Assistant Fleet Manager

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Control%20Plane-009688.svg)](https://fastapi.tiangolo.com/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Fleet%20Updates-41BDF5.svg)](https://www.home-assistant.io/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)](DOCKER.md)
[![Security](https://img.shields.io/badge/Security-Token%20Custody-2EA44F.svg)](SECURITY.md)

*A self-hosted control plane for monitoring and safely operating updates across multiple Home Assistant instances.*

</div>

<div align="center">
<table><tr>
<td align="center"><b>Start</b><br><sub><a href="#quick-start">Quick Start</a> · <a href="SETUP.md">Setup</a><br><a href="DOCKER.md">Docker</a> · <a href="DEPLOYMENT.md">Deployment</a></sub></td>
<td align="center"><b>Fleet</b><br><sub><a href="#features">Features</a> · <a href="#home-assistant-safety-model">Safety Model</a><br><a href="#update-policy">Update Policy</a> · <a href="#repairs-and-admin-actions">Repairs</a></sub></td>
<td align="center"><b>Govern</b><br><sub><a href="SECURITY.md">Security</a> · <a href="#credential-custody">Credential Custody</a><br><a href="#audit-and-operations">Audit</a> · <a href="#privacy-boundary">Privacy</a></sub></td>
<td align="center"><b>Operate</b><br><sub><a href="#admin-ui">Admin UI</a> · <a href="#automation-worker">Automation</a><br><a href="#local-verification">Tests</a> · <a href="#observability">Observability</a></sub></td>
<td align="center"><b>Design</b><br><sub><a href="ARCHITECTURE.md">Architecture</a> · <a href="STANDARD_LINEAGE.md">Lineage</a><br><a href="PUBLISHING_MANIFEST.md">Publishing</a> · <a href="#roadmap-direction">Roadmap</a></sub></td>
</tr></table>
</div>

---

## Overview

Home Assistant exposes update entities, repair issues, backup services, and restart/reboot controls. Those surfaces are powerful, but operating them across several homes, labs, rentals, or remote installations can become noisy and risky.

Home Assistant Fleet Manager adds a narrow operator control plane between trusted admins and Home Assistant instances:

- one authenticated UI for multiple Home Assistant instances
- encrypted long-lived token custody on the backend
- normalized update inventory across `update.*` entities
- risk classification for Core, OS, Supervisor, firmware, network, radio, and add-on updates
- confirmation gates for updates, skips, backups, restarts, and repair reboots
- audit events, operation records, backup records, and notifications
- optional automation worker for low-risk update handling

The browser never receives stored Home Assistant tokens and never calls Home Assistant directly. Fleet Manager is intentionally a control plane, not a generic proxy.

---

## Credits and lineage

This project follows the same public-repository documentation and operator-first style used for the Google Workspace Governance Gateway: clear setup path, explicit custody boundaries, day-2 UI administration, security notes, deployment paths, and publishing hygiene.

It is not affiliated with or endorsed by Nabu Casa or the Home Assistant project. Home Assistant names, APIs, and brands belong to their respective owners. This project consumes documented Home Assistant API surfaces and keeps the operator in control of risky actions.

See [STANDARD_LINEAGE.md](STANDARD_LINEAGE.md) for the full design lineage and boundary statement.

---

## Features

<table><tr><td valign="top" width="50%">

**Fleet inventory** — multiple Home Assistant instances with environment, location, status, and tags<br>
**Update center** — normalized update table with versions, entity IDs, categories, risk, and actions<br>
**Repairs** — conditional Repairs page with count badge and native actions where safe<br>
**Admin actions** — confirmation-gated backup, restart, skip, update, and selected repair reboot flows<br>
**Recent activity** — compact activity feed plus audit, notification, backup, job, and operation detail<br>
**Settings** — account, OIDC, and instance configuration in an admin-console UI

</td><td valign="top" width="50%">

**Token custody** — HA tokens encrypted at rest with runtime-injected master key<br>
**Policy guardrails** — Core/OS/Supervisor/firmware/radio/network classes stay manual by default<br>
**Execution validation** — update post-failure polling tolerates tunnel/add-on restarts before marking state<br>
**Audit evidence** — sensitive actions produce backend audit/operation records<br>
**Fast UI loading** — core landing data loads in parallel; activity/settings load lazily<br>
**Docker-ready** — Compose and standalone Docker deployment path

</td></tr></table>

---

## Quick Start

```bash
git clone https://github.com/<owner>/ha-fleet-manager.git
cd ha-fleet-manager
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local
```

Generate a local encryption key:

```bash
python - <<'PY'
import base64, os
print(base64.urlsafe_b64encode(os.urandom(32)).decode())
PY
```

Edit `.env.local`:

```env
DATABASE_URL=sqlite:///data/fleet_manager.db
MASTER_ENCRYPTION_KEY=<urlsafe-base64-32-byte-key>
SESSION_SECRET=<long-random-secret>
FLEET_ADMIN_EMAIL=admin@example.local
FLEET_ADMIN_PASSWORD=<initial-admin-password>
APP_TIMEZONE=UTC
```

Run locally:

```bash
set -a && . ./.env.local && set +a
uvicorn fleet_manager.app:app --host 0.0.0.0 --port 8799
```

Open `http://127.0.0.1:8799`. Change the bootstrap admin password in **Settings → Account** after first login.

---

## Normal setup flow

1. Install with [SETUP.md](SETUP.md) or [DOCKER.md](DOCKER.md).
2. Open Fleet Manager and sign in as the bootstrap admin.
3. Go to **Settings → Instance config**.
4. Add a Home Assistant instance URL and a long-lived access token.
5. Fleet Manager validates the connection server-side before saving.
6. Review **Updates** for pending update entities.
7. Review **Repairs** if the nav badge appears.
8. Use **Recent activity** for audit, notification, backup, job, and operation history.

Day-2 operations are UI-managed. Local files are for install-time secrets, runtime state, and recovery only.

---

## Home Assistant safety model

| Area | Default behavior |
|---|---|
| Browser token exposure | Never exposes stored HA tokens |
| Instance URL handling | Accepts only `http`/`https`; rejects credentials embedded in URLs |
| Generic proxying | No generic `/api/proxy` endpoint |
| Redirect handling | HA API client does not follow redirects with credentials |
| Core/OS/Supervisor/firmware/radio/network updates | Manual-required by default |
| Add-on/custom integration updates | Eligible for review and scoped automation only when policy allows |
| Destructive/admin actions | Confirmation-gated and audited |

---

## Update policy

Fleet Manager normalizes Home Assistant update entities into instance, entity ID, component/category, versions, release metadata, risk level, manual-action state, installation/skip state, and policy explanation.

| Update type | Default posture |
|---|---|
| Home Assistant Core | Manual-required |
| HAOS / Supervisor | Manual-required |
| Firmware / ESPHome / device radio stacks | Manual-required |
| Router / network / Zigbee / Z-Wave / Matter / Thread | Manual-required |
| Add-ons / custom integrations | Reviewable; may be eligible for scoped automation |
| Skipped updates | Disabled install button until re-enabled in Home Assistant |

---

## Repairs and admin actions

Fleet Manager reads Repairs through the Home Assistant WebSocket repair issue surface. When repair issues exist, the left nav shows a **Repairs** item with a count badge.

Native Fleet Manager repair actions are intentionally limited. Example: reboot/restart repairs are shown as **Reboot**, confirmation-gated, and call Home Assistant in a sequence designed to let shutdown automations run before Supervisor host reboot.

If Fleet Manager does not know a safe native action, it links to Home Assistant repair details instead of guessing.

---

## Credential custody

| Variable | Purpose |
|---|---|
| `MASTER_ENCRYPTION_KEY` | URL-safe base64 encoded 32-byte AES-GCM key |
| `SESSION_SECRET` | server-side session signing/entropy |
| `FLEET_ADMIN_EMAIL` | first admin bootstrap email |
| `FLEET_ADMIN_PASSWORD` | first admin bootstrap password |
| `DATABASE_URL` | SQLite or future database URL |
| `APP_TIMEZONE` | UI/scheduler timezone label |

Never commit `.env.local`, `.env`, runtime databases, logs, caches, or real Home Assistant tokens.

---

## Admin UI

- **Updates** — pending update review and update/skip actions
- **Repairs** — conditional page when Home Assistant reports repairs
- **Recent activity** — operational timeline and detail tables
- **Settings** — Account, OIDC, and Instance config tabs

---

## Automation worker

```bash
set -a && . ./.env.local && set +a
. .venv/bin/activate
python -m fleet_manager.automation_cli --dry-run
python -m fleet_manager.automation_cli --execute
```

Use an external scheduler such as systemd timers, cron, Kubernetes CronJob, or a platform scheduler. Keep scheduler-specific scripts and logs outside the public repository.

---

## Docker and deployment

- [DOCKER.md](DOCKER.md) covers Compose and container deployment.
- [DEPLOYMENT.md](DEPLOYMENT.md) covers native systemd, reverse proxy, TLS, backup, upgrade, and rollback guidance.

The recommended production shape is:

```text
browser -> TLS reverse proxy -> Fleet Manager -> Home Assistant instances
```

---

## API endpoints

| Endpoint | Purpose |
|---|---|
| `/health/live` | process liveness |
| `/health/ready` | database readiness |
| `/api/auth/login` / `/api/auth/logout` | session auth |
| `/api/session` | current user/session/CSRF |
| `/api/instances` | instance configuration and status |
| `/api/updates` | update inventory |
| `/api/repairs` | Home Assistant repairs |
| `/api/notifications` | operator notifications |
| `/api/operations` | update/admin operation records |
| `/api/backups` | backup operation records |
| `/api/audit` | audit events |

There is intentionally no generic proxy endpoint.

---

## Observability

Minimum recommended observability: service logs, `/health/live`, `/health/ready`, database/secret backups, and alerts for failed sync, failed backup, blocked update, failed automation run, and auth failures.

---

## Local verification

```bash
. .venv/bin/activate
pytest -q
python -m compileall fleet_manager ha_update_dashboard tests
```

---

## Privacy boundary

Public examples must use placeholders such as `admin@example.local`, `https://homeassistant.example.com`, and `http://<server-lan-ip>:8799`.

Do not publish real domains, local IPs, Home Assistant URLs, family names, customer/property names, OAuth client IDs, tokens, runtime SQLite databases, logs, or generated test state. See [PUBLISHING_MANIFEST.md](PUBLISHING_MANIFEST.md).

---

## Repository layout

```text
fleet_manager/                 Production Fleet Manager app
ha_update_dashboard/           Legacy read-only dashboard prototype
deploy/                        systemd and reverse-proxy examples
docs/                          Supporting docs
tests/                         pytest suite
```

---

## Roadmap direction

- richer capability detection for backups and Supervisor features
- database migrations instead of `create_all`
- optional Postgres deployment
- first-class metrics export
- stronger per-instance certificate policy controls
- expanded release-note source adapters
- configurable automation policy from the UI
- optional approval workflows for high-risk operations
