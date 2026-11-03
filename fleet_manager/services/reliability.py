from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import BackupRecord, JobRun, Operation, Instance, UpdateRecord, now

ACTIVE_STATUSES = {'queued', 'running', 'restarting', 'reconnecting', 'accepted', 'verification_pending'}
INCOMPATIBLE_KINDS = {'restart', 'repair_restart', 'repair_reboot', 'update_install', 'update_skip', 'backup_create'}
SECRET_WORDS = ('token', 'password', 'secret', 'credential', 'ciphertext', 'authorization')


def public_error_message(code: str | None) -> str:
    """Return actionable error categories without echoing provider payloads."""
    raw = str(code or 'operation_error')
    text = raw.lower()
    if 'auth' in text or 'permission' in text or 'forbidden' in text:
        return 'Authentication or permission failed'
    if 'timeout' in text:
        return 'Home Assistant did not respond before the timeout'
    if 'connect' in text or 'network' in text or 'offline' in text:
        return 'Home Assistant is unavailable'
    if 'deadline' in text:
        return 'The verification deadline expired before the outcome was confirmed'
    safe_code = raw[:80] if re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', raw) else 'unknown_error'
    return f'Operation failed ({safe_code})'


def safe_exception_message(exc: BaseException) -> str:
    return public_error_message(type(exc).__name__)


def aware_utc(value: datetime | None) -> datetime | None:
    """Normalize SQLite's naive timestamps before comparing them with aware UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_ha_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return aware_utc(datetime.fromisoformat(str(value).replace('Z', '+00:00')))
    except (TypeError, ValueError):
        return None


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        error_code = value.get('type') or value.get('error_type') or value.get('error_code')
        out = {}
        for k, v in value.items():
            key = str(k).lower()
            if any(w in key for w in SECRET_WORDS):
                out[k] = '[REDACTED]'
            elif key in {'message', 'error_message'} or (key == 'error' and isinstance(v, str)):
                out[k] = public_error_message(error_code or v)
            else:
                out[k] = _safe(v)
        return out
    if isinstance(value, list):
        return [_safe(v) for v in value]
    return value


def create_operation(db: Session, *, kind: str, instance: Instance | None = None,
                     idempotency_key: str | None = None, request_id: str | None = None,
                     recovery_action: str | None = None, details: dict | None = None,
                     deadline_seconds: int = 300, batch_id: str | None = None) -> Operation:
    if idempotency_key:
        existing = db.query(Operation).filter(
            Operation.idempotency_key == idempotency_key,
            Operation.status.in_(ACTIVE_STATUSES),
        ).order_by(Operation.id.desc()).first()
        if existing:
            return existing
    if instance and kind in INCOMPATIBLE_KINDS:
        active = db.query(Operation).filter(Operation.instance_id == instance.id, Operation.status.in_(ACTIVE_STATUSES)).all()
        if active:
            raise ValueError(f'active operation {active[0].id} blocks {kind}')
    t = now()
    op = Operation(kind=kind, instance_id=instance.id if instance else None,
                   state='queued', status='queued', started_at=None,
                   correlation_id=secrets.token_urlsafe(18), idempotency_key=idempotency_key,
                   request_id=request_id or secrets.token_urlsafe(12), recovery_action=recovery_action,
                   deadline_at=t + timedelta(seconds=max(1, deadline_seconds)),
                   max_attempts=3, batch_id=batch_id,
                   details_json=json.dumps(_safe(details or {}), default=str))
    try:
        with db.begin_nested():
            db.add(op)
            db.flush()
    except IntegrityError:
        if idempotency_key:
            existing = db.query(Operation).filter(
                Operation.idempotency_key == idempotency_key,
                Operation.status.in_(ACTIVE_STATUSES),
            ).first()
            if existing:
                return existing
        if instance and kind in INCOMPATIBLE_KINDS:
            active = db.query(Operation).filter(
                Operation.instance_id == instance.id,
                Operation.status.in_(ACTIVE_STATUSES),
            ).order_by(Operation.id.desc()).first()
            if active:
                raise ValueError(f'active operation {active.id} blocks {kind}')
        raise
    return op


def _details(op: Operation) -> dict:
    try: return json.loads(op.details_json or '{}')
    except (TypeError, ValueError): return {}


def _save(op: Operation, values: dict):
    data = _details(op); data.update(_safe(values)); op.details_json = json.dumps(data, default=str)
    op.updated_at = now()


def durable_dispatch(op: Operation) -> bool:
    details = _details(op)
    return isinstance(details, dict) and details.get('dispatch_protocol') == 2


def current_terminal_hold(op: Operation, current: datetime | None = None) -> bool:
    """Do not retroactively impose queue rules on ancient legacy history."""
    deadline = aware_utc(op.deadline_at)
    return durable_dispatch(op) or bool(deadline and deadline >= aware_utc(current or now()) - timedelta(hours=12))


def reconcile_operation(db: Session, op: Operation, *, adapter=None, now_value: datetime | None = None) -> Operation:
    current = aware_utc(now_value or now())
    assert current is not None
    if op.status not in ACTIVE_STATUSES and not (op.status in {'outcome_unknown', 'action_required'} and op.kind in {'update_install', 'update_skip'}):
        return op
    previous_status = op.status
    verified = False
    error = None
    try:
        if op.kind in {'restart', 'repair_restart', 'repair_reboot'}:
            if adapter is None: raise RuntimeError('verification adapter unavailable')
            d = _details(op)
            try:
                adapter.test_connection()
                # A healthy response alone does not prove that a restart happened;
                # success requires observing the expected disconnect first.
                verified = bool(d.get('disconnect_observed'))
                if not verified:
                    error = 'restart disconnect not observed'
            except Exception as exc:
                error = type(exc).__name__
                _save(op, {'disconnect_observed': True, 'disconnect_error': error})
                deadline = aware_utc(op.deadline_at)
                if deadline is None or current < deadline:
                    op.status = 'reconnecting'; op.state = 'reconnecting'
                    return op
        elif op.kind == 'update_install':
            d = _details(op); entity = d.get('entity_id'); target = d.get('target_version')
            state = adapter.get(f'/api/states/{entity}') if adapter and entity else {}
            attrs = state.get('attributes') or {}
            verified = bool(target and attrs.get('installed_version') == target and not attrs.get('in_progress') and state.get('state') not in {'installing', 'updating'})
            deadline = aware_utc(op.deadline_at)
            if not verified and (deadline is None or current < deadline) and (attrs.get('in_progress') or state.get('state') in {'installing', 'updating'}):
                op.status = previous_status if previous_status in {'outcome_unknown', 'action_required'} else 'reconnecting'
                op.state = 'Target still in progress; previous hold preserved'
                _save(op, {'verification': 'still_in_progress', 'verified': False}); return op
        elif op.kind == 'update_skip':
            d = _details(op); entity = d.get('entity_id'); target = d.get('target_version')
            state = adapter.get(f'/api/states/{entity}') if adapter and entity else {}
            attrs = state.get('attributes') or {}
            verified = bool(target and attrs.get('skipped_version') == target)
        elif op.kind == 'backup_create':
            d = _details(op)
            response = adapter.backup_info() if adapter and hasattr(adapter, 'backup_info') else (adapter.get('/api/backup') if adapter else {})
            backups = response.get('backups') or (response.get('data') or {}).get('backups') or []
            expected_id, expected_name = d.get('backup_id'), d.get('name')
            match = next((b for b in backups if (expected_id and str(b.get('backup_id') or b.get('slug')) == str(expected_id)) or (expected_name and b.get('name') == expected_name)), None)
            automatic_requested = any(
                str(attempt.get('endpoint') or '').endswith('/backup/create_automatic')
                for attempt in d.get('attempts') or []
                if isinstance(attempt, dict)
            )
            if not match and automatic_requested:
                started = aware_utc(op.started_at)
                attempted = parse_ha_datetime(response.get('last_attempted_automatic_backup'))
                completed = parse_ha_datetime(response.get('last_completed_automatic_backup'))
                belongs_to_operation = bool(started and attempted and attempted >= started - timedelta(seconds=30))
                if belongs_to_operation and response.get('state') == 'create_backup':
                    op.status = 'reconnecting'; op.state = 'reconnecting'
                    _save(op, {'verification': 'backup_in_progress'})
                    return op
                if belongs_to_operation and attempted is not None and completed and completed >= attempted:
                    dated = [(parse_ha_datetime(b.get('date') or b.get('date_created')), b) for b in backups]
                    candidates = [(created, b) for created, b in dated if created and created >= attempted - timedelta(seconds=30)]
                    if candidates:
                        match = max(candidates, key=lambda item: item[0])[1]
                automatic_failed = (
                    belongs_to_operation
                    and response.get('state') == 'idle'
                    and (completed is None or (attempted is not None and completed < attempted))
                )
                if automatic_failed:
                    # Reconciliation observes existing effects only. It must never
                    # submit a fallback backup from startup, polling or a retry read.
                    op.status = 'failed'; op.state = 'Automatic backup failed — explicit new request required'
                    op.ended_at = current; op.error_code = 'automatic_backup_failed'
                    _save(op, {'verification': 'automatic_backup_failed', 'verified': False,
                               'recovery': 'request_new_backup_explicitly'})
                    record = db.get(BackupRecord, d.get('backup_record_id')) if d.get('backup_record_id') else None
                    if record:
                        record.status = 'failed'
                    return op
            verified = bool(match)
            if match:
                backup_id = match.get('backup_id') or match.get('slug')
                _save(op, {'backup_id': backup_id, 'verified_backup': True})
                record = db.get(BackupRecord, d.get('backup_record_id')) if d.get('backup_record_id') else None
                if record:
                    record.backup_id = str(backup_id) if backup_id else record.backup_id
                    record.status = 'completed'; record.completed_at = current
                if op.instance_id:
                    instance = db.get(Instance, op.instance_id)
                    if instance:
                        instance.last_successful_backup = current
                        instance.backup_compliance_state = 'current'
        else:
            raise RuntimeError('no safe verification route')
    except Exception as exc:
        error = type(exc).__name__
    if verified:
        # Read restart evidence before changing status, even when the cached update
        # row was deleted. Missing/malformed evidence never releases a known hold.
        repairs = []
        if op.kind == 'update_install':
            from .automation import _restart_repair_issues
            repairs = _restart_repair_issues(adapter)
        op.error_code = None; op.error_message = None
        if op.kind == 'update_install' and repairs is None:
            deadline = aware_utc(op.deadline_at)
            op.status = previous_status if previous_status in {'outcome_unknown', 'action_required'} else ('outcome_unknown' if deadline and current >= deadline else 'verification_pending')
            op.state = 'Installed — restart clearance unverified'
            op.ended_at = current if op.status == 'outcome_unknown' else None
            if op.status == 'outcome_unknown':
                op.error_code = 'restart_clearance_unverified'
            _save(op, {'verified': False, 'installed_version_verified': True, 'restart_clearance_verified': False})
        else:
            op.status = 'action_required' if op.kind == 'update_install' and repairs else 'succeeded'
            op.state = 'Installed — restart required' if op.status == 'action_required' else 'succeeded'
            op.ended_at = current
            _save(op, {'verified': op.status == 'succeeded', 'verified_at': current.isoformat()})
            if op.kind == 'update_install':
                _save(op, {'installed_version_verified': True, 'restart_clearance_verified': not bool(repairs), 'restart_repairs': repairs})
        if op.kind in {'update_install', 'update_skip'}:
            d = _details(op)
            update = db.get(UpdateRecord, d.get('update_id')) if d.get('update_id') else None
            if update is None:
                update = db.query(UpdateRecord).filter_by(instance_id=op.instance_id, entity_id=d.get('entity_id')).first()
            if update and update.instance_id == op.instance_id and update.entity_id == d.get('entity_id'):
                update.raw_json = json.dumps(state, default=str)[:20000]
                if op.kind == 'update_install':
                    update.installed_version = attrs.get('installed_version')
                    update.available_version = attrs.get('latest_version') or update.available_version
                    update.installation_state = 'current' if update.installed_version == update.available_version else 'available'
                    update.approval_state = 'not_required' if update.installation_state == 'current' else 'required'
                    update.skip_state = 'none'
                    if repairs is not None:
                        update.restart_required = bool(repairs)
                else:
                    update.skip_state = 'skipped'; update.installation_state = 'skipped'
    else:
        deadline = aware_utc(op.deadline_at)
        if previous_status in {'outcome_unknown', 'action_required'}:
            op.status = previous_status
            op.state = 'Restart hold unverified' if previous_status == 'action_required' else 'outcome_unknown'
            _save(op, {'reason': error or 'state not confirmed; previous hold preserved', 'verified': False})
        elif deadline is not None and current >= deadline:
            op.status = 'outcome_unknown'; op.state = 'outcome_unknown'; op.ended_at = current
            op.error_code = 'verification_deadline_expired'; op.error_message = error or 'state not confirmed'
            _save(op, {'reason': op.error_message, 'verified': False})
        else:
            op.status = 'reconnecting'; op.state = 'reconnecting'
            _save(op, {'reason': error or 'verification pending', 'verified': False})
    return op


def reconcile_startup(db: Session, *, adapter_factory: Callable | None = None,
                      stale_after_seconds: int = 300, now_value: datetime | None = None) -> dict:
    current = aware_utc(now_value or now())
    result = {'operations': 0, 'jobs': 0, 'diagnostics': []}
    for op in db.query(Operation).filter(Operation.status.in_(ACTIVE_STATUSES | {'outcome_unknown', 'action_required'})).all():
        if op.status not in ACTIVE_STATUSES and not current_terminal_hold(op, current):
            continue  # archived legacy terminal rows were never auto-reconciled
        result['operations'] += 1
        adapter = None
        if adapter_factory and op.instance_id:
            try: adapter = adapter_factory(op.instance_id)
            except Exception as exc: result['diagnostics'].append(type(exc).__name__)
        reconcile_operation(db, op, adapter=adapter, now_value=current)
        db.commit()  # never hold the previous operation's writer across next HA call
    cutoff = current - timedelta(seconds=stale_after_seconds)
    for job in db.query(JobRun).filter(JobRun.status.in_(['running', 'accepted'])).all():
        from .bulk import is_managed_job
        if is_managed_job(job):
            continue  # resumable bulk queues retain their complete durable intents
        if job.started_at and aware_utc(job.started_at) <= cutoff:
            job.status = 'failed'; job.ended_at = current
            job.details_json = json.dumps({'reason': 'startup_reconciliation', 'diagnostic': 'stale job closed'})
            result['jobs'] += 1
    db.flush()
    # Startup reconciliation is itself durable diagnostics, not an in-memory probe.
    db.commit()
    return result


def safe_append_diagnostic(writer: Callable[[], object]) -> bool:
    try:
        writer()
        return True
    except Exception:
        return False


def migrate_operation_columns(engine):
    if engine.dialect.name != 'sqlite': return
    wanted = {
        'correlation_id':'VARCHAR(80)', 'idempotency_key':'VARCHAR(200)', 'request_id':'VARCHAR(120)',
        'recovery_action':'VARCHAR(120)', 'error_code':'VARCHAR(80)', 'error_message':'VARCHAR(500)',
        'deadline_at':'DATETIME', 'attempt_count':'INTEGER DEFAULT 0', 'max_attempts':'INTEGER DEFAULT 3',
        'next_retry_at':'DATETIME', 'batch_id':'VARCHAR(80)', 'per_item_json':'TEXT DEFAULT \'{}\'',
        'updated_at':'DATETIME',
    }
    with engine.connect() as conn:
        existing = {row[1] for row in conn.execute(text('PRAGMA table_info(operations)')).all()}
    with engine.begin() as conn:
        for name, spec in wanted.items():
            if name not in existing: conn.execute(text(f'ALTER TABLE operations ADD COLUMN {name} {spec}'))
        # Legacy active rows predate bounded deadlines and cannot be truthfully
        # reconciled. Close them as unknown before enforcing active-operation
        # uniqueness; preserve their full history and diagnostics.
        conn.execute(text("UPDATE operations SET status='outcome_unknown', state='outcome_unknown', ended_at=CURRENT_TIMESTAMP, error_code='legacy_unbounded_operation', error_message='operation predates bounded reconciliation' WHERE status IN ('queued','running','restarting','reconnecting','accepted','verification_pending') AND deadline_at IS NULL"))
        conn.execute(text('DROP INDEX IF EXISTS ix_operations_idempotency_key'))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_operations_active_idempotency ON operations (idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN ('queued','running','restarting','reconnecting','accepted','verification_pending')"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_operations_active_instance ON operations (instance_id) WHERE instance_id IS NOT NULL AND kind IN ('restart','repair_restart','repair_reboot','update_install','update_skip','backup_create') AND status IN ('queued','running','restarting','reconnecting','accepted','verification_pending')"))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_operations_instance_status ON operations (instance_id, status)'))
