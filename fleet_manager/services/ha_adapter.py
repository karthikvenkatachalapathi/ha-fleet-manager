from __future__ import annotations
import ipaddress, json, re, socket, ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlparse
import httpx
import certifi
from websockets.sync.client import connect
from sqlalchemy.orm import Session
from sqlalchemy import text
from hmac import compare_digest
from ..models import Instance, InstanceCredential, UpdateRecord, Approval, now

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
        from .api_policy import assert_live_access
        assert_live_access(self.instance.id, write=True)
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
        ssl_context = ssl.create_default_context(cafile=certifi.where()) if scheme == 'wss' else None
        connection = connect(url, open_timeout=timeout, close_timeout=2, proxy=None, ssl=ssl_context) if ssl_context is not None else connect(url, open_timeout=timeout, close_timeout=2, proxy=None)
        with connection as ws:
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
        if not isinstance(issues, list) or any(not isinstance(i, dict) for i in issues):
            raise ValueError('Home Assistant returned an invalid repairs response')
        return issues

    def backup_info(self) -> dict:
        result = self.websocket_command('backup/info') or {}
        return result if isinstance(result, dict) else {}

    def available_backup_services(self) -> list[str]:
        """Return only backup service endpoints advertised by this HA instance."""
        rows = self.get('/api/services')
        advertised: set[str] = set()
        for domain in rows if isinstance(rows, list) else []:
            name = str(domain.get('domain') or '')
            services = domain.get('services') or {}
            for service in services if isinstance(services, dict) else {}:
                advertised.add(f'/api/services/{name}/{service}')
        preferred = [
            '/api/services/backup/create_automatic',
            '/api/services/backup/create',
            '/api/services/hassio/backup_full',
        ]
        return [endpoint for endpoint in preferred if endpoint in advertised]

    def fix_repair(self, domain: str, issue_id: str):
        raise RuntimeError('No generic Home Assistant repairs fix command is available')

    def discover_updates(self) -> list[dict]:
        states=self.get('/api/states')
        # Validate the un-normalized HA envelope before it can be interpreted as
        # an empty fleet. An actual [] is a valid observation.
        if not isinstance(states, list):
            raise ValueError('Home Assistant returned an invalid states response')
        updates=[]; seen=set()
        for st in states:
            if not isinstance(st, dict):
                raise ValueError('Home Assistant returned an invalid state entry')
            eid=st.get('entity_id')
            if not isinstance(eid, str) or not eid:
                raise ValueError('Home Assistant returned a state without an entity_id')
            if not eid.startswith('update.'):
                continue
            if eid in seen or not re.fullmatch(r'update\.[a-z0-9_]+',eid):
                raise ValueError('Home Assistant returned duplicate or invalid update identities')
            seen.add(eid)
            state=st.get('state')
            attrs=st.get('attributes')
            if not isinstance(state, str) or not isinstance(attrs, dict):
                raise ValueError('Home Assistant returned an invalid update state')
            if state not in {'on','off','installing','updating','unknown','unavailable'}:
                raise ValueError('Home Assistant returned an invalid update lifecycle state')
            progress=attrs.get('in_progress')
            if progress is not None and (not isinstance(progress,(bool,int,float)) or not 0<=progress<=100):
                raise ValueError('Home Assistant returned invalid installation progress')
            installed=attrs.get('installed_version'); available=attrs.get('latest_version')
            if state not in {'unknown','unavailable'} and (installed is None or available is None):
                state='unknown'
            if installed is not None and not isinstance(installed, str):
                raise ValueError('Home Assistant returned an invalid installed version')
            if available is not None and not isinstance(available, str):
                raise ValueError('Home Assistant returned an invalid available version')
            title=attrs.get('title') or attrs.get('friendly_name') or eid
            if not isinstance(title, str):
                raise ValueError('Home Assistant returned an invalid update title')
            updates.append({'entity_id':eid,'state':state,'component':title,'installed_version':installed,'available_version':available,'release_url':attrs.get('release_url'),'in_progress':attrs.get('in_progress'),'skipped_version':attrs.get('skipped_version'),'supported_features':attrs.get('supported_features'),'raw':st})
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

def _snapshot_source(db: Session, instance_id: int):
    # Column reads deliberately avoid an ORM identity-map's stale attributes.
    return db.query(Instance.url, Instance.last_update_scan,
        InstanceCredential.nonce, InstanceCredential.ciphertext, InstanceCredential.tag
    ).outerjoin(InstanceCredential, InstanceCredential.instance_id == Instance.id).filter(Instance.id == instance_id).one_or_none()


def _snapshot_rows(db: Session, instance_id: int):
    return db.query(*UpdateRecord.__table__.columns).filter(UpdateRecord.instance_id == instance_id).order_by(UpdateRecord.id).all()


def sync_updates(db: Session, inst: Instance, token: str) -> int:
    # Collect first. Never acquire an observation write lock before HA I/O.
    # The caller owns its commit; discovery commits each home before continuing.
    with db.no_autoflush:
        instance_id, source_url = inst.id, inst.url
        source_before = _snapshot_source(db, instance_id)
        rows_before = _snapshot_rows(db, instance_id)
        if source_before is None or source_before.url != source_url:
            raise RuntimeError('Home Assistant source changed; refresh again')
        if source_before.nonce is not None:
            from .credentials import CredentialService
            configured = CredentialService().decrypt_parts(source_before.nonce, source_before.ciphertext, source_before.tag, f'instance:{instance_id}'.encode())
            if not compare_digest(configured.encode(), token.encode()):
                raise RuntimeError('Home Assistant credential changed; refresh again')
        updates = HomeAssistantAdapter(inst, token).discover_updates()
        if db.get_bind().dialect.name == 'sqlite':
            # SQLite serializes all writers, including approval/source edits.
            # A no-value-change write also upgrades pre-existing read transactions.
            db.execute(text('UPDATE instances SET last_update_scan=last_update_scan WHERE id=:id'), {'id':instance_id})
        else:
            db.query(Instance).filter_by(id=instance_id).with_for_update().all()
            db.query(InstanceCredential).filter_by(instance_id=instance_id).with_for_update().all()
            db.query(UpdateRecord).filter_by(instance_id=instance_id).with_for_update().all()
        if _snapshot_source(db, instance_id) != source_before or _snapshot_rows(db, instance_id) != rows_before:
            # Reject the ENTIRE observation. No skipped row inherits fresh status.
            raise RuntimeError('Fleet state changed during observation; refresh again')
        return _apply_update_snapshot(db, inst, updates)


def _apply_update_snapshot(db: Session, inst: Instance, updates: list[dict]) -> int:
    rows = {r.entity_id:r for r in db.query(UpdateRecord).filter_by(instance_id=inst.id).populate_existing().all()}
    pending=0; critical=0; approvals=0; seen=set(); observed_at=now()
    for u in updates:
        eid=u['entity_id']; seen.add(eid); row=rows.get(eid)
        if row is None:
            row=UpdateRecord(instance_id=inst.id, entity_id=eid, component=u.get('component') or eid); db.add(row)
        old_target, old_decision = row.available_version, row.approval_state
        if u.get('state') in {'unknown','unavailable'}:
            row.installation_state='unavailable'; row.last_discovered=observed_at
            row.raw_json=json.dumps(u.get('raw') or {}, default=str)[:20000]
            # Absence is not a version change or a revocation of a decision.
            continue
        same_version=bool(u.get('installed_version') and u.get('available_version') and u['installed_version']==u['available_version'])
        in_progress=bool(u.get('in_progress')) or u.get('state') in {'installing','updating'}
        is_pending=in_progress or (u.get('state')=='on' and not same_version)
        is_skipped=bool(u.get('skipped_version') and u.get('available_version') and u['skipped_version']==u['available_version'])
        category=categorize(u.get('component') or '',eid)
        release_notes=f"Release URL: {u['release_url']}" if u.get('release_url') else None
        breaking,excerpt=extract_breaking(release_notes)
        risk,critical_state,manual_required,approval,explanation=risk_for(category,u.get('component') or '',breaking)
        decision='required' if is_pending and approval=='approval_required' else 'not_required'
        if is_pending and old_target==u.get('available_version') and old_decision in {'approved','rejected'}:
            decision=old_decision
        if is_pending and row.id is not None:
            durable=db.query(Approval).filter_by(update_record_id=row.id,target_version=u.get('available_version')).one_or_none()
            if durable is not None and durable.status in {'approved','rejected'}: decision=durable.status
        if is_pending and not is_skipped:
            pending+=1
            if critical_state:
                critical+=1
                if decision=='required': approvals+=1
        row.component=u.get('component') or eid; row.category=category
        row.installed_version=u.get('installed_version'); row.available_version=u.get('available_version')
        row.release_url=u.get('release_url'); row.release_notes=sanitize_release_text(release_notes)
        row.breaking_state=breaking; row.breaking_excerpt=excerpt
        row.risk_level=risk; row.critical_state=critical_state; row.manual_action_required=manual_required; row.approval_state=decision
        row.installation_state='skipped' if is_skipped else 'available' if is_pending else 'current'
        row.skip_state='skipped' if is_skipped else 'none'
        if is_pending: row.restart_required=category in {'Core','OS','Supervisor'}
        row.policy_decision=approval; row.policy_explanation=explanation
        row.last_discovered=observed_at; row.raw_json=json.dumps(u.get('raw') or {},default=str)[:20000]
    for row in rows.values():
        if row.entity_id not in seen:
            row.installation_state='unavailable'; row.critical_state=False; row.manual_action_required=False
            if row.approval_state not in {'approved','rejected'}: row.approval_state='not_required'
    inst.available_updates=pending; inst.critical_updates=critical; inst.pending_approvals=approvals; inst.last_update_scan=observed_at
    if inst.connectivity_state=='online': inst.health_state='warning' if critical else 'healthy'
    return pending
