# Publishing Manifest

Use this checklist before pushing to a public GitHub branch.

## Include

- `README.md`
- `SETUP.md`
- `DEPLOYMENT.md`
- `DOCKER.md`
- `SECURITY.md`
- `ARCHITECTURE.md`
- `STANDARD_LINEAGE.md`
- `.env.example`
- `.gitignore`
- `.dockerignore`
- `Dockerfile`
- `docker-compose.yml`
- `fleet_manager/`
- `ha_update_dashboard/` legacy source
- `deploy/` examples with placeholder paths/domains
- `docs/` sanitized supporting docs
- `tests/`
- `requirements.txt`
- `pyproject.toml`

## Exclude

- `.env`, `.env.local`, and real env files
- `config.local.json`
- `data/`
- SQLite DBs
- logs
- `.venv/`
- `.pytest_cache/`
- `hafm-test-*/`
- personal domains and local IPs
- real Home Assistant tokens
- OAuth client IDs/secrets
- personal/family/property names
- generated screenshots that show real environments

## Required scans

```bash
git status --short
python -m compileall fleet_manager ha_update_dashboard tests
pytest -q
```

Privacy scan terms should include project-specific private terms plus common sensitive markers: personal domains, local IP ranges, operator emails, OAuth client IDs, Home Assistant tokens, and runtime SQLite data.

## Branch policy

Publish work to `dev` first. Merge to `main` only after tests pass, privacy scan passes, runtime state is excluded, docs match actual behavior, and remote branch tip is verified after push.
