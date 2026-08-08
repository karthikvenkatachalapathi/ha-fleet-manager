# Security Guide

## Trust boundary

```text
Browser -> Fleet Manager API -> encrypted credential service -> Home Assistant API
```

The browser never receives stored Home Assistant tokens. Existing tokens are never rendered back to the UI.

## Credential storage

Home Assistant tokens are encrypted at rest using AES-256-GCM. The encryption key is supplied by `MASTER_ENCRYPTION_KEY` and is not stored in the database.

Back up the database and the master key source together, but never in the same public Git repository.

## URL safety controls

Fleet Manager rejects non-HTTP(S) instance URLs, URLs with embedded credentials, and caller-controlled proxy paths. The Home Assistant adapter owns the API paths and disables ambient proxy/redirect behavior where credential leakage would be risky.

## Session security

- Argon2 password hashing
- server-side sessions
- HttpOnly SameSite cookies
- CSRF token header for mutating requests
- audit events for login/logout/failure and sensitive actions

Use HTTPS in production so cookies are protected in transit.

## Sensitive files that must not be committed

- `.env`, `.env.local`, `.env.*` except `.env.example`
- `config.local.json`
- `data/`
- `*.db`, `*.sqlite`, `*.sqlite3`
- logs and generated test/runtime directories
- real Home Assistant URLs/tokens
- OAuth client IDs/secrets
- personal domains, personal emails, local IPs, family/property names

## Token rotation

Rotate a Home Assistant token when an operator leaves, a runtime host is suspected of exposure, backup custody is uncertain, or a token was pasted into the wrong system.

Replace the token through **Settings → Instance config → Edit Instance**. Leaving the token field blank preserves the existing encrypted token.

## Public issue reports

Redact instance names, domains, local IPs, tokens, secrets, email addresses, audit rows containing personal environment details, and raw SQLite data.
