# Docker

## Compose

```bash
cp .env.example .env
# edit .env with real secrets
docker compose up -d --build
```

Open `http://127.0.0.1:8800`.

Required values:

```env
DATABASE_URL=sqlite:////data/fleet_manager.db
MASTER_ENCRYPTION_KEY=<urlsafe-base64-32-byte-key>
SESSION_SECRET=<long-random-secret>
FLEET_ADMIN_EMAIL=admin@example.local
FLEET_ADMIN_PASSWORD=<initial-admin-password>
APP_TIMEZONE=UTC
```

Generate secrets:

```bash
python3 - <<'PY'
import base64, os, secrets
print('MASTER_ENCRYPTION_KEY=' + base64.urlsafe_b64encode(os.urandom(32)).decode())
print('SESSION_SECRET=' + secrets.token_urlsafe(48))
PY
```

## Runtime data

Compose stores state in `fleet-manager-data:/data`. Back up that volume and the secret source used for `MASTER_ENCRYPTION_KEY`.

## Manual build

```bash
docker build -t ha-fleet-manager:local .
docker run --rm -p 8800:8800 --env-file .env -v ha-fleet-manager-data:/data ha-fleet-manager:local
```

## Health checks

```bash
curl -fsS http://127.0.0.1:8800/health/live
curl -fsS http://127.0.0.1:8800/health/ready
```

## Production notes

- Put HTTPS in front of the container.
- Do not bake `.env`, SQLite files, logs, or Home Assistant tokens into the image.
- Keep `.dockerignore` strict.
- Prefer container secrets or a secret manager over a plaintext production `.env` file.
