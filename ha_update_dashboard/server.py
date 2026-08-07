#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from ha_update_dashboard.scanner import scan

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
CONFIG = ROOT / "config.local.json"


class Handler(BaseHTTPRequestHandler):
    server_version = "HAUpdateDashboard/0.1"

    def log_message(self, format, *args):
        print(f"{self.client_address[0]} - {format % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, indent=2, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store" if path.name == "index.html" else "public, max-age=60")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json({"ok": True, "service": "ha-update-dashboard"})
            return
        if path == "/api/updates":
            try:
                self.send_json(scan(CONFIG))
            except Exception as exc:
                self.send_json({"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:240]}"}, 500)
            return
        if path in ("/", "/index.html"):
            self.send_file(STATIC / "index.html")
            return
        candidate = (STATIC / path.lstrip("/")).resolve()
        if STATIC.resolve() in candidate.parents:
            self.send_file(candidate)
        else:
            self.send_error(403)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"HA update dashboard listening on http://{args.host}:{args.port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
