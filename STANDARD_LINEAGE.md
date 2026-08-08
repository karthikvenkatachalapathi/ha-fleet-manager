# Standard Lineage

## Purpose

Home Assistant Fleet Manager follows the public-repository standard used for the Google Workspace Governance Gateway:

- operator-first README
- explicit setup and day-2 administration split
- clear credential custody boundary
- security and publishing hygiene documents
- native and Docker deployment paths
- admin-console UI language
- public examples with placeholders only

## Upstream and ecosystem relationship

This project uses public Home Assistant API concepts: REST config/state endpoints, `update.*` entities, service calls such as `update.install`, `update.skip`, backup services, restart/reboot controls, and WebSocket repairs issue listing.

It is not a Home Assistant add-on, not a Nabu Casa product, and not a replacement for the Home Assistant UI. It is an external fleet operator console.

## Design inheritance

| Standard | Applied here |
|---|---|
| Governance Gateway docs style | README badges, navigation table, setup/deploy/security split |
| Admin-only control plane | Fleet Manager UI is for trusted operators |
| Credential custody | HA tokens stay backend-side and encrypted |
| UI-authoritative operations | Instances/OIDC/account actions are managed in the browser UI |
| Publishing hygiene | no runtime DBs, local URLs, personal domains, or real tokens |
| Operational evidence | audit, operations, jobs, backups, notifications |

## Non-goals

- Generic Home Assistant proxy
- Full Home Assistant replacement UI
- Automatic Core/OS/Supervisor updates by default
- Guaranteed rollback
- Public hosted SaaS
- Credential sharing between users or profiles

## Naming

Use neutral public examples: `home`, `lab`, `rental`, `remote`, `admin@example.local`, `https://homeassistant.example.com`, and `http://<server-lan-ip>:8799`.

Avoid personal names, private domains, property names, local IPs, and live operator emails in docs, tests, screenshots, and seed data.
