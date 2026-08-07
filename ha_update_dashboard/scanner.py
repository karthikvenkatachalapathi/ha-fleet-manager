#!/usr/bin/env python3
"""Read-only Home Assistant update scanner.

Open-source ready: instances are configured by JSON; secrets are referenced by
Agent Vault key names and are fetched only at runtime.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

USER_AGENT = os.environ.get("HA_UPDATE_DASHBOARD_USER_AGENT", "Hermes-HA-Update-Dashboard/0.1")
VAULT = os.environ.get("AGENT_VAULT_VAULT", "hermes")
AV_ADDR = os.environ.get("AGENT_VAULT_ADDR", "http://192.168.2.7:14321").rstrip("/")
AV_TOKEN_FILE = os.environ.get("AGENT_VAULT_TOKEN_FILE", "/home/hermes/.config/secrets/agent-vault/shared.token")
CONFIG_PATH = Path(os.environ.get("HA_UPDATE_DASHBOARD_CONFIG", "/home/hermes/ha-update-dashboard/config.local.json"))

EXCLUDED_ENTITY_IDS = {
    "update.home_assistant_core_update",
    "update.home_assistant_operating_system_update",
}
EXCLUDED_KEYWORDS = ("home assistant core", "home assistant operating system", "haos", "operating system")
HIGH_RISK_KEYWORDS = ("firmware", "router", "unifi", "ratgdo", "esphome", "zwave", "zigbee", "matter", "thread")


@dataclass(frozen=True)
class Instance:
    alias: str
    label: str
    url: str
    token_key: str
    role: str = "secondary"


def no_proxy_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def av_token() -> str:
    token = os.environ.get("AGENT_VAULT_TOKEN")
    if token:
        return token
    p = Path(AV_TOKEN_FILE)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    raise RuntimeError("missing Agent Vault token")


def av_get(key: str) -> str:
    url = f"{AV_ADDR}/v1/credentials?vault={VAULT}&reveal=true&key={key}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {av_token()}", "User-Agent": USER_AGENT})
    data = json.loads(no_proxy_opener().open(req, timeout=20).read().decode())
    creds = data.get("credentials") or []
    value = data.get("value") or (creds[0].get("value") if creds else None)
    if not value:
        raise RuntimeError(f"missing Agent Vault credential {key}")
    return value


def load_instances(config_path: Path = CONFIG_PATH) -> list[Instance]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    instances = []
    for item in data.get("instances", []):
        instances.append(Instance(
            alias=item["alias"],
            label=item.get("label") or item["alias"],
            url=item["url"].rstrip("/"),
            token_key=item["token_key"],
            role=item.get("role", "secondary"),
        ))
    return instances


def ha_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def ha_get(instance: Instance, token: str, path: str) -> Any:
    req = urllib.request.Request(instance.url + path, headers=ha_headers(token))
    return json.loads(no_proxy_opener().open(req, timeout=45).read().decode())


def classify(update: dict[str, Any]) -> str:
    eid = update.get("entity_id", "")
    text = " ".join(str(update.get(k) or "") for k in ("entity_id", "title", "friendly_name", "installed_version", "latest_version")).lower()
    if eid in EXCLUDED_ENTITY_IDS or any(k in text for k in EXCLUDED_KEYWORDS):
        return "manual_core_haos"
    if any(k in text for k in HIGH_RISK_KEYWORDS):
        return "needs_review"
    return "candidate_safe"


def scan_instance(instance: Instance) -> dict[str, Any]:
    result: dict[str, Any] = {
        "alias": instance.alias,
        "label": instance.label,
        "role": instance.role,
        "url_host": instance.url.split("//", 1)[-1].split("/", 1)[0],
        "token_key": instance.token_key,
    }
    try:
        token = av_get(instance.token_key)
        cfg = ha_get(instance, token, "/api/config")
        states = ha_get(instance, token, "/api/states")
        updates = []
        for st in states:
            eid = st.get("entity_id", "")
            if not eid.startswith("update."):
                continue
            attrs = st.get("attributes") or {}
            update = {
                "entity_id": eid,
                "state": st.get("state"),
                "title": attrs.get("title") or attrs.get("friendly_name"),
                "friendly_name": attrs.get("friendly_name"),
                "installed_version": attrs.get("installed_version"),
                "latest_version": attrs.get("latest_version"),
                "release_url": attrs.get("release_url"),
                "skipped_version": attrs.get("skipped_version"),
                "in_progress": attrs.get("in_progress"),
                "device_class": attrs.get("device_class"),
            }
            update["policy"] = classify(update)
            updates.append(update)
        pending = [u for u in updates if u.get("state") == "on"]
        result.update({
            "ok": True,
            "ha_version": cfg.get("version"),
            "location_name": cfg.get("location_name"),
            "states_count": len(states),
            "updates_total": len(updates),
            "updates_pending_count": len(pending),
            "pending_by_policy": {
                "manual_core_haos": sum(1 for u in pending if u["policy"] == "manual_core_haos"),
                "needs_review": sum(1 for u in pending if u["policy"] == "needs_review"),
                "candidate_safe": sum(1 for u in pending if u["policy"] == "candidate_safe"),
            },
            "updates_pending": pending,
        })
    except Exception as exc:
        result.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}", "updates_pending_count": None})
    return result


def scan(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    instances = [scan_instance(inst) for inst in load_instances(config_path)]
    totals = {
        "instances": len(instances),
        "healthy": sum(1 for i in instances if i.get("ok")),
        "pending": sum(i.get("updates_pending_count") or 0 for i in instances),
        "manual_core_haos": sum((i.get("pending_by_policy") or {}).get("manual_core_haos", 0) for i in instances),
        "needs_review": sum((i.get("pending_by_policy") or {}).get("needs_review", 0) for i in instances),
        "candidate_safe": sum((i.get("pending_by_policy") or {}).get("candidate_safe", 0) for i in instances),
    }
    return {
        "generated_at_ct": datetime.now(ZoneInfo("America/Chicago")).isoformat(timespec="seconds"),
        "status": "ok" if totals["healthy"] == totals["instances"] else "degraded",
        "totals": totals,
        "policy": {
            "manual": ["Home Assistant Core", "Home Assistant OS / HAOS"],
            "review_required": ["Firmware", "Router", "UniFi", "ESPHome", "ratgdo", "Z-Wave/Zigbee/Matter/Thread"],
            "safe_candidates": "Non-core/non-HAOS updates only after release-note review by automation job",
        },
        "instances": instances,
    }


if __name__ == "__main__":
    print(json.dumps(scan(), indent=2, sort_keys=True))
