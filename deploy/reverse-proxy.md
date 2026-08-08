# Reverse proxy notes

Example hostname:

```text
fleet-manager.example.com
```

Backend target:

```text
http://127.0.0.1:8799
```

Use TLS at the proxy. If you expose Fleet Manager beyond a private network, put it behind SSO, an IP allowlist, VPN, or an equivalent access control layer.

Nginx-style headers:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host $host;
proxy_set_header X-Real-IP $remote_addr;
client_max_body_size 20m;
```

Do not configure Fleet Manager as a pass-through proxy to Home Assistant. It intentionally does not expose generic proxying.
