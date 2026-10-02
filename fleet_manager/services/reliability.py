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

from ..models import BackupRecord, JobRun, Operation, Instance, now

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


def reconcile_operation(db: Session, op: Operation, *, adapter=None, now_value: datetime | None = None) -> Operation:
    current = aware_utc(now_value or now())
    assert current is not None
    if op.status not in ACTIVE_STATUSES:
        return op
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
            verified = bool(target and attrs.get('installed_version') == target and not attrs.get('in_progress'))
            if not verified and (attrs.get('in_progress') or state.get('state') in {'installing', 'updating'}):
                op.status = 'reconnecting'; op.state = 'reconnecting'; _save(op, {'verification': 'still_in_progress'}); return op
        elif op.kind == 'update_skip':
            d = _details(op); entity = d.get('entity_id'); target = d.get('target_version')
            state = adapter.get(f'/api/states/{entity}') if adapter and entity else {}
            attrs = state.get('attributes') or {}
            verified = bool((target and attrs.get('skipped_version') == target) or state.get('state') in {'off', 'skipped'})
        elif op.kind == 'backup_create':
            d = _details(op)
            response = adapter.get('/api/backup') if adapter else {}
            backups = response.get('backups') or (response.get('data') or {}).get('backups') or []
            expected_id, expected_name = d.get('backup_id'), d.get('name')
            match = next((b for b in backups if (expected_id and str(b.get('backup_id') or b.get('slug')) == str(expected_id)) or (expected_name and b.get('name') == expected_name)), None)
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
        op.status = 'succeeded'; op.state = 'succeeded'; op.ended_at = current
        _save(op, {'verified': True, 'verified_at': current.isoformat()})
    else:
        deadline = aware_utc(op.deadline_at)
        if deadline is not None and current >= deadline:
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
    for op in db.query(Operation).filter(Operation.status.in_(ACTIVE_STATUSES)).all():
        result['operations'] += 1
        adapter = None
        if adapter_factory and op.instance_id:
            try: adapter = adapter_factory(op.instance_id)
            except Exception as exc: result['diagnostics'].append(type(exc).__name__)
        reconcile_operation(db, op, adapter=adapter, now_value=current)
    cutoff = current - timedelta(seconds=stale_after_seconds)
    for job in db.query(JobRun).filter(JobRun.status.in_(['running', 'accepted'])).all():
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
