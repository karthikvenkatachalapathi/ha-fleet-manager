# Project boundaries

Fleet Manager uses public Home Assistant API surfaces: REST config/state endpoints, `update.*` entities, service calls such as `update.install` and `update.skip`, backup services, restart/reboot controls, and WebSocket repair issue listing.

It is not a Home Assistant add-on, not a Nabu Casa product, and not a replacement for the Home Assistant UI. It is an external admin console for admins who manage more than one Home Assistant instance.

## Non-goals

- generic Home Assistant proxy
- full replacement Home Assistant UI
- automatic rollback
- hosted SaaS
- shared credentials between users or unrelated tools

## Public examples

Use neutral placeholders: `home`, `lab`, `rental`, `remote`, `admin@example.local`, `https://homeassistant.example.com`, and `http://<server-lan-ip>:8799`.

Avoid personal names, private domains, property names, local IPs, live emails, and real screenshots in docs, tests, screenshots, and seed data.
