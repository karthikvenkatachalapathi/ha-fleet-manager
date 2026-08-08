# Security

## Trust boundary

```text
Browser -> Fleet Manager API -> encrypted credential service -> Home Assistant API
```

Stored Home Assistant tokens stay on the server. The UI never renders them back after save.

## Credential storage

Home Assistant tokens are encrypted with AES-256-GCM. `MASTER_ENCRYPTION_KEY` supplies the key and is not stored in the database.

Back up the database and key source together. Do not put either one in a public repo.

## URL and proxy controls

Fleet Manager rejects instance URLs that are not HTTP(S), rejects URLs with embedded credentials, and does not expose a generic proxy endpoint. Home Assistant API paths are chosen by the backend, not by the browser.

The Home Assistant adapter does not follow redirects when sending credentials.

## Session security

- Argon2 password hashing
- server-side sessions
- HttpOnly SameSite cookies
- CSRF header for mutating requests
- audit events for login, logout, failure, and sensitive actions
- optional OIDC login for admin access

Use HTTPS in production.

## Do not commit

- `.env`, `.env.local`, or real env files
- `config.local.json`
- `data/`
- `*.db`, `*.sqlite`, `*.sqlite3`
- logs and generated runtime/test folders
- real Home Assistant URLs or tokens
- OAuth client IDs or secrets
- personal domains, personal emails, local IPs, family names, or property names

## Token rotation

Rotate a Home Assistant token when an operator leaves, a host may have been exposed, backup custody is uncertain, or a token was pasted into the wrong place.

Replace the token in **Settings → Instance config → Edit Instance**. Leaving the token field blank keeps the existing encrypted token.

## Public bug reports

Redact instance names, domains, local IPs, tokens, email addresses, audit rows with private environment details, and raw SQLite data.
