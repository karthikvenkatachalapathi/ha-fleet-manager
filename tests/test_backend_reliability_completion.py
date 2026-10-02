import json
from datetime import datetime, timezone

import pytest

from fleet_manager.db import Base, SessionLocal, engine
from fleet_manager.models import AuditEvent, BackupRecord, Instance, JobRun, Operation, UpdateRecord
from fleet_manager.services import automation
from fleet_manager.services.reliability import _safe, create_operation, migrate_operation_columns, reconcile_operation
from fleet_manager import app as appmod


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    migrate_operation_columns(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def make_update(db, name='HA', entity='update.demo'):
    inst = Instance(friendly_name=name, url='http://ha.local')
    db.add(inst)
    db.flush()
    update = UpdateRecord(instance_id=inst.id, entity_id=entity, component=name,
                          installed_version='1', available_version='2', installation_state='available')
    db.add(update)
    db.flush()
    return inst, update


def test_backup_persists_operation_before_post_and_accepted_is_pending(db, monkeypatch):
    inst = Instance(friendly_name='HA', url='http://ha.local')
    db.add(inst); db.flush()
    seen = []

    class Adapter:
        def __init__(self, *_): pass
        def post(self, path, payload):
            seen.append(db.query(Operation).count())
            return {'slug': 'stable-id'}

    monkeypatch.setattr(automation, 'HomeAssistantAdapter', Adapter)
    monkeypatch.setattr(automation, 'CredentialService', lambda: type('C', (), {'get_instance_token': lambda self, db, iid: 'token'})())
    monkeypatch.setattr(automation, 'append_vault_update_log', lambda *a, **k: None)
    rec = automation.create_instance_backup(db, inst, actor='test')
    assert seen == [1]
    assert rec.status == 'completed'
    assert inst.last_successful_backup is not None
    op = db.query(Operation).one()
    assert op.status == 'succeeded'
    assert json.loads(op.details_json)['backup_id'] == 'stable-id'


def test_backup_without_stable_id_is_verification_pending_and_audited(db, monkeypatch):
    inst = Instance(friendly_name='HA2', url='http://ha.local')
    db.add(inst); db.flush()
    class Adapter:
        def __init__(self, *_): pass
        def post(self, path, payload): return {}
    monkeypatch.setattr(automation, 'HomeAssistantAdapter', Adapter)
    monkeypatch.setattr(automation, 'CredentialService', lambda: type('C', (), {'get_instance_token': lambda self, db, iid: 'token'})())
    monkeypatch.setattr(automation, 'append_vault_update_log', lambda *a, **k: None)
    rec = automation.create_instance_backup(db, inst, actor='test')
    db.flush()
    op = db.query(Operation).one()
    audits = db.query(AuditEvent).all()
    assert audits, {'record_status': rec.status, 'operation_status': op.status}
    audit = next(a for a in audits if a.action == 'backup_created')
    assert rec.status == op.status == 'verification_pending'
    assert inst.last_successful_backup is None
    assert audit.request_id == op.request_id
    assert json.loads(audit.metadata_json)['operation_id'] == op.id


def test_bulk_deduplicates_and_records_per_item_outcomes(db, monkeypatch):
    inst, update = make_update(db)
    class Op:
        id = 44
        status = 'verification_pending'
        state = 'accepted'
        recovery_action = 'reconcile_install'
        batch_id = None
    monkeypatch.setattr(appmod, 'install_update', lambda *a, **k: Op())
    session = type('S', (), {'user': type('U', (), {'email': 'x'})(), 'user_id': 1})()
    monkeypatch.setattr('fleet_manager.app.require_permission', lambda *a, **k: None)
    result = appmod.bulk_updates(type('D', (), {'update_ids': [update.id, update.id], 'action': 'update'})(), session, db)
    assert result['selected'] == 1
    assert result['updated'] == 0
    assert len(result['results']) == 1
    assert {'update', 'instance', 'operation', 'status', 'reason'} <= result['results'][0].keys()
    assert db.query(JobRun).filter_by(kind='bulk_update').count() == 1


def test_repair_rejects_unsupported_action_before_operation(db, monkeypatch):
    inst = Instance(friendly_name='Repair', url='http://ha.local')
    db.add(inst); db.flush()
    session = type('S', (), {'user': type('U', (), {'email': 'x'})(), 'user_id': 1})()
    monkeypatch.setattr('fleet_manager.app.require_permission', lambda *a, **k: None)
    with pytest.raises(Exception) as exc:
        appmod.fix_repair(type('D', (), {'instance_id': inst.id, 'domain': 'hassio', 'issue_id': 'x', 'action': 'nope'})(), session, db)
    assert getattr(exc.value, 'status_code', None) == 422
    assert db.query(Operation).count() == 0


def test_terminal_operation_does_not_block_later_intent(db):
    inst = Instance(friendly_name='Repeatable', url='http://ha.local')
    db.add(inst); db.flush()
    first = create_operation(db, kind='backup_create', instance=inst, idempotency_key=f'backup_create:{inst.id}')
    first.status = 'succeeded'; db.flush()
    second = create_operation(db, kind='backup_create', instance=inst, idempotency_key=f'backup_create:{inst.id}')
    assert second.id != first.id


def test_accepted_install_reconciles_before_any_retry(db):
    inst, update = make_update(db, name='Accepted')
    op = create_operation(db, kind='update_install', instance=inst, idempotency_key='accepted:1',
                          details={'entity_id': update.entity_id, 'target_version': '2'})
    op.status = 'accepted'
    class Adapter:
        def get(self, path):
            return {'state': 'off', 'attributes': {'installed_version': '2', 'latest_version': '2', 'in_progress': False}}
    reconcile_operation(db, op, adapter=Adapter())
    assert op.status == 'succeeded'


def test_ambiguous_backup_failure_is_not_resubmitted(db, monkeypatch):
    inst = Instance(friendly_name='Ambiguous backup', url='http://ha.local')
    db.add(inst); db.flush()
    calls = []
    class Adapter:
        def __init__(self, *_): pass
        def post(self, path, payload):
            calls.append(path)
            raise TimeoutError('response lost')
    monkeypatch.setattr(automation, 'HomeAssistantAdapter', Adapter)
    monkeypatch.setattr(automation, 'CredentialService', lambda: type('C', (), {'get_instance_token': lambda self, db, iid: 't'})())
    monkeypatch.setattr(automation, 'safe_append_diagnostic', lambda writer: True)
    rec = automation.create_instance_backup(db, inst, actor='test')
    assert calls == ['/api/services/backup/create']
    assert rec.status == 'verification_pending'
    assert db.query(Operation).filter_by(instance_id=inst.id, kind='backup_create').one().status == 'verification_pending'


def test_definitive_backup_service_rejection_tries_next_known_endpoint(db, monkeypatch):
    import httpx

    inst = Instance(friendly_name='Rejected backup endpoint', url='http://ha.local')
    db.add(inst); db.flush()
    calls = []

    class Adapter:
        def __init__(self, *_): pass
        def post(self, path, payload):
            calls.append(path)
            if len(calls) == 1:
                response = httpx.Response(400, request=httpx.Request('POST', 'http://ha.local' + path))
                raise httpx.HTTPStatusError('definitive rejection', request=response.request, response=response)
            return {'backup_id': 'verified-backup'}

    monkeypatch.setattr(automation, 'HomeAssistantAdapter', Adapter)
    monkeypatch.setattr(automation, 'CredentialService', lambda: type('C', (), {'get_instance_token': lambda self, db, iid: 't'})())
    monkeypatch.setattr(automation, 'safe_append_diagnostic', lambda writer: True)
    record = automation.create_instance_backup(db, inst, actor='test')
    assert calls == ['/api/services/backup/create', '/api/services/backup/create_automatic']
    assert record.status == 'completed'
    assert record.backup_id == 'verified-backup'


def test_monitor_partial_failure_is_persisted(db, monkeypatch):
    inst = Instance(friendly_name='Offline monitor', url='http://ha.local')
    db.add(inst); db.flush()
    monkeypatch.setattr(automation, 'CredentialService', lambda: type('C', (), {'get_instance_token': lambda self, db, iid: 't'})())
    monkeypatch.setattr(automation, 'persist_instance_health', lambda *args: (_ for _ in ()).throw(TimeoutError('offline')))
    result = automation.monitor_and_act(db, auto_execute=False, actor='test')
    job = db.get(JobRun, result['job_id'])
    assert result['sync_failed'] >= 1
    assert job.status == 'partial_failed'


def test_skip_and_backup_verification_pending_operations_reconcile(db):
    inst, update = make_update(db, name='Reconcile')
    skip = create_operation(db, kind='update_skip', instance=inst, idempotency_key='skip:reconcile',
                            details={'entity_id': update.entity_id, 'target_version': '2'})
    skip.status = 'verification_pending'
    class SkipAdapter:
        def get(self, path): return {'state': 'off', 'attributes': {'skipped_version': '2'}}
    reconcile_operation(db, skip, adapter=SkipAdapter())
    assert skip.status == 'succeeded'

    record = BackupRecord(instance_id=inst.id, status='verification_pending', name='Fleet backup')
    db.add(record); db.flush()
    backup = create_operation(db, kind='backup_create', instance=inst, idempotency_key='backup:reconcile',
                              details={'backup_record_id': record.id, 'name': record.name})
    backup.status = 'verification_pending'
    class BackupAdapter:
        def get(self, path): return {'backups': [{'name': 'Fleet backup', 'backup_id': 'backup-123'}]}
    reconcile_operation(db, backup, adapter=BackupAdapter())
    assert backup.status == 'succeeded'
    assert record.status == 'completed'
    assert record.backup_id == 'backup-123'


def test_automatic_backup_reconciliation_tracks_running_then_completed(db):
    inst = Instance(friendly_name='Automatic backup', url='http://ha.local')
    db.add(inst); db.flush()
    record = BackupRecord(instance_id=inst.id, status='verification_pending', name='Fleet backup')
    db.add(record); db.flush()
    backup = create_operation(
        db,
        kind='backup_create',
        instance=inst,
        idempotency_key='backup:auto-reconcile',
        details={
            'backup_record_id': record.id,
            'name': record.name,
            'attempts': [{'endpoint': '/api/services/backup/create_automatic', 'ok': False}],
        },
    )
    backup.status = 'verification_pending'
    backup.started_at = datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)
    backup.error_code = 'ReadTimeout'
    backup.error_message = 'Home Assistant did not respond before the timeout'

    class RunningAdapter:
        def backup_info(self):
            return {
                'state': 'create_backup',
                'last_attempted_automatic_backup': '2026-10-02T08:00:01-05:00',
                'backups': [],
            }

    reconcile_operation(db, backup, adapter=RunningAdapter(), now_value=datetime(2026, 10, 2, 13, 1, tzinfo=timezone.utc))
    assert backup.status == 'reconnecting'
    assert record.status == 'verification_pending'

    class CompletedAdapter:
        def backup_info(self):
            return {
                'state': 'idle',
                'last_attempted_automatic_backup': '2026-10-02T08:00:01-05:00',
                'last_completed_automatic_backup': '2026-10-02T08:02:00-05:00',
                'backups': [{'backup_id': 'automatic-123', 'name': 'Automatic backup', 'date': '2026-10-02T13:02:00+00:00'}],
            }

    reconcile_operation(db, backup, adapter=CompletedAdapter(), now_value=datetime(2026, 10, 2, 13, 2, tzinfo=timezone.utc))
    assert backup.status == 'succeeded'
    assert record.status == 'completed'
    assert record.backup_id == 'automatic-123'
    assert backup.error_code is None
    assert backup.error_message is None


def test_sync_and_notification_failures_do_not_echo_provider_text(db, monkeypatch):
    inst = Instance(friendly_name='Sanitize', url='http://ha.local')
    db.add(inst); db.flush()
    session = type('S', (), {'user_id': 1})()
    monkeypatch.setattr(appmod, 'require_permission', lambda *a, **k: None)
    monkeypatch.setattr(appmod, 'CredentialService', lambda: type('C', (), {'get_instance_token': lambda self, db, iid: 'token'})())
    monkeypatch.setattr(appmod, 'persist_instance_health', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('remote password=abc123')))
    with pytest.raises(Exception) as sync_exc:
        appmod.sync_instance(inst.id, session, db)
    assert 'abc123' not in str(getattr(sync_exc.value, 'detail', sync_exc.value))

    monkeypatch.setattr(appmod, 'send_test_notification', lambda *a, **k: (_ for _ in ()).throw(appmod.smtplib.SMTPException('smtp password=abc123')))
    with pytest.raises(Exception) as mail_exc:
        appmod.test_notification_settings(appmod.TestNotificationIn(channel='email'), session, db)
    assert 'abc123' not in str(getattr(mail_exc.value, 'detail', mail_exc.value))


def test_activity_api_boundaries_sanitize_stored_legacy_details(db, monkeypatch):
    monkeypatch.setattr(appmod, 'require_permission', lambda *a, **k: None)
    session = object()
    db.add(AuditEvent(action='legacy', resource_type='test', metadata_json=json.dumps({'error': 'password=abc123'})))
    db.add(JobRun(kind='legacy', details_json=json.dumps({'message': 'token abc123'})))
    db.add(BackupRecord(instance_id=999, details_json=json.dumps({'error_message': 'credential abc123'})))
    db.flush()
    payload = {
        'audit': appmod.audit_log(session, db),
        'jobs': appmod.jobs(session, db),
        'backups': appmod.backups(session, db),
    }
    assert 'abc123' not in json.dumps(payload)


def test_retry_ambiguous_backup_is_explicitly_rejected(db, monkeypatch):
    inst = Instance(friendly_name='Backup retry', url='http://ha.local')
    db.add(inst); db.flush()
    op = create_operation(db, kind='backup_create', instance=inst, idempotency_key='backup:retry',
                          details={'name': 'Maybe created'})
    op.status = 'outcome_unknown'; db.flush()
    monkeypatch.setattr(appmod, 'require_permission', lambda *a, **k: None)
    monkeypatch.setattr(appmod, 'CredentialService', lambda: type('C', (), {'get_instance_token': lambda self, db, iid: 't'})())
    monkeypatch.setattr(appmod, 'HomeAssistantAdapter', lambda *a: type('A', (), {'get': lambda self, path: {'backups': []}})())
    session = type('S', (), {'user': type('U', (), {'email': 'x'})(), 'user_id': 1})()
    with pytest.raises(Exception) as exc:
        appmod.retry_operation(op.id, session, db)
    assert getattr(exc.value, 'status_code', None) == 409
    sanitized = _safe({'type': 'RuntimeError', 'message': 'must not expose remote secret abc123'})
    assert 'abc123' not in json.dumps(sanitized)
    assert sanitized['message'] == 'Operation failed (RuntimeError)'
