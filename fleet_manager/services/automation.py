from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import os
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from ..models import (
    Approval,
    AuditEvent,
    BackupRecord,
    Instance,
    JobRun,
    Notification,
    Operation,
    PolicySetting,
    UpdateRecord,
    now,
)
from ..settings import APP_TIMEZONE
from .audit import audit
from .credentials import CredentialService
from .ha_adapter import HomeAssistantAdapter, persist_instance_health, sync_updates
from .release_parser import parse_breaking_sections

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

VAULT_NOTE = Path(os.environ.get('FLEET_MANAGER_VAULT_NOTE', 'docs/home-assistant-update-automation.md'))
AUTO_EXCLUDED_CATEGORIES = {'Core', 'OS', 'Supervisor', 'Firmware'}
AUTO_EXCLUDED_ENTITY_IDS = {'update.home_assistant_core_update', 'update.home_assistant_operating_system_update'}
SAFE_AUTO_CATEGORIES = {'Add-on', 'HACS', 'Update Entity'}
DEFAULT_AUTO_POLICY = {
    'auto_execute_enabled': True,
    'excluded_categories': sorted(AUTO_EXCLUDED_CATEGORIES),
    'excluded_stacks': ['router', 'zigbee', 'z-wave', 'matter', 'thread'],
    'safe_categories': sorted(SAFE_AUTO_CATEGORIES),
    'requires_public_release_notes': True,
    'block_on_breaking_or_action_required': True,
    'core_haos_manual_only': True,
}
MAX_RELEASE_BYTES = 250_000
BREAKING_RE = re.compile(r'(breaking changes?|backward.?incompatible|action required|migration required|manual migration|deprecated|removed|upgrade notes?)', re.I)
UPDATE_FEATURE_BACKUP = 8



def normalized_policy(policy: dict | None = None) -> dict:
    merged = {**DEFAULT_AUTO_POLICY, **(policy or {})}
    for key in ('excluded_categories', 'excluded_stacks', 'safe_categories'):
        merged[key] = [str(x).strip() for x in (merged.get(key) or []) if str(x).strip()]
    return merged


def load_auto_policy(db: Session) -> dict:
    row = db.query(PolicySetting).filter_by(key='auto_update_policy').one_or_none()
    if not row:
        return normalized_policy()
    try:
        return normalized_policy(json.loads(row.value_json or '{}'))
    except json.JSONDecodeError:
        return normalized_policy()


def update_matches_stack(update: UpdateRecord, stacks: list[str]) -> str | None:
    haystack = f'{update.component} {update.category} {update.entity_id}'.lower()
    for stack in stacks:
        token = stack.lower().strip()
        if token and token in haystack:
            return stack
    return None


def ct_now() -> str:
    if ZoneInfo:
        return datetime.now(ZoneInfo(APP_TIMEZONE)).isoformat(timespec='seconds')
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def append_vault_update_log(title: str, lines: list[str]) -> None:
    VAULT_NOTE.parent.mkdir(parents=True, exist_ok=True)
    if not VAULT_NOTE.exists():
        VAULT_NOTE.write_text('# Home Assistant Update Automation\n\n', encoding='utf-8')
    entry = '\n'.join(['', '---', '', f'## {title}', '', *lines, ''])
    with VAULT_NOTE.open('a', encoding='utf-8') as f:
        f.write(entry)


def _public_release_url(url: str | None) -> bool:
    if not url:
        return False
    p = urlparse(url)
    if p.scheme not in {'http', 'https'} or not p.hostname:
        return False
    host = p.hostname.lower()
    if host in {'localhost'} or host.endswith('.local'):
        return False
    return True


def fetch_release_notes(url: str | None) -> tuple[str | None, str | None]:
    if not _public_release_url(url):
        return None, 'missing_or_non_public_release_url'
    try:
        with httpx.Client(follow_redirects=True, timeout=20, trust_env=False, headers={'User-Agent': 'HomeAssistantFleetManager/automation'}) as client:
            assert url is not None
            r = client.get(url)
            r.raise_for_status()
            text = r.text[:MAX_RELEASE_BYTES]
            text = re.sub(r'<(script|style|iframe|object|embed)[\s\S]*?</\1>', '', text, flags=re.I)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:50000], None
    except Exception as exc:
        return None, f'release_fetch_failed:{type(exc).__name__}'


def review_update_for_auto(update: UpdateRecord, policy: dict | None = None) -> tuple[bool, list[str], str | None]:
    policy = normalized_policy(policy)
    reasons: list[str] = []
    if not policy.get('auto_execute_enabled', True):
        reasons.append('automatic_updates_disabled_by_policy')
    if update.installation_state != 'available':
        reasons.append('not_available')
    if update.entity_id in AUTO_EXCLUDED_ENTITY_IDS and policy.get('core_haos_manual_only', True):
        reasons.append('core_or_haos_excluded')
    if update.category in set(policy.get('excluded_categories') or []):
        reasons.append(f'category_excluded:{update.category}')
    stack = update_matches_stack(update, policy.get('excluded_stacks') or [])
    if stack:
        reasons.append(f'infrastructure_stack_manual:{stack}')
    if update.category not in set(policy.get('safe_categories') or []):
        reasons.append(f'category_not_auto_safe:{update.category}')
    if update.approval_state == 'required' or update.critical_state or update.risk_level in {'high', 'critical'}:
        reasons.append('approval_or_high_risk_required')
    if not update.available_version:
        reasons.append('missing_target_version')

    notes, fetch_error = fetch_release_notes(update.release_url)
    if fetch_error and policy.get('requires_public_release_notes', True):
        reasons.append(fetch_error)
    if notes:
        breaking, excerpt = parse_breaking_sections(notes)
        update.release_notes = notes
        update.breaking_state = breaking
        update.breaking_excerpt = excerpt
        if policy.get('block_on_breaking_or_action_required', True) and (breaking or BREAKING_RE.search(notes[:20000])):
            reasons.append('breaking_or_action_required_detected')
    elif policy.get('requires_public_release_notes', True):
        reasons.append('no_release_notes_verified')

    return not reasons, reasons, notes


def create_notification(db: Session, *, severity: str, title: str, body: str, instance_id: int | None = None, update_id: int | None = None) -> Notification:
    n = Notification(severity=severity, title=title, body=body, instance_id=instance_id, update_record_id=update_id)
    db.add(n)
    return n


def current_approval_for(db: Session, update: UpdateRecord) -> Approval | None:
    return db.query(Approval).filter_by(update_record_id=update.id, target_version=update.available_version, status='approved').one_or_none()


def mark_manual_notifications(db: Session, inst: Instance, update: UpdateRecord) -> bool:
    title = f'Manual Home Assistant update required: {inst.friendly_name} / {update.component}'
    body = f'{update.component} {update.installed_version or "unknown"} → {update.available_version or "unknown"} is {update.category}; Core/HAOS/Supervisor/Firmware policy requires human handling.'
    exists = db.query(Notification).filter_by(title=title, status='open', update_record_id=update.id).one_or_none()
    if not exists:
        create_notification(db, severity='warning', title=title, body=body, instance_id=inst.id, update_id=update.id)
        return True
    return False


def _poll_update_install_result(adapter: HomeAssistantAdapter, entity_id: str, target_version: str | None, *, attempts: int = 10, delay: float = 3.0) -> tuple[bool, dict, dict, list[dict]]:
    observations: list[dict] = []
    after: dict = {'state': 'unknown'}
    after_attrs: dict = {}
    for attempt in range(attempts):
        try:
            after = adapter.get(f'/api/states/{entity_id}')
            after_attrs = after.get('attributes') or {}
            observations.append({'attempt': attempt, 'state': after.get('state'), 'installed_version': after_attrs.get('installed_version'), 'latest_version': after_attrs.get('latest_version'), 'in_progress': after_attrs.get('in_progress')})
            if target_version and after_attrs.get('installed_version') == target_version:
                return True, after, after_attrs, observations
            if after.get('state') != 'on' and not after_attrs.get('in_progress'):
                return True, after, after_attrs, observations
        except Exception as exc:
            observations.append({'attempt': attempt, 'error_type': type(exc).__name__, 'error': str(exc)[:240]})
        if attempt < attempts - 1:
            time.sleep(delay)
    return False, after, after_attrs, observations


def install_update(db: Session, inst: Instance, update: UpdateRecord, *, actor: str = 'automation') -> Operation:
    token = CredentialService().get_instance_token(db, inst.id)
    adapter = HomeAssistantAdapter(inst, token)
    op = Operation(kind='update_install', instance_id=inst.id, state='Preflight', status='running', started_at=now(), details_json=json.dumps({'update_id': update.id, 'entity_id': update.entity_id, 'target_version': update.available_version, 'actor': actor}))
    db.add(op); db.flush()
    before = adapter.get(f'/api/states/{update.entity_id}')
    attrs = before.get('attributes') or {}
    if attrs.get('latest_version') != update.available_version:
        op.state = 'Blocked'; op.status = 'blocked'; op.ended_at = now(); op.details_json = json.dumps({'reason': 'target_version_changed', 'expected': update.available_version, 'actual': attrs.get('latest_version')})
        return op
    op.state = 'Installing'; db.flush()
    payload = {'entity_id': update.entity_id}
    if update.available_version:
        payload['version'] = update.available_version
    supported = int(attrs.get('supported_features') or 0)
    if supported & UPDATE_FEATURE_BACKUP:
        payload['backup'] = True
    response = None
    post_exception = None
    try:
        response = adapter.post('/api/services/update/install', payload)
    except Exception as exc:
        # Add-ons such as Cloudflared can interrupt their own tunnel/API path while the update is actually accepted.
        # Poll before reporting; do not mark a persistent 5xx as success just because preflight passed.
        post_exception = {'type': type(exc).__name__, 'message': str(exc)[:300]}
    op.state = 'Validating'; db.flush()
    success, after, after_attrs, observations = _poll_update_install_result(adapter, update.entity_id, update.available_version)
    if success:
        op.state = 'Succeeded'
        op.status = 'succeeded'
        update.installation_state = 'installed'
        update.installed_version = update.available_version
        update.approval_state = 'not_required'
    elif post_exception:
        op.state = 'Accepted by Home Assistant; verification pending'
        op.status = 'accepted'
    else:
        op.state = 'Manual Intervention Required'
        op.status = 'manual_intervention_required'
        create_notification(db, severity='critical', title=f'Home Assistant update needs manual validation: {inst.friendly_name} / {update.component}', body=f'Install service returned but update still appears pending for {update.entity_id}.', instance_id=inst.id, update_id=update.id)
    op.ended_at = now()
    op.details_json = json.dumps({'update_id': update.id, 'entity_id': update.entity_id, 'target_version': update.available_version, 'backup_requested': bool(payload.get('backup')), 'service_payload': payload, 'service_response': response, 'post_exception': post_exception, 'before': {'state': before.get('state'), 'installed_version': attrs.get('installed_version'), 'latest_version': attrs.get('latest_version')}, 'after': {'state': after.get('state'), 'installed_version': after_attrs.get('installed_version'), 'latest_version': after_attrs.get('latest_version'), 'in_progress': after_attrs.get('in_progress')}, 'validation_observations': observations[-8:]})
    append_vault_update_log(
        f'{ct_now()} — {inst.friendly_name} — {update.component}',
        [
            f'- Instance: {inst.friendly_name}',
            f'- Entity: `{update.entity_id}`',
            f'- Version: `{attrs.get("installed_version")}` → `{update.available_version}`',
            f'- Category: {update.category}',
            f'- Actor: {actor}',
            f'- Result: {op.state}',
            f'- Release URL: {update.release_url or "not provided"}',
            f'- Operation ID: {op.id}',
        ],
    )
    audit_action = 'update_install_executed' if op.status != 'accepted' else 'update_install_accepted_unverified'
    audit(db, action=audit_action, resource_type='update_record', resource_id=update.id, instance_id=inst.id, result=op.status, metadata={'operation_id': op.id, 'target_version': update.available_version, 'actor': actor, 'post_exception': post_exception.get('type') if post_exception else None})
    return op


def skip_update(db: Session, inst: Instance, update: UpdateRecord, *, actor: str = 'user') -> Operation:
    token = CredentialService().get_instance_token(db, inst.id)
    adapter = HomeAssistantAdapter(inst, token)
    op = Operation(kind='update_skip', instance_id=inst.id, state='Skipping', status='running', started_at=now(), details_json=json.dumps({'update_id': update.id, 'entity_id': update.entity_id, 'target_version': update.available_version, 'actor': actor}))
    db.add(op); db.flush()
    payload = {'entity_id': update.entity_id}
    # HA's update.skip skips the current latest version for the entity; passing version is not universally supported.
    try:
        response = adapter.post('/api/services/update/skip', payload)
        update.skip_state = 'skipped'
        update.installation_state = 'skipped'
        op.state = 'Skipped'; op.status = 'succeeded'; op.ended_at = now()
        op.details_json = json.dumps({'update_id': update.id, 'entity_id': update.entity_id, 'target_version': update.available_version, 'service_payload': payload, 'service_response': response})
        audit(db, action='update_skipped', resource_type='update_record', resource_id=update.id, instance_id=inst.id, result='success', metadata={'operation_id': op.id, 'actor': actor})
    except Exception as exc:
        op.state = 'Skip Failed'; op.status = 'failed'; op.ended_at = now()
        op.details_json = json.dumps({'update_id': update.id, 'entity_id': update.entity_id, 'target_version': update.available_version, 'service_payload': payload, 'error': {'type': type(exc).__name__, 'message': str(exc)[:300]}})
        create_notification(db, severity='warning', title=f'Home Assistant skip failed: {inst.friendly_name} / {update.component}', body=f'{type(exc).__name__}: {str(exc)[:300]}', instance_id=inst.id, update_id=update.id)
        audit(db, action='update_skip_failed', resource_type='update_record', resource_id=update.id, instance_id=inst.id, result='failed', metadata={'operation_id': op.id, 'error': type(exc).__name__})
    return op


def monitor_and_act(db: Session, *, auto_execute: bool = True, actor: str = 'automation') -> dict:
    job = JobRun(kind='monitor_and_act', status='running', started_at=now(), details_json='{}')
    db.add(job); db.flush()
    summary = {'instances': 0, 'sync_ok': 0, 'sync_failed': 0, 'manual_notifications': 0, 'auto_candidates': 0, 'auto_installed': 0, 'blocked': []}
    try:
        policy = load_auto_policy(db)
        for inst in db.query(Instance).order_by(Instance.id).all():
            summary['instances'] += 1
            try:
                token = CredentialService().get_instance_token(db, inst.id)
                persist_instance_health(db, inst, token)
                sync_updates(db, inst, token)
                summary['sync_ok'] += 1
            except Exception as exc:
                summary['sync_failed'] += 1
                create_notification(db, severity='critical', title=f'Home Assistant sync failed: {inst.friendly_name}', body=f'{type(exc).__name__}: {str(exc)[:300]}', instance_id=inst.id)
                continue
            updates = db.query(UpdateRecord).filter_by(instance_id=inst.id, installation_state='available').all()
            for upd in updates:
                if upd.category in set(policy.get('excluded_categories') or []) or upd.entity_id in AUTO_EXCLUDED_ENTITY_IDS or update_matches_stack(upd, policy.get('excluded_stacks') or []) or upd.approval_state == 'required':
                    if mark_manual_notifications(db, inst, upd):
                        summary['manual_notifications'] += 1
                    continue
                ok, reasons, _notes = review_update_for_auto(upd, policy)
                if ok:
                    summary['auto_candidates'] += 1
                    if auto_execute:
                        op = install_update(db, inst, upd, actor=actor)
                        if op.status == 'succeeded':
                            summary['auto_installed'] += 1
                        else:
                            summary['blocked'].append({'instance': inst.friendly_name, 'component': upd.component, 'reason': op.state})
                    else:
                        summary['blocked'].append({'instance': inst.friendly_name, 'component': upd.component, 'reason': 'dry_run_candidate'})
                else:
                    summary['blocked'].append({'instance': inst.friendly_name, 'component': upd.component, 'reason': ','.join(reasons[:5])})
        job.status = 'succeeded'; job.ended_at = now(); job.details_json = json.dumps(summary, default=str)
        audit(db, action='monitor_and_act_completed', resource_type='job_run', resource_id=job.id, result='success', metadata=summary)
        return summary | {'job_id': job.id}
    except Exception as exc:
        job.status = 'failed'; job.ended_at = now(); job.details_json = json.dumps({'error': type(exc).__name__, 'message': str(exc)[:500], 'summary': summary}, default=str)
        audit(db, action='monitor_and_act_failed', resource_type='job_run', resource_id=job.id, result='failed', metadata={'error': type(exc).__name__})
        raise


def create_instance_backup(db: Session, inst: Instance, *, actor: str = 'user') -> BackupRecord:
    token = CredentialService().get_instance_token(db, inst.id)
    adapter = HomeAssistantAdapter(inst, token)
    rec = BackupRecord(instance_id=inst.id, provider='home_assistant', status='running', started_at=now(), name=f'Fleet Manager backup {ct_now()}')
    db.add(rec); db.flush()
    details = {'actor': actor, 'attempts': []}
    try:
        response = None
        try:
            response = adapter.post('/api/services/hassio/backup_full', {'name': rec.name, 'compressed': True})
            details['attempts'].append({'endpoint': '/api/services/hassio/backup_full', 'ok': True})
        except Exception as exc:
            details['attempts'].append({'endpoint': '/api/services/hassio/backup_full', 'ok': False, 'error': type(exc).__name__, 'message': str(exc)[:240]})
            response = adapter.post('/api/services/backup/create_automatic', {})
            details['attempts'].append({'endpoint': '/api/services/backup/create_automatic', 'ok': True})
        rec.status = 'completed'; rec.completed_at = now()
        rec.backup_id = str((response or {}).get('slug') or (response or {}).get('backup_id') or '') or None
        inst.last_successful_backup = rec.completed_at; inst.backup_compliance_state = 'current'
        details['response_summary'] = {k: v for k, v in (response or {}).items() if k in {'slug','backup_id','name','date'}} if isinstance(response, dict) else {}
        append_vault_update_log(f'{ct_now()} — {inst.friendly_name} — backup created', [f'- Instance: {inst.friendly_name}', f'- Backup record ID: {rec.id}', f'- Backup ID: {rec.backup_id or "not returned"}', f'- Actor: {actor}', '- Result: completed'])
        audit(db, action='backup_created', resource_type='backup_record', resource_id=rec.id, instance_id=inst.id, result='success', metadata={'actor': actor})
    except Exception as exc:
        rec.status = 'unsupported_or_failed'; rec.completed_at = now()
        details['error'] = {'type': type(exc).__name__, 'message': str(exc)[:300]}
        create_notification(db, severity='warning', title=f'Home Assistant backup unavailable: {inst.friendly_name}', body=f'{type(exc).__name__}: {str(exc)[:300]}', instance_id=inst.id)
        audit(db, action='backup_failed', resource_type='backup_record', resource_id=rec.id, instance_id=inst.id, result='failed', metadata={'error': type(exc).__name__})
    rec.details_json = json.dumps(details, default=str)
    return rec
