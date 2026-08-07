# Home Assistant Update Dashboard

Read-only LAN dashboard for Home Assistant update visibility across multiple instances.

## Goals

- One dashboard for all Home Assistant instances.
- Secrets never enter browser responses.
- Core and HAOS updates are clearly manual-only.
- Firmware/router/ESPHome/network updates are flagged as review-required.
- Safe-candidate updates are visible for the separate automation job that performs release-note checks before install.
- Open-source-ready config shape: URLs and Agent Vault key names live outside source in `config.local.json`.

## Quick start

```bash
cp config.sample.json config.local.json
python3 -m ha_update_dashboard.server --host 0.0.0.0 --port 8799
```

Open: `http://<server-lan-ip>:8799/`

## Config

```json
{
  "instances": [
    {
      "alias": "home",
      "label": "Home",
      "role": "primary_residence",
      "url": "https://homeassistant.example.com",
      "token_key": "HASS_TOKEN_HOME"
    }
  ]
}
```

`token_key` is an Agent Vault credential key. Token values are fetched server-side only and are never returned through `/api/updates`.

## Endpoints

- `/` — dashboard UI
- `/api/health` — service health
- `/api/updates` — sanitized update status JSON

## User systemd service

```bash
mkdir -p ~/.config/systemd/user
cp deploy/ha-update-dashboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ha-update-dashboard.service
```

## Safety model

This dashboard is read-only. It does not expose install buttons or Home Assistant tokens to the browser.
