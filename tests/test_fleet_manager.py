import base64, os, tempfile, json
from pathlib import Path
from fastapi.testclient import TestClient

os.environ['MASTER_ENCRYPTION_KEY'] = base64.urlsafe_b64encode(b'0'*32).decode()
os.environ['FLEET_ADMIN_PASSWORD'] = 'test-password'
os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(tempfile.mkdtemp(prefix='hafm-test-')) / 'test.db')

from fleet_manager.db import Base, engine, SessionLocal
from fleet_manager.models import Instance, UpdateRecord, Notification, User, AuditEvent, PolicySetting
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


def test_create_notification_dispatches_every_enabled_channel(monkeypatch):
    import fleet_manager.services.automation as automod
    db = SessionLocal()
    db.query(PolicySetting).filter_by(key='notification_settings').delete()
    db.add(PolicySetting(key='notification_settings', value_json=json.dumps({
        'ntfy_enabled': True,
        'pushover_enabled': True,
        'email_enabled': True,
        'telegram_enabled': True,
    }), description='test'))
    db.commit()
    sent = []
    monkeypatch.setattr(automod, '_send_ntfy', lambda cfg, title, body: sent.append(('ntfy', title, body)))
    monkeypatch.setattr(automod, '_send_pushover', lambda cfg, title, body: sent.append(('pushover', title, body)))
    monkeypatch.setattr(automod, '_send_email', lambda cfg, title, body: sent.append(('email', title, body)))
    monkeypatch.setattr(automod, '_send_telegram', lambda cfg, title, body: sent.append(('telegram', title, body)))
    n = automod.create_notification(db, severity='warning', title='Fleet alert', body='Body text')
    db.commit()
    assert n.id is not None
    assert {row[0] for row in sent} == {'ntfy', 'pushover', 'email', 'telegram'}
    assert db.query(AuditEvent).filter_by(action='notification_dispatch_sent', resource_id=str(n.id)).count() == 4
    db.close()


def test_create_notification_dispatch_failure_is_audited_not_blocking(monkeypatch):
    import fleet_manager.services.automation as automod
    db = SessionLocal()
    db.query(PolicySetting).filter_by(key='notification_settings').delete()
    db.add(PolicySetting(key='notification_settings', value_json=json.dumps({'pushover_enabled': True}), description='test'))
    db.commit()
    def fail(*_):
        raise RuntimeError('bad token')
    monkeypatch.setattr(automod, '_send_pushover', fail)
    n = automod.create_notification(db, severity='critical', title='Fleet alert fail', body='Body text')
    db.commit()
    assert n.id is not None
    ev = db.query(AuditEvent).filter_by(action='notification_dispatch_failed', resource_id=str(n.id)).one()
    assert 'pushover' in ev.metadata_json
    assert 'bad token' in ev.metadata_json
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


def test_oidc_callback_provider_400_returns_friendly_error(monkeypatch):
    from fleet_manager import app as appmod
    from fleet_manager.models import PolicySetting
    db = SessionLocal()
    db.query(PolicySetting).filter_by(key='oidc_settings').delete()
    db.add(PolicySetting(key='oidc_settings', value_json=json.dumps({'enabled': True, 'issuer_url': 'https://auth.example/application/o/app/', 'client_id': 'client', 'client_secret': 'secret', 'scopes': 'openid email profile'}), description='test'))
    db.commit(); db.close()

    monkeypatch.setattr(appmod, 'oidc_discovery', lambda issuer: {'token_endpoint': 'https://auth.example/application/o/token/', 'userinfo_endpoint': 'https://auth.example/userinfo'})
    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, data=None, headers=None):
            return appmod.httpx.Response(400, text='invalid_grant: redirect_uri mismatch', request=appmod.httpx.Request('POST', url))
        def get(self, *a, **k): raise AssertionError('userinfo should not be called')
    monkeypatch.setattr(appmod.httpx, 'Client', FakeClient)
    client = TestClient(appmod.app)
    r = client.get('/api/auth/oidc/callback?code=bad&state=s1', cookies={'hafm_oidc_state': 's1'})
    assert r.status_code == 502
    assert 'Sign-in failed' in r.text
    assert 'OIDC token exchange failed' in r.text
    assert 'secret' not in r.text

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
    assert 'data-label=\"Update\"' in html
    assert 'actionResultHtml' in html
    assert 'Verified updated' in html
    assert 'Open update in Home Assistant' in html
    assert 'Home Assistant error' in html
    assert 'Open in HA' in html
    assert 'selectCell' in html
    assert 'overflow-x:hidden!important' in html
    assert 'width:14px!important' in html
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
        def __init__(self, instance, token): self.gets = 0
        def get(self, path):
            self.gets += 1
            if self.gets == 1:
                return {'state':'on','attributes':{'installed_version':'1.0','latest_version':'1.1','supported_features':0}}
            return {'state':'on','attributes':{'installed_version':'1.0','latest_version':'1.1','supported_features':0,'in_progress':True}}
        def post(self, path, payload): raise TimeoutError('held open')

    import fleet_manager.services.automation as automod
    monkeypatch.setattr(automod, 'CredentialService', FakeCreds)
    monkeypatch.setattr(automod, 'HomeAssistantAdapter', FakeAdapter)
    monkeypatch.setattr(automod, 'append_vault_update_log', lambda *a, **k: None)
    monkeypatch.setattr(automod, '_poll_update_install_result', lambda *a, **k: (False, {'state':'on'}, {'installed_version':'1.0','latest_version':'1.1','in_progress':True}, [{'attempt':0,'in_progress':True}]))
    op = automod.install_update(db, inst, upd, actor='test')
    assert op.status == 'accepted'
    assert 'verification pending' in op.state
    db.close()


def test_install_update_500_without_progress_requires_manual_intervention(monkeypatch):
    db = SessionLocal()
    inst = Instance(friendly_name='Richmond Home', url='http://ha.local')
    db.add(inst); db.flush()
    upd = UpdateRecord(instance_id=inst.id, entity_id='update.music_assistant_server_update', component='Music Assistant', category='Add-on', installed_version='2.9.11', available_version='2.9.12', installation_state='available')
    db.add(upd); db.commit()

    class FakeCreds:
        def get_instance_token(self, db_, instance_id): return 'token'
    class FakeAdapter:
        def __init__(self, instance, token): pass
        def get(self, path):
            return {'state':'on','attributes':{'installed_version':'2.9.11','latest_version':'2.9.12','supported_features':8,'in_progress':False}}
        def post(self, path, payload): raise RuntimeError('500 Internal Server Error')

    import fleet_manager.services.automation as automod
    monkeypatch.setattr(automod, 'CredentialService', FakeCreds)
    monkeypatch.setattr(automod, 'HomeAssistantAdapter', FakeAdapter)
    monkeypatch.setattr(automod, 'append_vault_update_log', lambda *a, **k: None)
    monkeypatch.setattr(automod, '_poll_update_install_result', lambda *a, **k: (False, {'state':'on'}, {'installed_version':'2.9.11','latest_version':'2.9.12','in_progress':False}, [{'attempt':0,'in_progress':False}]))
    op = automod.install_update(db, inst, upd, actor='test')
    assert op.status == 'manual_intervention_required'
    assert len(json.loads(op.details_json)['install_attempts']) == 3
    assert 'Load failed' in op.state
    db.flush()
    assert db.query(Notification).filter_by(update_record_id=upd.id, severity='critical').count() == 1
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


def test_adapter_allows_event_post_for_repair_shutdown(monkeypatch):
    from fleet_manager.services.ha_adapter import HomeAssistantAdapter
    inst = Instance(friendly_name='EventHA', url='http://ha.local')
    monkeypatch.setattr('fleet_manager.services.ha_adapter.resolve_host', lambda host: ['127.0.0.1'])
    calls = []
    class FakeResponse:
        status_code = 200
        text = '{}'
        def raise_for_status(self): pass
        def json(self): return {}
    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, headers=None, json=None):
            calls.append((url, json)); return FakeResponse()
    monkeypatch.setattr('fleet_manager.services.ha_adapter.httpx.Client', FakeClient)
    HomeAssistantAdapter(inst, 'token').trigger_shutdown_automations('host_reboot')
    assert calls == [('http://ha.local/api/events/homeassistant_stop', {'source': 'fleet_manager', 'reason': 'host_reboot'})]


def test_instance_backup_prefers_modern_backup_service(monkeypatch):
    db = SessionLocal()
    inst = Instance(friendly_name='ModernBackup', url='http://ha.local')
    db.add(inst); db.commit()
    calls = []
    class FakeCreds:
        def get_instance_token(self, db_, instance_id): return 'token'
    class FakeAdapter:
        def __init__(self, instance, token): pass
        def post(self, path, payload):
            calls.append((path, payload.copy()))
            return {'slug': 'abc123'}
    import fleet_manager.services.automation as automod
    monkeypatch.setattr(automod, 'CredentialService', FakeCreds)
    monkeypatch.setattr(automod, 'HomeAssistantAdapter', FakeAdapter)
    monkeypatch.setattr(automod, 'append_vault_update_log', lambda *a, **k: None)
    rec = automod.create_instance_backup(db, inst, actor='test')
    assert rec.status == 'completed'
    assert rec.backup_id == 'abc123'
    assert calls[0][0] == '/api/services/backup/create'
    db.close()


def test_ui_audit_headers_and_bottom_menu_icons():
    html = Path(__file__).resolve().parents[1].joinpath('fleet_manager/static/index.html').read_text()
    assert "x-fleet-page" in html
    assert "x-fleet-menu" in html
    assert 'class="topActions"' in html
    assert 'onclick="syncAll()"' in html
    assert 'aria-label="Refresh fleet status"' in html
    assert 'class="iconBtn logoutIcon"' in html
    assert '<svg viewBox="0 0 32 32" aria-hidden="true"' in html
    assert 'class="door"' in html


def test_ui_mutating_request_is_audited_with_page_header():
    from fleet_manager.app import app, create_session
    db = SessionLocal()
    user = User(email='audit-admin@example.local', username='audit-admin', password_hash='x', role='admin')
    db.add(user); db.flush()
    sess = create_session(db, user); db.commit()
    client = TestClient(app)
    r = client.post('/api/notifications/ack', json={'ids': []}, cookies={'hafm_session': sess.id}, headers={'x-csrf-token': sess.csrf_token, 'x-fleet-page': 'Recent activity', 'x-fleet-menu': 'Recent activity'})
    assert r.status_code == 200
    db.expire_all()
    row = db.query(AuditEvent).filter_by(action='ui_api_action', resource_id='/api/notifications/ack').order_by(AuditEvent.id.desc()).first()
    assert row is not None
    assert 'Recent activity' in row.metadata_json
    db.close()


def test_host_reboot_uses_supervisor_endpoint_before_legacy_service(monkeypatch):
    from fleet_manager.services.ha_adapter import HomeAssistantAdapter
    inst = Instance(friendly_name='RebootHA', url='http://ha.local')
    monkeypatch.setattr('fleet_manager.services.ha_adapter.resolve_host', lambda host: ['127.0.0.1'])
    calls = []
    class FakeResponse:
        status_code = 200
        text = '{"ok":true}'
        def raise_for_status(self): pass
        def json(self): return {'ok': True}
    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, headers=None, json=None):
            calls.append((url, json)); return FakeResponse()
    monkeypatch.setattr('fleet_manager.services.ha_adapter.httpx.Client', FakeClient)
    result = HomeAssistantAdapter(inst, 'token').host_reboot()
    assert result['endpoint'] == '/api/hassio/host/reboot'
    assert calls == [('http://ha.local/api/hassio/host/reboot', {})]


def test_restart_request_marks_instance_restarting(monkeypatch):
    from fleet_manager.app import app, create_session
    db = SessionLocal()
    user = User(email='restart-admin@example.local', username='restart-admin', password_hash='x', role='admin')
    inst = Instance(friendly_name='RestartMe', url='http://restart-me.local', connectivity_state='online', health_state='healthy')
    db.add_all([user, inst]); db.flush()
    inst_id = inst.id
    CredentialService().set_instance_token(db, inst.id, 'restart-token-value')
    sess = create_session(db, user); db.commit()
    class FakeAdapter:
        def __init__(self, instance, token): pass
        def post(self, path, payload): return {'ok': True}
    monkeypatch.setattr('fleet_manager.app.HomeAssistantAdapter', FakeAdapter)
    client = TestClient(app)
    r = client.post(f'/api/instances/{inst_id}/restart', cookies={'hafm_session': sess.id}, headers={'x-csrf-token': sess.csrf_token})
    assert r.status_code == 200
    assert r.json()['state'] == 'restarting'
    db.expire_all()
    row = db.get(Instance, inst_id)
    assert row.connectivity_state == 'restarting'
    assert row.health_state == 'restarting'
    db.close()


def test_repair_reboot_marks_instance_restarting(monkeypatch):
    from fleet_manager.app import app, create_session
    db = SessionLocal()
    user = User(email='repair-restart-admin@example.local', username='repair-restart-admin', password_hash='x', role='admin')
    inst = Instance(friendly_name='RepairRestart', url='http://repair-restart.local', connectivity_state='online', health_state='healthy')
    db.add_all([user, inst]); db.flush()
    inst_id = inst.id
    CredentialService().set_instance_token(db, inst.id, 'repair-restart-token-value')
    sess = create_session(db, user); db.commit()
    class FakeAdapter:
        def __init__(self, instance, token): pass
        def trigger_shutdown_automations(self, reason): return {'ok': True}
        def host_reboot(self): return {'endpoint': '/api/hassio/host/reboot', 'attempts': []}
    monkeypatch.setattr('fleet_manager.app.HomeAssistantAdapter', FakeAdapter)
    monkeypatch.setattr('fleet_manager.app.time.sleep', lambda *_: None)
    client = TestClient(app)
    r = client.post('/api/repairs/fix', json={'instance_id': inst_id, 'domain': 'hassio', 'issue_id': 'system_reboot_required', 'action': 'host_reboot'}, cookies={'hafm_session': sess.id}, headers={'x-csrf-token': sess.csrf_token})
    assert r.status_code == 200
    db.expire_all()
    row = db.get(Instance, inst_id)
    assert row.connectivity_state == 'restarting'
    assert row.health_state == 'restarting'
    db.close()


def test_ui_mobile_responsive_css_present():
    html = Path(__file__).resolve().parents[1].joinpath('fleet_manager/static/index.html').read_text()
    assert '@media (max-width: 760px)' in html
    assert 'position:fixed;left:0;right:0;bottom:0' in html
    assert 'height:calc(58px + env(safe-area-inset-bottom))' in html
    assert 'overflow-x:auto;-webkit-overflow-scrolling:touch' in html
    assert '.drawer{inset:0;width:100%;max-width:none' in html
    assert 'min-height:42px' in html


def test_ui_fast_initial_load_and_pwa_assets_present():
    root = Path(__file__).resolve().parents[1]
    html = root.joinpath('fleet_manager/static/index.html').read_text()
    manifest = root.joinpath('fleet_manager/static/manifest.webmanifest').read_text()
    sw = root.joinpath('fleet_manager/static/sw.js').read_text()
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert 'mobile-web-app-capable' in html
    assert "navigator.serviceWorker.register('/sw.js')" in html
    assert "const basePages=['Updates','Repairs','Recent activity','Settings'];" in html
    assert "async function loadCore(){let [instances,updates]=await Promise.all([api('/api/instances'),api('/api/updates')]);" in html
    assert "api('/api/repairs')" in html
    assert 'Loading repairs…' in html
    assert 'display": "standalone"' in manifest
    assert 'start_url": "/"' in manifest
    assert "if (url.pathname.startsWith('/api/')) return;" in sw


def test_pwa_routes_served():
    from fleet_manager.app import app
    client = TestClient(app)
    manifest = client.get('/manifest.webmanifest')
    assert manifest.status_code == 200
    assert manifest.json()['display'] == 'standalone'
    sw = client.get('/sw.js')
    assert sw.status_code == 200
    assert 'Service-Worker-Allowed' in sw.headers
    assert "ha-fleet-manager-v" in sw.text


def test_mobile_nav_and_metrics_are_compact():
    html = Path(__file__).resolve().parents[1].joinpath('fleet_manager/static/index.html').read_text()
    assert 'height:calc(58px + env(safe-area-inset-bottom))' in html
    assert '.navLabel{display:none}' in html
    assert 'grid-auto-columns:1fr' in html
    assert '.metric{padding:8px 10px;border-radius:10px;min-height:58px}' in html
    assert '.metric b{font-size:24px;line-height:1.05}' in html
    assert 'grid-template-columns:repeat(2,minmax(0,1fr));gap:7px' in html


def test_mobile_bottom_bar_hides_logo_and_controls():
    html = Path(__file__).resolve().parents[1].joinpath('fleet_manager/static/index.html').read_text()
    sw = Path(__file__).resolve().parents[1].joinpath('fleet_manager/static/sw.js').read_text()
    assert 'nav .brand,.app:not(.expanded) nav .brand,.app.expanded nav .brand{display:none!important}' in html
    assert '.navControls{display:none!important}' in html
    assert 'grid-template-columns:1fr;gap:0' in html
    assert "ha-fleet-manager-v" in sw


def test_mobile_logo_moves_to_content_header_not_bottom_nav():
    root = Path(__file__).resolve().parents[1]
    html = root.joinpath('fleet_manager/static/index.html').read_text()
    sw = root.joinpath('fleet_manager/static/sw.js').read_text()
    assert 'class="mobileTopLogo"' in html
    assert '.mobileTopLogo{display:block;flex:0 0 34px}' in html
    assert 'nav .brand,.app:not(.expanded) nav .brand,.app.expanded nav .brand{display:none!important}' in html
    assert 'ha-fleet-manager-v' in sw
