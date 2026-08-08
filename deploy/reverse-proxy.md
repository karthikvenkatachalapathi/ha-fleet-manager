# Nginx Proxy Manager / reverse proxy notes

Recommended public/internal hostname:

```text
fleet-manager.example.com
```

Backend target:

```text
http://127.0.0.1:8799
```

Current app cookie is `HttpOnly` + `SameSite=Lax`. Enable TLS at the reverse proxy. If exposing beyond LAN/Tailscale, place behind Authentik or IP allowlist.

NPM advanced options:

```nginx
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host $host;
proxy_set_header X-Real-IP $remote_addr;
client_max_body_size 20m;
```

Do not configure proxying to the Home Assistant instances directly through Fleet Manager; it intentionally has no generic proxy endpoint.
