import json
from datetime import timedelta
import pytest

from fleet_manager.db import Base, SessionLocal, engine
from fleet_manager.models import Instance, JobRun, now
from fleet_manager.services.reliability import migrate_operation_columns

@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    migrate_operation_columns(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback(); session.close()
from fleet_manager.services.reliability import (
    create_operation,
    reconcile_operation,
    reconcile_startup,
    safe_append_diagnostic,
)


def test_operation_persists_correlation_recovery_and_safe_details(db):
    inst = Instance(friendly_name='HA', url='http://ha.local')
    db.add(inst); db.flush()
    op = create_operation(db, kind='restart', instance=inst, idempotency_key='req-1',
                          request_id='request-1', recovery_action='retry_restart',
                          details={'token': 'must-not-persist', 'target': 'restart'})
    assert op.correlation_id
    assert op.request_id == 'request-1'
    assert op.recovery_action == 'retry_restart'
    assert 'must-not-persist' not in op.details_json
    assert create_operation(db, kind='restart', instance=inst, idempotency_key='req-1').id == op.id


def test_conflicting_active_operation_is_rejected(db):
    inst = Instance(friendly_name='HA', url='http://ha.local')
    db.add(inst); db.flush()
    create_operation(db, kind='restart', instance=inst, idempotency_key='r1')
    try:
        create_operation(db, kind='update_install', instance=inst, idempotency_key='u1')
    except ValueError as exc:
        assert 'active operation' in str(exc)
    else:
        raise AssertionError('conflicting operation was accepted')


def test_reconcile_restart_success_checks_actual_state(db):
    inst = Instance(friendly_name='HA', url='http://ha.local')
    db.add(inst); db.flush()
    op = create_operation(db, kind='restart', instance=inst, idempotency_key='r1',
                          deadline_seconds=60)
    op.status = 'restarting'
    op.state = 'restarting'
    db.flush()
    class Adapter:
        def __init__(self): self.calls = 0
        def test_connection(self):
            self.calls += 1
            if self.calls == 1: raise TimeoutError('expected restart disconnect')
            return {'config': {'version': '2026.1'}}
    adapter = Adapter()
    reconcile_operation(db, op, adapter=adapter, now_value=now())
    assert op.status == 'reconnecting'
    reconcile_operation(db, op, adapter=adapter, now_value=now())
    assert op.status == 'succeeded'
    assert op.state == 'succeeded'
    assert json.loads(op.details_json)['verified'] is True


def test_expired_restart_is_outcome_unknown_not_failed(db):
    inst = Instance(friendly_name='HA', url='http://ha.local')
    db.add(inst); db.flush()
    op = create_operation(db, kind='restart', instance=inst, idempotency_key='r1', deadline_seconds=1)
    op.status = 'restarting'; op.state = 'restarting'
    op.deadline_at = now() - timedelta(seconds=1)
    reconcile_operation(db, op, adapter=type('A', (), {'test_connection': lambda self: (_ for _ in ()).throw(TimeoutError())})(), now_value=now())
    assert op.status == 'outcome_unknown'
    assert op.state == 'outcome_unknown'
    assert json.loads(op.details_json)['reason']


def test_startup_reconciliation_closes_stale_jobs(db):
    job = JobRun(kind='monitor', status='running', started_at=now() - timedelta(hours=2))
    db.add(job); db.flush()
    reconcile_startup(db, stale_after_seconds=60)
    assert job.status == 'failed'
    assert 'startup_reconciliation' in job.details_json


def test_vault_logging_failure_is_swallowed():
    safe_append_diagnostic(lambda: (_ for _ in ()).throw(OSError('vault unavailable')))
