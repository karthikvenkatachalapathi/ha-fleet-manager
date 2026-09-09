from __future__ import annotations
import ipaddress, json, re, socket
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlparse
import httpx
from websockets.sync.client import connect
from sqlalchemy.orm import Session
from ..models import Instance, UpdateRecord, now

USER_AGENT = 'HomeAssistantFleetManager/0.1'
BREAKING_PATTERNS = [r'breaking changes?', r'backward.?incompatible', r'action required', r'migration required', r'deprecated', r'removed', r'important', r'upgrade notes?']
HIGH_RISK = ('core', 'operating system', 'haos', 'supervisor', 'zigbee', 'z-wave', 'zwave', 'matter', 'thread', 'router', 'firmware')

def validate_instance_url(raw: str) -> str:
    p = urlparse(raw)
    if p.scheme not in {'http','https'}:
        raise ValueError('Only http and https Home Assistant URLs are supported')
    if not p.hostname:
        raise ValueError('Invalid Home Assistant URL')
    # SSRF baseline: allow private HA networks but pin to supplied host and disallow dangerous URL forms.
    if p.username or p.password:
        raise ValueError('Credentials in URLs are not allowed')
    if p.path not in ('', '/'):
        raise ValueError('Use the Home Assistant base URL only, without API paths')
    return raw.rstrip('/')

def resolve_host(host: str) -> list[str]:
    try:
        return sorted({x[4][0] for x in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})
    except socket.gaierror as exc:
        raise ValueError('DNS lookup failed') from exc

def sanitize_release_text(text: str | None) -> str | None:
    if not text: return None
    text = re.sub(r'<(script|iframe|object|embed)[\s\S]*?</\1>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = unescape(text)
    return text[:50000]

def extract_breaking(text: str | None) -> tuple[bool, str | None]:
    if not text: return False, None
    lines=text.splitlines()
    hits=[]
    for i,l in enumerate(lines):
        if any(re.search(p,l,re.I) for p in BREAKING_PATTERNS):
            hits.extend(lines[i:i+12])
    excerpt='\n'.join(hits).strip()[:4000]
    return bool(excerpt), excerpt or None

def categorize(title: str, entity_id: str) -> str:
    s=f'{title} {entity_id}'.lower()
    if 'home assistant core' in s: return 'Core'
    if 'operating system' in s or 'haos' in s: return 'OS'
    if 'supervisor' in s: return 'Supervisor'
    if 'hacs' in s: return 'HACS'
    if 'esphome' in s or 'firmware' in s: return 'Firmware'
    if 'add-on' in s or 'addon' in s: return 'Add-on'
    return 'Update Entity'

def risk_for(category: str, title: str, breaking: bool) -> tuple[str,bool,bool,str,str]:
    s=f'{category} {title}'.lower()
    if breaking:
        return 'critical', True, True, 'approval_required', 'Breaking/action-required release content detected'
    if any(x in s for x in HIGH_RISK):
        return 'high', True, False, 'approval_required', f'{category} or infrastructure-sensitive component requires approval'
    return 'low', False, False, 'eligible_manual_plan', 'Eligible for manual deployment plan or scoped automation policy'

class HomeAssistantAdapter:
    def __init__(self, instance: Instance, token: str):
        self.instance=instance
        self.token=token
        self.base=validate_instance_url(instance.url)
        self.host=urlparse(self.base).hostname or ''
        resolve_host(self.host)

    def _headers(self):
        return {'Authorization': f'Bearer {self.token}', 'Accept': 'application/json', 'Content-Type': 'application/json', 'User-Agent': USER_AGENT}

    def get(self, path: str):
        if not path.startswith('/api/'):
            raise ValueError('Only Home Assistant /api paths are allowed')
        with httpx.Client(follow_redirects=False, timeout=20, verify=True, trust_env=False) as client:
            r=client.get(self.base+path, headers=self._headers())
            if 300 <= r.status_code < 400:
                raise RuntimeError('Redirects are not followed to protect credentials')
            if r.status_code == 401:
                raise PermissionError('Authentication failed')
            r.raise_for_status()
            return r.json()

    def _post_allowed(self, path: str, payload: dict, *, timeout: int = 12):
        with httpx.Client(follow_redirects=False, timeout=timeout, verify=True, trust_env=False) as client:
            r=client.post(self.base+path, headers=self._headers(), json=payload)
            if 300 <= r.status_code < 400:
                raise RuntimeError('Redirects are not followed to protect credentials')
            if r.status_code == 401:
                raise PermissionError('Authentication failed')
            r.raise_for_status()
            return r.json() if r.text else {}

    def post(self, path: str, payload: dict):
        if not (path.startswith('/api/services/') or path.startswith('/api/events/')):
            raise ValueError('Only Home Assistant service or event API paths are allowed for POST')
        return self._post_allowed(path, payload)

    def supervisor_post(self, path: str, payload: dict | None = None):
        if not path.startswith('/api/hassio/'):
            raise ValueError('Only Home Assistant Supervisor API paths are allowed for supervisor POST')
        return self._post_allowed(path, payload or {}, timeout=20)

    def test_connection(self) -> dict:
        cfg=self.get('/api/config')
        states=self.get('/api/states')
        return {'config': cfg, 'state_count': len(states)}

    def websocket_command(self, command_type: str, payload: dict | None = None, *, timeout: int = 15):
        parsed = urlparse(self.base)
        scheme = 'wss' if parsed.scheme == 'https' else 'ws'
        url = f'{scheme}://{parsed.netloc}/api/websocket'
        with connect(url, open_timeout=timeout, close_timeout=2) as ws:
            hello = json.loads(ws.recv())
            if hello.get('type') != 'auth_required':
                raise RuntimeError('Unexpected Home Assistant websocket handshake')
            ws.send(json.dumps({'type': 'auth', 'access_token': self.token}))
            auth = json.loads(ws.recv())
            if auth.get('type') != 'auth_ok':
                raise PermissionError('Home Assistant websocket authentication failed')
            command = {'id': 1, 'type': command_type}
            if payload:
                command.update(payload)
            ws.send(json.dumps(command))
            result = json.loads(ws.recv())
            if not result.get('success'):
                raise RuntimeError((result.get('error') or {}).get('message') or f'{command_type} failed')
            return result.get('result')


    def fire_event(self, event_type: str, event_data: dict | None = None):
        return self.post(f'/api/events/{event_type}', event_data or {})

    def trigger_shutdown_automations(self, reason: str):
        # Supervisor host reboot can bypass Home Assistant's normal shutdown event.
        # Fire the canonical shutdown event first so user automations listening for
        # Home Assistant shutdown still run before the host goes down.
        return self.fire_event('homeassistant_stop', {'source': 'fleet_manager', 'reason': reason})

    def host_reboot(self) -> dict:
        attempts: list[dict] = []
        for endpoint, supervisor in [
            ('/api/hassio/host/reboot', True),
            ('/api/services/hassio/host_reboot', False),
            ('/api/services/hassio/host_reboot_full', False),
        ]:
            try:
                response = self.supervisor_post(endpoint, {}) if supervisor else self.post(endpoint, {})
                attempts.append({'endpoint': endpoint, 'ok': True})
                return {'ok': True, 'endpoint': endpoint, 'response': response, 'attempts': attempts}
            except Exception as exc:
                attempts.append({'endpoint': endpoint, 'ok': False, 'error': type(exc).__name__, 'message': str(exc)[:240]})
        raise RuntimeError('Host reboot failed via all known Home Assistant/Supervisor endpoints: ' + json.dumps(attempts))

    def list_repairs(self) -> list[dict]:
        result = self.websocket_command('repairs/list_issues') or {}
        issues = result.get('issues') if isinstance(result, dict) else result
        return issues if isinstance(issues, list) else []

    def fix_repair(self, domain: str, issue_id: str):
        raise RuntimeError('No generic Home Assistant repairs fix command is available')

    def discover_updates(self) -> list[dict]:
        states=self.get('/api/states')
        updates=[]
        for st in states:
            eid=st.get('entity_id','')
            if not eid.startswith('update.'):
                continue
            a=st.get('attributes') or {}
            title=a.get('title') or a.get('friendly_name') or eid
            updates.append({'entity_id':eid,'state':st.get('state'),'component':title,'installed_version':a.get('installed_version'),'available_version':a.get('latest_version'),'release_url':a.get('release_url'),'in_progress':a.get('in_progress'),'skipped_version':a.get('skipped_version'),'supported_features':a.get('supported_features'),'raw':st})
        return updates

def persist_instance_health(db: Session, inst: Instance, token: str):
    try:
        meta=HomeAssistantAdapter(inst, token).test_connection()
        cfg=meta['config']
        inst.connectivity_state='online'; inst.health_state='healthy'; inst.ha_core_version=cfg.get('version'); inst.location=inst.location or cfg.get('location_name')
        inst.last_successful_connection=now(); inst.last_successful_auth=now()
        return meta
    except PermissionError:
        inst.connectivity_state='auth_failed'; inst.health_state='critical'; inst.last_auth_failure=now(); inst.last_failed_connection=now(); raise
    except Exception:
        inst.connectivity_state='offline'; inst.health_state='offline'; inst.last_failed_connection=now(); raise

def sync_updates(db: Session, inst: Instance, token: str) -> int:
    adapter=HomeAssistantAdapter(inst, token)
    updates=adapter.discover_updates()
    pending=0; critical=0; approvals=0
    seen=set()
    for u in updates:
        seen.add(u['entity_id'])
        same_version = u.get('installed_version') and u.get('available_version') and u.get('installed_version') == u.get('available_version')
        is_pending = u.get('state') == 'on' and not same_version
        is_skipped = bool(u.get('skipped_version') and u.get('available_version') and u.get('skipped_version') == u.get('available_version'))
        if is_pending and not is_skipped: pending += 1
        category=categorize(u.get('component') or '', u['entity_id'])
        release_notes=None; breaking=False; excerpt=None
        if u.get('release_url'):
            release_notes=f"Release URL: {u['release_url']}"
            breaking, excerpt = extract_breaking(release_notes)
        risk, critical_state, manual_required, approval, explanation = risk_for(category, u.get('component') or '', breaking)
        if is_pending and critical_state: critical += 1; approvals += 1
        row=db.query(UpdateRecord).filter_by(instance_id=inst.id, entity_id=u['entity_id']).one_or_none()
        if row is None:
            row=UpdateRecord(instance_id=inst.id, entity_id=u['entity_id']); db.add(row)
        row.component=u.get('component') or u['entity_id']; row.category=category; row.installed_version=u.get('installed_version'); row.available_version=u.get('available_version')
        row.release_url=u.get('release_url'); row.release_notes=sanitize_release_text(release_notes); row.breaking_state=breaking; row.breaking_excerpt=excerpt
        row.risk_level=risk; row.critical_state=critical_state; row.manual_action_required=manual_required; row.approval_state='required' if is_pending and approval=='approval_required' else 'not_required'
        row.installation_state='skipped' if is_skipped else ('available' if is_pending else 'current'); row.skip_state='skipped' if is_skipped else 'none'; row.restart_required=category in {'Core','OS','Supervisor'}; row.policy_decision=approval; row.policy_explanation=explanation
        row.last_discovered=now(); row.raw_json=json.dumps(u.get('raw') or {}, default=str)[:20000]
    for row in db.query(UpdateRecord).filter_by(instance_id=inst.id).all():
        if row.entity_id not in seen:
            row.installation_state='unavailable'; row.approval_state='not_required'; row.critical_state=False; row.manual_action_required=False
    inst.available_updates=pending; inst.critical_updates=critical; inst.pending_approvals=approvals; inst.last_update_scan=now()
    if inst.connectivity_state == 'online' and critical: inst.health_state='warning'
    return pending
