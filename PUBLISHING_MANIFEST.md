# Publishing checklist

Use this before pushing a public branch.

## Include

- source: `fleet_manager/`, `ha_update_dashboard/`
- tests: `tests/`
- deployment examples: `deploy/`, `Dockerfile`, `docker-compose.yml`
- docs: `README.md`, `SETUP.md`, `DEPLOYMENT.md`, `DOCKER.md`, `SECURITY.md`, `STANDARD_LINEAGE.md`
- config examples: `.env.example`, `.gitignore`, `.dockerignore`, `requirements.txt`, `pyproject.toml`

## Exclude

- `.env`, `.env.local`, and real env files
- `config.local.json`
- `data/`
- SQLite databases
- logs
- `.venv/`
- `.pytest_cache/`
- `hafm-test-*/`
- real domains, local IPs, emails, tokens, OAuth IDs/secrets, property names, or live screenshots

## Required checks

```bash
git status --short
python -m compileall fleet_manager ha_update_dashboard tests
pytest -q
```

Run a privacy scan for private domains, local IP ranges, real emails, OAuth IDs, Home Assistant tokens, SQLite files, and generated runtime state.

## Branch policy

Push to `dev` first. Merge to `main` only after tests pass, the privacy scan is clean, docs match current behavior, and the remote tip is verified.
