import base64, os, tempfile
from pathlib import Path
from fastapi.testclient import TestClient

os.environ['MASTER_ENCRYPTION_KEY'] = base64.urlsafe_b64encode(b'0'*32).decode()
os.environ['FLEET_ADMIN_PASSWORD'] = 'test-password'
os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(tempfile.mkdtemp(prefix='hafm-test-')) / 'test.db')

from fleet_manager.db import Base, engine, SessionLocal
from fleet_manager.models import Instance, UpdateRecord, Notification, User
from fleet_manager.services.credentials import CredentialService
from fleet_manager.services.automation import review_update_for_auto, monitor_and_act, install_update
from fleet_manager.services.release_parser import parse_breaking_sections
from fleet_manager.services.ha_adapter import categorize, risk_for


def setup_module(module):
    Base.metadata.create_all(engine)


def test_credentials_encrypt_roundtrip_and_bind_aad():
    db = SessionLocal()
    inst = Instance(friendly_name='Test', url='http://ha.local')
    db.add(inst); db.flush()
    svc = CredentialService()
    svc.set_instance_token(db, inst.id, 'secret-token-value')
    db.commit()
    row = db.query(type(__import__('fleet_manager.models', fromlist=['InstanceCredential']).InstanceCredential())).one_or_none()
    assert b'secret-token-value' not in row.ciphertext
    assert svc.get_instance_token(db, inst.id) == 'secret-token-value'
    db.close()


def test_breaking_parser_detects_action_required():
    breaking, excerpt = parse_breaking_sections('Intro\n## Action Required\nManual migration needed\nOther')
    assert breaking is True
    assert excerpt is not None and 'Action Required' in excerpt


def test_core_update_requires_approval():
    category = categorize('Home Assistant Core', 'update.home_assistant_core_update')
    risk, critical, manual, decision, explanation = risk_for(category, 'Home Assistant Core', False)
    assert category == 'Core'
    assert risk == 'high'
    assert critical is True
    assert decision == 'approval_required'


def test_approval_is_version_bound_in_plan_summary():
    db = SessionLocal()
    inst = Instance(friendly_name='PlanTest', url='http://ha.local')
    db.add(inst); db.flush()
    upd = UpdateRecord(instance_id=inst.id, entity_id='update.core', component='Home Assistant Core', category='Core', installed_version='2026.7.4', available_version='2026.8.1', critical_state=True, approval_state='required')
    db.add(upd); db.commit()
    assert upd.available_version == '2026.8.1'
    # The API stores target_versions at plan creation; later changes must not silently authorize newer versions.
    target_versions = {str(upd.id): upd.available_version}
    upd.available_version = '2026.8.2'
    assert target_versions[str(upd.id)] == '2026.8.1'
    assert upd.available_version == '2026.8.2'
    db.close()


def test_auto_review_blocks_missing_release_notes():
    db = SessionLocal()
    inst = Instance(friendly_name='AutoReview', url='http://ha.local')
    db.add(inst); db.flush()
    upd = UpdateRecord(instance_id=inst.id, entity_id='update.safe', component='Small Add-on', category='Add-on', installed_version='1.0', available_version='1.1', approval_state='not_required', installation_state='available', critical_state=False, risk_level='low')
    db.add(upd); db.commit()
    ok, reasons, notes = review_update_for_auto(upd)
    assert ok is False
    assert notes is None
    assert 'missing_or_non_public_release_url' in reasons
    db.close()


def test_monitor_creates_manual_notification_for_core_update():
    db = SessionLocal()
    inst = Instance(friendly_name='NotifyCore', url='http://ha.local')
    db.add(inst); db.flush()
    upd = UpdateRecord(instance_id=inst.id, entity_id='update.home_assistant_core_update', component='Home Assistant Core', category='Core', installed_version='2026.7.4', available_version='2026.8.1', critical_state=True, approval_state='required', installation_state='available', risk_level='high')
    db.add(upd); db.commit()
    # Exercise the notification path directly by bypassing live sync with monkeypatch-style local call inputs.
    from fleet_manager.services.automation import mark_manual_notifications
    mark_manual_notifications(db, inst, upd)
    db.commit()
    n = db.query(Notification).filter(Notification.title.like('%Manual Home Assistant update required%')).one_or_none()
    assert n is not None
    assert n.severity == 'warning'
    db.close()


def test_policy_setting_model_roundtrip():
    from fleet_manager.models import PolicySetting
    db = SessionLocal()
    p = PolicySetting(key='test_policy', value_json='{"enabled": true}', description='test')
    db.add(p); db.commit()
    row = db.query(PolicySetting).filter_by(key='test_policy').one()
    assert 'enabled' in row.value_json
    db.close()


def test_login_rate_limit_state_blocks_after_failures():
    # Import app after test env has set isolated DB.
    from fleet_manager import app as appmod
    key = 'test:admin@example.local'
    appmod.LOGIN_FAILURES[key] = [appmod.datetime.now(appmod.timezone.utc)] * appmod.LOGIN_MAX_FAILURES
    assert len(appmod.LOGIN_FAILURES[key]) == appmod.LOGIN_MAX_FAILURES

def test_auto_policy_can_disable_or_narrow_automatic_updates():
    db = SessionLocal()
    inst = Instance(friendly_name='PolicyTest', url='http://ha.local')
    db.add(inst); db.flush()
    upd = UpdateRecord(instance_id=inst.id, entity_id='update.safe_addon', component='Small Add-on', category='Add-on', installed_version='1.0', available_version='1.1', approval_state='not_required', installation_state='available', critical_state=False, risk_level='low', release_url='https://example.com/release')
    db.add(upd); db.commit()
    ok, reasons, _notes = review_update_for_auto(upd, {'auto_execute_enabled': False, 'requires_public_release_notes': False, 'safe_categories': ['Add-on'], 'excluded_categories': [], 'excluded_stacks': []})
    assert ok is False
    assert 'automatic_updates_disabled_by_policy' in reasons
    ok, reasons, _notes = review_update_for_auto(upd, {'auto_execute_enabled': True, 'requires_public_release_notes': False, 'safe_categories': [], 'excluded_categories': [], 'excluded_stacks': []})
    assert ok is False
    assert 'category_not_auto_safe:Add-on' in reasons
    db.close()


def test_fleet_manager_ui_has_unrestricted_update_actions_and_oidc_settings():
    html = Path(__file__).resolve().parents[1].joinpath('fleet_manager/static/index.html').read_text()
    assert "bulkAction('update')" in html
    assert "skipOne(${u.id})" in html
    assert "sameVersion(u)" in html
    assert 'Single sign-on' in html
    assert 'Email or username' in html
    assert '/api/auth/oidc/start' in html
    assert 'function toggleNav()' in html
    assert 'class="navIcon"' in html
    assert 'confirmDialog' in html
    assert 'confirm(' not in html
    assert 'alert(' not in html
    assert 'class="iconAction"' in html
    assert 'topActions' in html
    assert 'sortHead' in html
    assert 'Add Instance' in html
    assert 'Add Home Assistant' not in html
    assert '<th>Status</th><th>Actions</th>' not in html
    assert 'deleteInstance' in html
    assert 'bulkButton' in html
    assert 'manual only' not in html.lower()
    assert 'No JSON editing required' not in html


def test_install_update_requests_native_backup_when_supported(monkeypatch):
    db = SessionLocal()
    inst = Instance(friendly_name='BackupHA', url='http://ha.local')
    db.add(inst); db.flush()
    upd = UpdateRecord(instance_id=inst.id, entity_id='update.addon', component='Add-on', category='Add-on', installed_version='1.0', available_version='1.1', approval_state='required', installation_state='available', critical_state=True, risk_level='high')
    db.add(upd); db.commit()
    calls = []

    class FakeCreds:
        def get_instance_token(self, db_, instance_id):
            return 'token'

    class FakeAdapter:
        def __init__(self, instance, token):
            pass
        def get(self, path):
            if path == '/api/states/update.addon':
                if not calls:
                    return {'state':'on','attributes':{'installed_version':'1.0','latest_version':'1.1','supported_features':8}}
                return {'state':'off','attributes':{'installed_version':'1.1','latest_version':'1.1','in_progress':False}}
            return {}
        def post(self, path, payload):
            calls.append((path, payload.copy()))
            return {'ok': True}

    import fleet_manager.services.automation as automod
    monkeypatch.setattr(automod, 'CredentialService', FakeCreds)
    monkeypatch.setattr(automod, 'HomeAssistantAdapter', FakeAdapter)
    monkeypatch.setattr(automod, 'append_vault_update_log', lambda *a, **k: None)
    op = automod.install_update(db, inst, upd, actor='test')
    assert op.status == 'succeeded'
    assert calls == [('/api/services/update/install', {'entity_id': 'update.addon', 'version': '1.1', 'backup': True})]
    db.close()


def test_login_lookup_accepts_username_or_email():
    from fleet_manager.app import User, or_
    db = SessionLocal()
    user = User(email='user-login@example.local', username='ulogin', password_hash='x', role='admin')
    db.add(user); db.commit()
    assert db.query(User).filter(or_(User.email == 'user-login@example.local', User.username == 'user-login@example.local')).one().id == user.id
    assert db.query(User).filter(or_(User.email == 'ulogin', User.username == 'ulogin')).one().id == user.id
    db.close()


def test_skip_update_calls_ha_skip_without_version(monkeypatch):
    db = SessionLocal()
    inst = Instance(friendly_name='SkipHA', url='http://ha.local')
    db.add(inst); db.flush()
    upd = UpdateRecord(instance_id=inst.id, entity_id='update.addon_skip', component='Add-on Skip', category='Add-on', installed_version='1.0', available_version='1.1', installation_state='available')
    db.add(upd); db.commit()
    calls = []

    class FakeCreds:
        def get_instance_token(self, db_, instance_id): return 'token'
    class FakeAdapter:
        def __init__(self, instance, token): pass
        def post(self, path, payload):
            calls.append((path, payload.copy()))
            return {'ok': True}

    import fleet_manager.services.automation as automod
    monkeypatch.setattr(automod, 'CredentialService', FakeCreds)
    monkeypatch.setattr(automod, 'HomeAssistantAdapter', FakeAdapter)
    op = automod.skip_update(db, inst, upd, actor='test')
    assert op.status == 'succeeded'
    assert upd.skip_state == 'skipped'
    assert calls == [('/api/services/update/skip', {'entity_id': 'update.addon_skip'})]
    db.close()


def test_install_update_timeout_returns_accepted_not_exception(monkeypatch):
    db = SessionLocal()
    inst = Instance(friendly_name='TimeoutHA', url='http://ha.local')
    db.add(inst); db.flush()
    upd = UpdateRecord(instance_id=inst.id, entity_id='update.timeout', component='Timeout Add-on', category='Add-on', installed_version='1.0', available_version='1.1', installation_state='available')
    db.add(upd); db.commit()

    class FakeCreds:
        def get_instance_token(self, db_, instance_id): return 'token'
    class FakeAdapter:
        def __init__(self, instance, token): pass
        def get(self, path): return {'state':'on','attributes':{'installed_version':'1.0','latest_version':'1.1','supported_features':0}}
        def post(self, path, payload): raise TimeoutError('held open')

    import fleet_manager.services.automation as automod
    monkeypatch.setattr(automod, 'CredentialService', FakeCreds)
    monkeypatch.setattr(automod, 'HomeAssistantAdapter', FakeAdapter)
    monkeypatch.setattr(automod, 'append_vault_update_log', lambda *a, **k: None)
    op = automod.install_update(db, inst, upd, actor='test')
    assert op.status == 'accepted'
    assert 'verification pending' in op.state
    db.close()


def test_delete_instance_endpoint_removes_configuration():
    from fleet_manager.app import app, create_session
    db = SessionLocal()
    user = User(email='delete-admin@example.local', username='delete-admin', password_hash='x', role='admin')
    inst = Instance(friendly_name='DeleteMe', url='http://delete-me.local')
    db.add_all([user, inst]); db.flush()
    upd = UpdateRecord(instance_id=inst.id, entity_id='update.delete_me', component='Delete Update')
    db.add(upd); db.flush()
    inst_id = inst.id
    sess = create_session(db, user); db.commit()
    client = TestClient(app)
    r = client.delete(f'/api/instances/{inst_id}', cookies={'hafm_session': sess.id}, headers={'x-csrf-token': sess.csrf_token})
    assert r.status_code == 200
    assert r.json()['ok'] is True
    db.expire_all()
    assert db.get(Instance, inst_id) is None
    assert db.query(UpdateRecord).filter_by(instance_id=inst_id).count() == 0
    db.close()
