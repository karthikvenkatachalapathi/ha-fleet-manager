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
from .reliability import create_operation, safe_append_diagnostic, safe_exception_message

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
                metadata={'channel': channel, 'error': type(exc).__name__, 'message': safe_exception_message(exc)},
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
            observations.append({'attempt': attempt, 'error_type': type(exc).__name__, 'error': safe_exception_message(exc)})
        if attempt < attempts - 1:
            time.sleep(delay)
    return False, after, after_attrs, observations




def _restart_repair_issues(adapter: HomeAssistantAdapter) -> list[dict] | None:
    """Return active HA repair issues that mean the install is not fully complete yet."""
    issues: list[dict] = []
    try:
        raw_issues = adapter.list_repairs()
    except Exception:
        return None  # unavailable evidence is not a verified empty repair list
    if not isinstance(raw_issues, list) or any(not isinstance(i, dict) for i in raw_issues):
        return None
    for issue in raw_issues:
        if not isinstance(issue.get('domain'), str) or not issue['domain'] or not isinstance(issue.get('issue_id'), str) or not issue['issue_id']:
            return None
        placeholders = issue.get('translation_placeholders')
        if placeholders is None:
            placeholders = {}
        if not isinstance(placeholders, dict):
            return None
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

def install_update(db: Session, inst: Instance, update: UpdateRecord, *, actor: str = 'automation', batch_id=None, dispatch_guard=None) -> Operation:
    """Reserve and commit before I/O; exactly one version-pinned POST per intent."""
    target, entity, iid, uid = update.available_version, update.entity_id, inst.id, update.id
    if not target:
        raise ValueError('missing_target_version')
    key = f'update_install:{iid}:{entity}:{target}'
    unresolved = db.query(Operation).filter(Operation.idempotency_key == key, Operation.status.in_(['outcome_unknown', 'manual_intervention_required'])).first()
    if unresolved:
        return unresolved
    token = CredentialService().get_instance_token(db, iid)
    adapter = HomeAssistantAdapter(inst, token)
    op = create_operation(db, kind='update_install', instance=inst, idempotency_key=key,
                          recovery_action='reconcile_install', batch_id=batch_id, deadline_seconds=1800,
                          details={'update_id': uid, 'entity_id': entity, 'target_version': target, 'actor': actor, 'dispatch_protocol': 2})
    if op.started_at is not None or op.status != 'queued':
        return op
    op.status = 'running'; op.state = 'Preflight'; op.started_at = now(); op.attempt_count = 1
    db.commit()  # durable intent and released SQLite writer before the first GET
    details = json.loads(op.details_json or '{}')
    try:
        before = adapter.get(f'/api/states/{entity}')
    except Exception as exc:
        op.status = 'failed'; op.state = 'Preflight failed — nothing dispatched'
        op.error_code = type(exc).__name__; op.ended_at = now(); db.commit()
        return op
    attrs = before.get('attributes') or {}
    in_progress = bool(attrs.get('in_progress')) or before.get('state') in {'installing', 'updating'}
    already_installed = attrs.get('installed_version') == target and not in_progress
    if not already_installed and attrs.get('latest_version') != target:
        op.status = 'blocked'; op.state = 'target_version_changed'; op.ended_at = now()
        details.update(reason='target_version_changed', expected=target, actual=attrs.get('latest_version'))
        op.details_json = json.dumps(details); db.commit()
        return op
    payload = {'entity_id': entity, 'version': target}
    if int(attrs.get('supported_features') or 0) & UPDATE_FEATURE_BACKUP:
        payload['backup'] = True
    post_exception, response, post_attempted = None, None, False
    if not already_installed and not in_progress:
        # Authorization is checked again immediately before reservation/dispatch.
        if dispatch_guard:
            dispatch_guard()
        op.status = 'accepted'; op.state = 'Dispatch reserved; verification pending'
        details.update(service_payload=payload, dispatch_reserved=True)
        op.details_json = json.dumps(details); db.commit()
        try:
            if dispatch_guard:
                dispatch_guard()  # fence/reauthorize after reservation, adjacent to the POST
            post_attempted = True
            response = adapter.post('/api/services/update/install', payload)
        except Exception as exc:
            from .bulk import LostLease, DispatchDenied
            if isinstance(exc, (LostLease, DispatchDenied)):
                raise
            post_exception = {'type': type(exc).__name__, 'message': safe_exception_message(exc)}
        # Neither a timeout nor 5xx proves rejection. Never fall back to an unpinned
        # 'latest' request and never replay this non-idempotent service call.
    db.commit()
    success, after, after_attrs, observations = _poll_update_install_result(adapter, entity, target, attempts=1, delay=0)
    restart_repairs = _restart_repair_issues(adapter) if success else []
    if success:
        update.installation_state = 'installed'
        update.installed_version = target
        update.available_version = after_attrs.get('latest_version') or target
        update.raw_json = json.dumps(after, default=str)[:20000]
        update.approval_state = 'not_required'
        if restart_repairs is None:
            op.status = 'verification_pending'; op.state = 'Installed — restart clearance unverified'; op.ended_at = None
        else:
            update.restart_required = bool(restart_repairs)
            op.status = 'action_required' if restart_repairs else 'succeeded'
            op.state = 'Installed — restart required' if restart_repairs else 'Succeeded'
            op.ended_at = now()
    else:
        # A successful HTTP response is only acceptance, not verified installation.
        progressing = bool(after_attrs.get('in_progress')) or after.get('state') in {'installing', 'updating'}
        op.status = 'outcome_unknown' if post_exception and not progressing else 'accepted'
        op.state = 'Accepted by Home Assistant; verification pending' if not post_exception or progressing else 'Install outcome unverified; inspect Home Assistant'
        op.ended_at = now() if op.status == 'outcome_unknown' else None
        if post_exception:
            op.error_code = post_exception['type']
            if not post_attempted:
                op.status = 'blocked'; op.state = 'Authorization rejected before dispatch'; op.ended_at = now()
    update.raw_json = json.dumps(after, default=str)[:20000]
    details.update(backup_requested=bool(payload.get('backup')), service_payload=payload,
                   service_response='accepted' if response is not None else None,
                   post_exception=post_exception, restart_repairs=restart_repairs,
                   install_attempts=[] if already_installed or in_progress else [{'attempt': 1, 'service_payload': payload, 'error_type': post_exception['type'] if post_exception else None}],
                   before={k: attrs.get(k) for k in ['installed_version', 'latest_version', 'in_progress', 'update_percentage']},
                   after={k: after_attrs.get(k) for k in ['installed_version', 'latest_version', 'in_progress', 'update_percentage']},
                   validation_observations=observations)
    op.details_json = json.dumps(details, default=str)
    audit(db, action='update_install_executed' if success else 'update_install_accepted_unverified', resource_type='update_record', resource_id=uid, instance_id=iid, result=op.status, metadata={'operation_id': op.id, 'target_version': target, 'actor': actor})
    db.commit()
    safe_append_diagnostic(lambda: append_vault_update_log(f'{ct_now()} — {inst.friendly_name} — {update.component}', [f'- Entity: `{entity}`', f'- Target: `{target}`', f'- Result: {op.state}', f'- Operation ID: {op.id}']))
    return op


def skip_update(db: Session, inst: Instance, update: UpdateRecord, *, actor: str = 'user', batch_id=None, dispatch_guard=None) -> Operation:
    target, entity, iid, uid = update.available_version, update.entity_id, inst.id, update.id
    if not target:
        raise ValueError('missing_target_version')
    key = f'update_skip:{iid}:{entity}:{target}'
    unresolved = db.query(Operation).filter(Operation.idempotency_key == key, Operation.status == 'outcome_unknown').first()
    if unresolved:
        return unresolved
    token = CredentialService().get_instance_token(db, iid)
    adapter = HomeAssistantAdapter(inst, token)
    op = create_operation(db, kind='update_skip', instance=inst, idempotency_key=key, batch_id=batch_id,
                          recovery_action='reconcile_skip', details={'update_id': uid, 'entity_id': entity, 'target_version': target, 'actor': actor, 'dispatch_protocol': 2})
    if op.started_at is not None or op.status != 'queued':
        return op
    op.state = 'Skip preflight'; op.status = 'running'; op.started_at = now(); op.attempt_count = 1
    db.commit()
    details = json.loads(op.details_json or '{}')
    post_attempted = False
    try:
        before = adapter.get(f'/api/states/{entity}')
        if (before.get('attributes') or {}).get('latest_version') != target:
            op.status = 'blocked'; op.state = 'target_version_changed'; op.ended_at = now(); db.commit()
            return op
        if dispatch_guard:
            dispatch_guard()
        op.status = 'verification_pending'; op.state = 'Skip dispatch reserved'
        details['dispatch_reserved'] = True; op.details_json = json.dumps(details)
        db.commit()
        if dispatch_guard:
            dispatch_guard()  # same post-reservation fence as install
        post_attempted = True
        adapter.post('/api/services/update/skip', {'entity_id': entity})
        state = adapter.get(f'/api/states/{entity}')
        attrs = state.get('attributes') or {}
        verified = attrs.get('skipped_version') == target
        if verified:
            update.skip_state = 'skipped'; update.installation_state = 'skipped'
            update.raw_json = json.dumps(state, default=str)[:20000]
        op.status = 'succeeded' if verified else 'verification_pending'
        op.state = 'Skipped' if verified else 'Verification pending'
        op.ended_at = now() if verified else None
    except Exception as exc:
        from .bulk import LostLease, DispatchDenied
        if isinstance(exc, (LostLease, DispatchDenied)):
            raise
        op.status = 'verification_pending' if post_attempted else 'blocked'
        op.state = 'Skip outcome unverified; verification pending' if post_attempted else 'Skip rejected before dispatch'
        op.ended_at = None if post_attempted else now()
        op.error_code = type(exc).__name__
        details['error'] = {'type': type(exc).__name__, 'message': safe_exception_message(exc)}
    op.details_json = json.dumps(details)
    audit(db, action='update_skipped' if op.status == 'succeeded' else 'update_skip_unverified', resource_type='update_record', resource_id=uid, instance_id=iid, result=op.status, metadata={'operation_id': op.id, 'actor': actor})
    db.commit()
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
                sync_failure_lines.append(f'- {inst.friendly_name}: {safe_exception_message(exc)}')
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
        job.status = 'partial_failed' if summary['sync_failed'] else 'succeeded'; job.ended_at = now(); job.details_json = json.dumps(summary, default=str)
        audit(db, action='monitor_and_act_completed', resource_type='job_run', resource_id=job.id, result=job.status, metadata=summary)
        return summary | {'job_id': job.id}
    except Exception as exc:
        job.status = 'failed'; job.ended_at = now(); job.details_json = json.dumps({'error': type(exc).__name__, 'message': safe_exception_message(exc), 'summary': summary}, default=str)
        audit(db, action='monitor_and_act_failed', resource_type='job_run', resource_id=job.id, result='failed', metadata={'error': type(exc).__name__})
        raise


def create_instance_backup(db: Session, inst: Instance, *, actor: str = 'user') -> BackupRecord:
    token = CredentialService().get_instance_token(db, inst.id)
    adapter = HomeAssistantAdapter(inst, token)
    op = create_operation(db, kind='backup_create', instance=inst,
                          idempotency_key=f'backup_create:{inst.id}',
                          recovery_action='verify_backup', details={'actor': actor}, deadline_seconds=900)
    existing = db.query(BackupRecord).filter(BackupRecord.details_json.like(f'%"operation_id": {op.id}%')).order_by(BackupRecord.id.desc()).first()
    if existing and op.status in {'succeeded', 'verification_pending', 'outcome_unknown'}:
        return existing
    rec = BackupRecord(instance_id=inst.id, provider='home_assistant', status='running', started_at=now(), name=f'Fleet Manager backup {ct_now()}')
    db.add(rec); db.flush()
    details = {'actor': actor, 'operation_id': op.id, 'request_id': op.request_id,
               'backup_record_id': rec.id, 'name': rec.name, 'attempts': []}
    op.started_at = now()
    op.status = 'running'; op.state = 'running'
    try:
        response = None
        known_backup_attempts = [
            ('/api/services/backup/create', {'name': rec.name}),
            ('/api/services/backup/create_automatic', {}),
            ('/api/services/hassio/backup_full', {'name': rec.name, 'compressed': True}),
        ]
        payload_by_endpoint = dict(known_backup_attempts)
        if hasattr(adapter, 'available_backup_services'):
            advertised = adapter.available_backup_services()
            backup_attempts = [(endpoint, payload_by_endpoint[endpoint]) for endpoint in advertised if endpoint in payload_by_endpoint]
            if not backup_attempts:
                raise RuntimeError('Home Assistant instance does not advertise a supported backup service')
        else:
            # Compatibility for custom adapters; the production adapter always
            # performs capability discovery before any mutation.
            backup_attempts = known_backup_attempts
        last_error: Exception | None = None
        for endpoint, payload in backup_attempts:
            try:
                response = adapter.post(endpoint, payload)
                details['attempts'].append({'endpoint': endpoint, 'ok': True})
                break
            except Exception as exc:
                last_error = exc
                details['attempts'].append({'endpoint': endpoint, 'ok': False, 'error': type(exc).__name__, 'message': safe_exception_message(exc)})
                message = str(exc).lower()
                status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                # These client responses prove HA rejected the request before
                # execution, so trying the next known backup service cannot
                # duplicate a backup. Authentication/conflict errors and all
                # transport/5xx failures remain ambiguous and stop immediately.
                definitively_unsupported = status_code in {400, 404, 405, 422} or any(
                    marker in message for marker in ('not found', 'unknown service', 'service not found')
                )
                if not definitively_unsupported:
                    # A timeout, disconnect, or 5xx may mean HA accepted the
                    # request. Never submit a second backup in that ambiguity.
                    raise
        else:
            assert last_error is not None
            raise last_error
        rec.backup_id = str((response or {}).get('slug') or (response or {}).get('backup_id') or '') or None
        # A service response is not proof that HA persisted the backup.  A stable
        # returned identifier is the only response-only evidence we accept.
        verified = bool(rec.backup_id)
        rec.status = 'completed' if verified else 'verification_pending'
        rec.completed_at = now()
        op.status = 'succeeded' if verified else 'verification_pending'
        op.state = op.status
        op.ended_at = rec.completed_at if verified else None
        if verified:
            inst.last_successful_backup = rec.completed_at
            inst.backup_compliance_state = 'current'
        details['backup_id'] = rec.backup_id
        details['response_summary'] = {k: v for k, v in (response or {}).items() if k in {'slug','backup_id','name','date'}} if isinstance(response, dict) else {}
        safe_append_diagnostic(lambda: append_vault_update_log(f'{ct_now()} — {inst.friendly_name} — backup created', [f'- Instance: {inst.friendly_name}', f'- Backup record ID: {rec.id}', f'- Backup ID: {rec.backup_id or "not returned"}', f'- Actor: {actor}', f'- Result: {rec.status}', f'- Operation ID: {op.id}']))
        audit(db, action='backup_created', resource_type='backup_record', resource_id=rec.id, instance_id=inst.id, request_id=op.request_id, result=op.status, metadata={'actor': actor, 'operation_id': op.id})
    except Exception as exc:
        ambiguous = isinstance(exc, (TimeoutError, httpx.TimeoutException, httpx.TransportError)) or (
            isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500
        )
        rec.status = 'verification_pending' if ambiguous else 'unsupported_or_failed'
        rec.completed_at = None if ambiguous else now()
        op.status = 'verification_pending' if ambiguous else 'failed'
        op.state = op.status
        op.ended_at = None if ambiguous else rec.completed_at
        op.error_code = type(exc).__name__; op.error_message = safe_exception_message(exc)
        details['error'] = {'type': type(exc).__name__, 'message': safe_exception_message(exc)}
        if not ambiguous:
            create_notification(db, severity='warning', title=f'Home Assistant backup unavailable: {inst.friendly_name}', body=safe_exception_message(exc), instance_id=inst.id)
        audit(db, action='backup_verification_pending' if ambiguous else 'backup_failed', resource_type='backup_record', resource_id=rec.id, instance_id=inst.id, request_id=op.request_id, result=op.status, metadata={'error': type(exc).__name__, 'operation_id': op.id})
    rec.details_json = json.dumps(details, default=str)
    op.details_json = json.dumps(details, default=str)
    return rec
