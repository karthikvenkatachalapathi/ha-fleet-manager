# Deployment Guide

## Recommended production topology

```text
browser -> HTTPS reverse proxy -> Fleet Manager -> Home Assistant instances
```

Use the reverse proxy for TLS, public DNS, access controls, and secure cookies. Keep Fleet Manager bound to `127.0.0.1` when possible.

## Native systemd deployment

Example target path: `/opt/ha-fleet-manager`.

```bash
sudo mkdir -p /opt/ha-fleet-manager
sudo chown -R "$USER":"$USER" /opt/ha-fleet-manager
git clone https://github.com/<owner>/ha-fleet-manager.git /opt/ha-fleet-manager
cd /opt/ha-fleet-manager
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local
```

Create secrets as described in [SETUP.md](SETUP.md).

Install user service example:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/home-assistant-fleet-manager.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now home-assistant-fleet-manager.service
```

Check:

```bash
systemctl --user status home-assistant-fleet-manager.service
curl -fsS http://127.0.0.1:8799/health/ready
```

## Reverse proxy

See `deploy/reverse-proxy.md` for a generic Nginx-style example.

Minimum headers:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

## Upgrades

```bash
cd /opt/ha-fleet-manager
git fetch origin
git checkout dev
git pull --ff-only
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
systemctl --user restart home-assistant-fleet-manager.service
curl -fsS http://127.0.0.1:8799/health/ready
```

## Backups

Back up runtime database, encrypted credential rows, `.env.local` or secret-manager values, and reverse-proxy configuration. The encrypted database is not enough by itself; the matching `MASTER_ENCRYPTION_KEY` is required for recovery.

## Rollback

```bash
cd /opt/ha-fleet-manager
git log --oneline -10
git checkout <known-good-commit>
. .venv/bin/activate
pip install -r requirements.txt
systemctl --user restart home-assistant-fleet-manager.service
curl -fsS http://127.0.0.1:8799/health/ready
```

## Hardening checklist

- [ ] Serve through HTTPS.
- [ ] Restrict UI access to trusted networks/users.
- [ ] Use strong admin password or OIDC.
- [ ] Store `.env.local` outside Git and back it up securely.
- [ ] Monitor `/health/ready`.
- [ ] Alert on failed sync, failed backups, failed update operations, and auth failures.
- [ ] Rotate Home Assistant tokens when operator or host custody changes.
