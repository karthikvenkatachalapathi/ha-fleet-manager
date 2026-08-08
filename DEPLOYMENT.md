# Deployment

Recommended production shape:

```text
browser -> HTTPS reverse proxy -> Fleet Manager -> Home Assistant instances
```

Keep Fleet Manager bound to localhost or a private interface when you can. Let the reverse proxy handle TLS and access controls.

## Native systemd install

Example path: `/opt/ha-fleet-manager`.

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

Generate and edit secrets as shown in [SETUP.md](SETUP.md).

Install the user service example:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/home-assistant-fleet-manager.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now home-assistant-fleet-manager.service
```

Check it:

```bash
systemctl --user status home-assistant-fleet-manager.service
curl -fsS http://127.0.0.1:8799/health/ready
```

## Reverse proxy

See [deploy/reverse-proxy.md](deploy/reverse-proxy.md). At minimum, pass these headers:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

## Upgrade

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

## Backup

Back up:

- runtime database
- `MASTER_ENCRYPTION_KEY`
- `SESSION_SECRET`
- reverse proxy config
- service unit overrides

The database by itself is not enough. You need the matching encryption key to recover stored Home Assistant tokens.

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

- [ ] Use HTTPS.
- [ ] Restrict the UI to trusted operators.
- [ ] Use a strong admin password or OIDC.
- [ ] Keep `.env.local` out of Git.
- [ ] Back up the database and encryption key.
- [ ] Monitor `/health/ready`.
- [ ] Alert on failed sync, backup, update, and auth events.
- [ ] Rotate Home Assistant tokens when custody changes.
