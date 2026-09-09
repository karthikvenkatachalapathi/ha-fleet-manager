from __future__ import annotations

import json
import re
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
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
DEFAULT_NOTIFICATION_SETTINGS = {
    'ntfy_enabled': False,
    'ntfy_url': '',
    'ntfy_topic': '',
    'ntfy_token': '',
    'pushover_enabled': False,
    'pushover_user_key': '',
    'pushover_app_token': '',
    'email_enabled': False,
    'email_to': '',
    'email_from': '',
    'smtp_host': '',
    'smtp_port': 587,
    'smtp_username': '',
    'smtp_password': '',
    'telegram_enabled': False,
    'telegram_bot_token': '',
    'telegram_chat_id': '',
}



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


def _load_json_setting(db: Session, key: str, default: dict) -> dict:
    row = db.query(PolicySetting).filter_by(key=key).one_or_none()
    if not row:
        return dict(default)
    try:
        value = json.loads(row.value_json or '{}')
    except json.JSONDecodeError:
        return dict(default)
    if not isinstance(value, dict):
        return dict(default)
    return {**default, **value}


def load_notification_settings(db: Session) -> dict:
    return _load_json_setting(db, 'notification_settings', DEFAULT_NOTIFICATION_SETTINGS)


def _send_ntfy(cfg: dict, title: str, body: str) -> None:
    url = (cfg.get('ntfy_url') or 'https://ntfy.sh').rstrip('/')
    topic = (cfg.get('ntfy_topic') or '').strip('/')
    if not topic:
        raise ValueError('ntfy topic is required')
    headers = {'Title': title}
    if cfg.get('ntfy_token'):
        headers['Authorization'] = f"Bearer {cfg['ntfy_token']}"
    with httpx.Client(timeout=15, trust_env=False) as client:
        client.post(f'{url}/{topic}', content=body.encode(), headers=headers).raise_for_status()


def _send_pushover(cfg: dict, title: str, body: str) -> None:
    if not cfg.get('pushover_user_key') or not cfg.get('pushover_app_token'):
        raise ValueError('Pushover user key and application token are required')
    with httpx.Client(timeout=15, trust_env=False) as client:
        client.post(
            'https://api.pushover.net/1/messages.json',
            data={'token': cfg['pushover_app_token'], 'user': cfg['pushover_user_key'], 'title': title, 'message': body},
        ).raise_for_status()


def _send_telegram(cfg: dict, title: str, body: str) -> None:
    if not cfg.get('telegram_bot_token') or not cfg.get('telegram_chat_id'):
        raise ValueError('Telegram bot token and chat ID are required')
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    with httpx.Client(timeout=15, trust_env=False) as client:
        client.post(url, json={'chat_id': cfg['telegram_chat_id'], 'text': f'{title}\n\n{body}'}).raise_for_status()


def _send_email(cfg: dict, title: str, body: str) -> None:
    for key in ['email_to', 'email_from', 'smtp_host']:
        if not cfg.get(key):
            raise ValueError(f'{key} is required')
    msg = EmailMessage()
    msg['Subject'] = title
    msg['From'] = cfg['email_from']
    msg['To'] = cfg['email_to']
    msg.set_content(body)
    with smtplib.SMTP(cfg['smtp_host'], int(cfg.get('smtp_port') or 587), timeout=15) as smtp:
        smtp.starttls()
        if cfg.get('smtp_username') or cfg.get('smtp_password'):
            smtp.login(cfg.get('smtp_username') or '', cfg.get('smtp_password') or '')
        smtp.send_message(msg)


def dispatch_notification(db: Session, notification: Notification) -> dict[str, str]:
    """Send a DB notification through every enabled outbound channel.

    Dispatch failures are audited but never prevent the dashboard notification
    from being created. Secrets are intentionally excluded from audit metadata.
    """
    cfg = load_notification_settings(db)
    senders = {
        'ntfy': ('ntfy_enabled', _send_ntfy),
        'pushover': ('pushover_enabled', _send_pushover),
        'email': ('email_enabled', _send_email),
        'telegram': ('telegram_enabled', _send_telegram),
    }
    results: dict[str, str] = {}
    for channel, (enabled_key, sender) in senders.items():
        if not cfg.get(enabled_key):
            continue
        try:
            sender(cfg, notification.title, notification.body)
        except Exception as exc:
            results[channel] = f'failed:{type(exc).__name__}'
            audit(
                db,
                action='notification_dispatch_failed',
                resource_type='notification',
                resource_id=notification.id,
                instance_id=notification.instance_id,
                result='failed',
                metadata={'channel': channel, 'error': type(exc).__name__, 'message': str(exc)[:160]},
            )
        else:
            results[channel] = 'sent'
            audit(
                db,
                action='notification_dispatch_sent',
                resource_type='notification',
                resource_id=notification.id,
                instance_id=notification.instance_id,
                result='success',
                metadata={'channel': channel},
            )
    return results


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
    db.flush()
    dispatch_notification(db, n)
    return n


def create_or_update_consolidated_notification(db: Session, *, severity: str, title: str, body: str) -> bool:
    """Maintain one open fleet-wide alert and dispatch only when its body changes."""
    existing = db.query(Notification).filter_by(title=title, status='open', instance_id=None, update_record_id=None).one_or_none()
    if existing:
        if existing.severity == severity and existing.body == body:
            return False
        existing.severity = severity
        existing.body = body
        dispatch_notification(db, existing)
        return True
    create_notification(db, severity=severity, title=title, body=body, instance_id=None, update_id=None)
    return True


def resolve_consolidated_notification(db: Session, *, title: str) -> bool:
    """Close the dashboard alert silently when the fresh scan has nothing actionable."""
    existing = db.query(Notification).filter_by(title=title, status='open', instance_id=None, update_record_id=None).one_or_none()
    if not existing:
        return False
    existing.status = 'resolved'
    existing.acknowledged_at = now()
    return True


def current_approval_for(db: Session, update: UpdateRecord) -> Approval | None:
    return db.query(Approval).filter_by(update_record_id=update.id, target_version=update.available_version, status='approved').one_or_none()


def mark_manual_notifications(db: Session, inst: Instance, update: UpdateRecord) -> bool:
    return mark_manual_update_notifications(db, inst, [update])


def _manual_update_line(update: UpdateRecord) -> str:
    return f'- {update.component}: {update.installed_version or "unknown"} → {update.available_version or "unknown"} ({update.category})'


def _simple_update_line(inst: Instance, update: UpdateRecord, risk: str | None = None) -> str:
    risk_category = risk or update.risk_level or update.category or 'unknown'
    return f'{inst.friendly_name} | {update.component} | {update.installed_version or "unknown"} → {update.available_version or "unknown"} | {risk_category}'


def _is_actionable_update(update: UpdateRecord) -> bool:
    """Reject stale rows and HA entities whose installed/latest versions already match."""
    if update.installation_state != 'available' or update.skip_state == 'skipped':
        return False
    return not (
        update.installed_version
        and update.available_version
        and update.installed_version == update.available_version
    )


def mark_review_update_notification(db: Session, inst: Instance, update: UpdateRecord, reasons: list[str] | None = None) -> bool:
    """Notify once for an available update that needs user visibility/review."""
    if not _is_actionable_update(update):
        return False
    title = f'Home Assistant update available: {inst.friendly_name} / {update.component}'
    body = f'{update.component} {update.installed_version or "unknown"} → {update.available_version or "unknown"} is available on {inst.friendly_name} ({update.category}).'
    clean_reasons = [r for r in (reasons or []) if r and r != 'dry_run_candidate']
    if clean_reasons:
        body += '\nReview reason: ' + ', '.join(clean_reasons[:5])
    exists = db.query(Notification).filter_by(title=title, status='open', update_record_id=update.id).one_or_none()
    if exists:
        if exists.body != body:
            exists.body = body
            dispatch_notification(db, exists)
            return True
        return False
    create_notification(db, severity='info', title=title, body=body, instance_id=inst.id, update_id=update.id)
    return True


def mark_manual_update_notifications(db: Session, inst: Instance, updates: list[UpdateRecord]) -> bool:
    pending = [u for u in updates if _is_actionable_update(u)]
    if not pending:
        return False
    if len(pending) == 1:
        update = pending[0]
        title = f'Manual Home Assistant update required: {inst.friendly_name} / {update.component}'
        body = f'{update.component} {update.installed_version or "unknown"} → {update.available_version or "unknown"} is {update.category}; Core/HAOS/Supervisor/Firmware policy requires human handling.'
        exists = db.query(Notification).filter_by(title=title, status='open', update_record_id=update.id).one_or_none()
        if not exists:
            create_notification(db, severity='warning', title=title, body=body, instance_id=inst.id, update_id=update.id)
            return True
        return False

    title = f'Manual Home Assistant updates required: {inst.friendly_name}'
    body = f'{len(pending)} updates require human handling on {inst.friendly_name}:\n' + '\n'.join(_manual_update_line(u) for u in pending[:12])
    if len(pending) > 12:
        body += f'\n- … {len(pending) - 12} more'
    exists = db.query(Notification).filter_by(title=title, status='open', instance_id=inst.id, update_record_id=None).one_or_none()
    if exists:
        if exists.body != body:
            exists.body = body
            return True
        return False

    # Collapse any older per-update manual notices for this instance once there is a group notice.
    prefix = f'Manual Home Assistant update required: {inst.friendly_name} /'
    for old in db.query(Notification).filter(Notification.status == 'open', Notification.instance_id == inst.id).all():
        if old.title.startswith(prefix):
            old.status = 'superseded'
            old.acknowledged_at = now()
    create_notification(db, severity='warning', title=title, body=body, instance_id=inst.id, update_id=None)
    return True


def _poll_update_install_result(adapter: HomeAssistantAdapter, entity_id: str, target_version: str | None, *, attempts: int = 10, delay: float = 3.0) -> tuple[bool, dict, dict, list[dict]]:
    observations: list[dict] = []
    after: dict = {'state': 'unknown'}
    after_attrs: dict = {}
    for attempt in range(attempts):
        try:
            after = adapter.get(f'/api/states/{entity_id}')
            after_attrs = after.get('attributes') or {}
            observations.append({'attempt': attempt, 'state': after.get('state'), 'installed_version': after_attrs.get('installed_version'), 'latest_version': after_attrs.get('latest_version'), 'in_progress': after_attrs.get('in_progress'), 'update_percentage': after_attrs.get('update_percentage')})
            in_progress = bool(after_attrs.get('in_progress')) or after.get('state') in {'installing', 'updating'}
            if target_version and after_attrs.get('installed_version') == target_version and not in_progress:
                return True, after, after_attrs, observations
            if not target_version and after.get('state') != 'on' and not in_progress:
                return True, after, after_attrs, observations
        except Exception as exc:
            observations.append({'attempt': attempt, 'error_type': type(exc).__name__, 'error': str(exc)[:240]})
        if attempt < attempts - 1:
            time.sleep(delay)
    return False, after, after_attrs, observations




def _restart_repair_issues(adapter: HomeAssistantAdapter) -> list[dict]:
    """Return active HA repair issues that mean the install is not fully complete yet."""
    issues: list[dict] = []
    try:
        raw_issues = adapter.list_repairs()
    except Exception:
        return issues
    for issue in raw_issues:
        placeholders = issue.get('translation_placeholders') or {}
        text = ' '.join(str(x or '') for x in [
            issue.get('domain'),
            issue.get('issue_id'),
            issue.get('translation_key'),
            placeholders.get('name'),
            placeholders.get('addon'),
            placeholders.get('integration'),
        ]).lower().replace('_', ' ').replace('-', ' ')
        if 'restart required' in text or 'reboot required' in text or 'system_reboot_required' in text:
            issues.append({
                'domain': issue.get('domain'),
                'issue_id': issue.get('issue_id'),
                'translation_key': issue.get('translation_key'),
                'title': placeholders.get('name') or issue.get('translation_key') or issue.get('issue_id'),
                'severity': issue.get('severity') or 'warning',
            })
    return issues

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
    install_attempts: list[dict] = []
    success = False
    after: dict = {'state': 'unknown'}
    after_attrs: dict = {}
    observations: list[dict] = []
    op.state = 'Installing'; db.flush()
    payload_variants = [payload]
    if payload.get('version'):
        latest_payload = {k: v for k, v in payload.items() if k != 'version'}
        payload_variants.append(latest_payload)
    stop_attempts = False
    for service_attempt in range(1, 4):
        for variant_index, attempt_payload in enumerate(payload_variants, start=1):
            attempt_exception = None
            try:
                response = adapter.post('/api/services/update/install', attempt_payload)
                install_attempts.append({'attempt': service_attempt, 'variant': variant_index, 'service_payload': attempt_payload, 'service_response': 'ok'})
            except Exception as exc:
                # Some Home Assistant update platforms reject an explicit version but accept "install latest".
                # Poll before reporting; do not mark a persistent 5xx as success just because preflight passed.
                attempt_exception = {'type': type(exc).__name__, 'message': str(exc)[:300]}
                post_exception = attempt_exception
                install_attempts.append({'attempt': service_attempt, 'variant': variant_index, 'service_payload': attempt_payload, 'error_type': attempt_exception['type'], 'error': attempt_exception['message']})
            op.state = 'Validating'; db.flush()
            success, after, after_attrs, attempt_observations = _poll_update_install_result(adapter, update.entity_id, update.available_version)
            observations.extend(attempt_observations)
            observed_state = after.get('state') not in {None, 'unknown'}
            made_progress = (
                bool(after_attrs.get('in_progress'))
                or (after_attrs.get('installed_version') is not None and after_attrs.get('installed_version') != attrs.get('installed_version'))
                or (observed_state and after.get('state') in {'installing', 'updating'})
            )
            if success or made_progress or not attempt_exception:
                stop_attempts = True
                break
        if stop_attempts:
            break
        if service_attempt < 3:
            op.state = f'Retrying install after Home Assistant error ({service_attempt}/3)'; db.flush()
    restart_repairs = _restart_repair_issues(adapter) if success else []
    if success:
        update.installation_state = 'installed'
        update.installed_version = after_attrs.get('installed_version') or update.available_version
        update.available_version = after_attrs.get('latest_version') or update.available_version
        update.raw_json = json.dumps(after or {}, default=str)[:20000]
        update.approval_state = 'not_required'
        if restart_repairs:
            op.state = 'Installed — restart required'
            op.status = 'action_required'
            update.restart_required = True
        else:
            op.state = 'Succeeded'
            op.status = 'succeeded'
    elif post_exception:
        observed_state = after.get('state') not in {None, 'unknown'}
        made_progress = (
            bool(after_attrs.get('in_progress'))
            or (after_attrs.get('installed_version') is not None and after_attrs.get('installed_version') != attrs.get('installed_version'))
            or (observed_state and after.get('state') in {'installing', 'updating'})
        )
        if made_progress:
            op.state = 'Accepted by Home Assistant; verification pending'
            op.status = 'accepted'
            update.raw_json = json.dumps(after or {}, default=str)[:20000]
        else:
            op.state = 'Load failed — Home Assistant rejected update install'
            op.status = 'manual_intervention_required'
            create_notification(db, severity='critical', title=f'Home Assistant update failed: {inst.friendly_name} / {update.component}', body=f'Install service failed for {update.entity_id}; version stayed at {attrs.get("installed_version") or "unknown"}. Check the instance directly.', instance_id=inst.id, update_id=update.id)
    else:
        op.state = 'Manual Intervention Required'
        op.status = 'manual_intervention_required'
        create_notification(db, severity='critical', title=f'Home Assistant update needs manual validation: {inst.friendly_name} / {update.component}', body=f'Install service returned but update still appears pending for {update.entity_id}.', instance_id=inst.id, update_id=update.id)
    op.ended_at = now()
    op.details_json = json.dumps({'update_id': update.id, 'entity_id': update.entity_id, 'target_version': payload.get('version'), 'backup_requested': bool(payload.get('backup')), 'service_payload': payload, 'service_response': response, 'post_exception': post_exception, 'install_attempts': install_attempts, 'restart_repairs': restart_repairs if 'restart_repairs' in locals() else [], 'before': {'state': before.get('state'), 'installed_version': attrs.get('installed_version'), 'latest_version': attrs.get('latest_version'), 'in_progress': attrs.get('in_progress'), 'update_percentage': attrs.get('update_percentage')}, 'after': {'state': after.get('state'), 'installed_version': after_attrs.get('installed_version'), 'latest_version': after_attrs.get('latest_version'), 'in_progress': after_attrs.get('in_progress'), 'update_percentage': after_attrs.get('update_percentage')}, 'validation_observations': observations[-12:]})
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
    audit_action = 'update_install_action_required' if op.status == 'action_required' else ('update_install_executed' if op.status != 'accepted' else 'update_install_accepted_unverified')
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
    sync_failure_lines: list[str] = []
    manual_update_lines: list[str] = []
    review_update_lines: list[str] = []
    try:
        policy = load_auto_policy(db)
        for inst in db.query(Instance).order_by(Instance.id).all():
            summary['instances'] += 1
            try:
                token = CredentialService().get_instance_token(db, inst.id)
                persist_instance_health(db, inst, token)
                sync_updates(db, inst, token)
                # SessionLocal disables autoflush. Persist the fresh HA snapshot before querying
                # actionable rows, otherwise stale rows leak in and newly discovered rows vanish.
                db.flush()
                summary['sync_ok'] += 1
            except Exception as exc:
                summary['sync_failed'] += 1
                sync_failure_lines.append(f'- {inst.friendly_name}: {type(exc).__name__}: {str(exc)[:180]}')
                continue
            updates = [
                update
                for update in db.query(UpdateRecord).filter_by(instance_id=inst.id, installation_state='available').all()
                if _is_actionable_update(update)
            ]
            manual_updates: list[UpdateRecord] = []
            auto_review_updates: list[UpdateRecord] = []
            for upd in updates:
                if upd.category in set(policy.get('excluded_categories') or []) or upd.entity_id in AUTO_EXCLUDED_ENTITY_IDS or update_matches_stack(upd, policy.get('excluded_stacks') or []) or upd.approval_state == 'required':
                    manual_updates.append(upd)
                else:
                    auto_review_updates.append(upd)
            if manual_updates:
                manual_update_lines.extend(_simple_update_line(inst, u, 'manual/high-risk') for u in manual_updates)
            for upd in auto_review_updates:
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
                        review_update_lines.append(_simple_update_line(inst, upd, upd.risk_level or upd.category))
                        summary['blocked'].append({'instance': inst.friendly_name, 'component': upd.component, 'reason': 'dry_run_candidate'})
                else:
                    review_update_lines.append(_simple_update_line(inst, upd, upd.risk_level or upd.category))
                    summary['blocked'].append({'instance': inst.friendly_name, 'component': upd.component, 'reason': ','.join(reasons[:5])})
        notification_sections: list[str] = []
        if sync_failure_lines:
            notification_sections.append('Sync failed:\n' + '\n'.join(sync_failure_lines[:12]))
        update_lines = manual_update_lines + review_update_lines
        if update_lines:
            notification_sections.append('Instance | What needs update | Current → New | Risk category\n' + '\n'.join(update_lines[:30]))
        summary_title = 'Home Assistant Fleet Manager update summary'
        if notification_sections:
            severity = 'critical' if sync_failure_lines else ('warning' if manual_update_lines else 'info')
            body = '\n\n'.join(notification_sections)
            if create_or_update_consolidated_notification(db, severity=severity, title=summary_title, body=body):
                summary['manual_notifications'] += 1
        else:
            resolve_consolidated_notification(db, title=summary_title)
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
        backup_attempts = [
            ('/api/services/backup/create', {'name': rec.name}),
            ('/api/services/backup/create_automatic', {}),
            ('/api/services/hassio/backup_full', {'name': rec.name, 'compressed': True}),
        ]
        last_error: Exception | None = None
        for endpoint, payload in backup_attempts:
            try:
                response = adapter.post(endpoint, payload)
                details['attempts'].append({'endpoint': endpoint, 'ok': True})
                break
            except Exception as exc:
                last_error = exc
                details['attempts'].append({'endpoint': endpoint, 'ok': False, 'error': type(exc).__name__, 'message': str(exc)[:240]})
        else:
            assert last_error is not None
            raise last_error
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
