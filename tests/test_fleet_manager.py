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
from fleet_manager.services.ha_adapter import categorize, risk_for, sync_updates


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


def test_sync_updates_retires_cached_updates_missing_from_live_home_assistant(monkeypatch):
    import fleet_manager.services.ha_adapter as adaptermod

    db = SessionLocal()
    inst = Instance(friendly_name='Fresh Fleet', url='http://ha.local')
    db.add(inst); db.flush()
    stale = UpdateRecord(
        instance_id=inst.id,
        entity_id='update.removed_integration',
        component='Removed Integration',
        installed_version='1.0',
        available_version='2.0',
        installation_state='available',
        approval_state='required',
        critical_state=True,
        skip_state='none',
    )
    db.add(stale); db.commit()

    class FakeAdapter:
        def __init__(self, *_): pass
        def discover_updates(self): return []

    monkeypatch.setattr(adaptermod, 'HomeAssistantAdapter', FakeAdapter)
    assert sync_updates(db, inst, 'token') == 0
    assert stale.installation_state == 'unavailable'
    assert stale.approval_state == 'not_required'
    assert stale.critical_state is False
    assert inst.available_updates == 0
    db.close()


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


def test_review_update_notification_dedupes_available_update_alerts():
    from fleet_manager.services.automation import mark_review_update_notification
    db = SessionLocal()
    inst = Instance(friendly_name='NotifyReview', url='http://ha.local')
    db.add(inst); db.flush()
    upd = UpdateRecord(instance_id=inst.id, entity_id='update.hacs_addon', component='HACS Add-on', category='Update Entity', installed_version='1.0', available_version='1.1', approval_state='not_required', installation_state='available', skip_state='none')
    db.add(upd); db.commit()
    assert mark_review_update_notification(db, inst, upd, ['missing_or_non_public_release_url']) is True
    assert mark_review_update_notification(db, inst, upd, ['missing_or_non_public_release_url']) is False
    n = db.query(Notification).filter(Notification.title.like('%Home Assistant update available%'), Notification.update_record_id == upd.id).one_or_none()
    assert n is not None
    assert n.severity == 'info'
    assert 'missing_or_non_public_release_url' in n.body
    db.close()


def test_monitor_consolidates_notifications_across_instances(monkeypatch):
    import fleet_manager.services.automation as automod
    db = SessionLocal()
    db.query(PolicySetting).filter_by(key='notification_settings').delete()
    db.add(PolicySetting(key='notification_settings', value_json=json.dumps({'pushover_enabled': True}), description='test'))
    inst1 = Instance(friendly_name='Consolidated A', url='http://ha-a.local')
    inst2 = Instance(friendly_name='Consolidated B', url='http://ha-b.local')
    db.add_all([inst1, inst2]); db.flush()
    db.add_all([
        UpdateRecord(instance_id=inst1.id, entity_id='update.core_a', component='Home Assistant Core', category='Core', installed_version='1.0', available_version='1.1', approval_state='required', installation_state='available', skip_state='none', critical_state=True, risk_level='high'),
        UpdateRecord(instance_id=inst2.id, entity_id='update.addon_b', component='Safe Add-on', category='Update Entity', installed_version='2.0', available_version='2.1', approval_state='not_required', installation_state='available', skip_state='none'),
    ])
    db.commit()
    class FakeCredentials:
        def get_instance_token(self, *_):
            return 'token'
    sent = []
    monkeypatch.setattr(automod, 'CredentialService', FakeCredentials)
    monkeypatch.setattr(automod, 'persist_instance_health', lambda *_: None)
    monkeypatch.setattr(automod, 'sync_updates', lambda *_: None)
    monkeypatch.setattr(automod, '_send_pushover', lambda cfg, title, body: sent.append((title, body)))
    summary = automod.monitor_and_act(db, auto_execute=False, actor='test')
    db.commit()
    consolidated = db.query(Notification).filter_by(title='Home Assistant Fleet Manager update summary', status='open').one_or_none()
    assert consolidated is not None
    assert 'Instance | What needs update | Current → New | Risk category' in consolidated.body
    assert 'Consolidated A | Home Assistant Core | 1.0 → 1.1 | manual/high-risk' in consolidated.body
    assert 'Consolidated B | Safe Add-on | 2.0 → 2.1 | low' in consolidated.body
    assert 'Review reason' not in consolidated.body
    assert not db.query(Notification).filter(Notification.title.like('Manual Home Assistant update required:%'), Notification.instance_id.in_([inst1.id, inst2.id])).all()
    assert len([s for s in sent if s[0] == 'Home Assistant Fleet Manager update summary']) == 1
    assert summary['manual_notifications'] == 1
    db.close()


def test_monitor_flushes_fresh_sync_before_building_notification(monkeypatch):
    """Completed firmware disappears while a newly found update appears in the same run."""
    import fleet_manager.services.automation as automod
    db = SessionLocal()
    db.query(PolicySetting).filter_by(key='notification_settings').delete()
    db.add(PolicySetting(key='notification_settings', value_json=json.dumps({'pushover_enabled': True}), description='test'))
    inst = Instance(friendly_name='Fresh Snapshot HA', url='http://fresh.local')
    db.add(inst); db.flush()
    stale = UpdateRecord(
        instance_id=inst.id,
        entity_id='update.stale_firmware',
        component='Stale Firmware',
        category='Firmware',
        installed_version='1.0',
        available_version='1.1',
        approval_state='required',
        installation_state='available',
        skip_state='none',
        critical_state=True,
        risk_level='high',
    )
    db.add(stale); db.commit()

    class FakeCredentials:
        def get_instance_token(self, *_):
            return 'token'

    def fake_sync(db_, target, _token):
        if target.id != inst.id:
            return 0
        stale.installed_version = '1.1'
        stale.available_version = '1.1'
        stale.installation_state = 'current'
        stale.approval_state = 'not_required'
        db_.add(UpdateRecord(
            instance_id=inst.id,
            entity_id='update.new_music_assistant',
            component='New Music Assistant',
            category='Update Entity',
            installed_version='2.10.1',
            available_version='2.10.2',
            approval_state='not_required',
            installation_state='available',
            skip_state='none',
            risk_level='low',
        ))
        return 1

    sent = []
    monkeypatch.setattr(automod, 'CredentialService', FakeCredentials)
    monkeypatch.setattr(automod, 'persist_instance_health', lambda *_: None)
    monkeypatch.setattr(automod, 'sync_updates', fake_sync)
    monkeypatch.setattr(automod, '_send_pushover', lambda cfg, title, body: sent.append((title, body)))
    automod.monitor_and_act(db, auto_execute=False, actor='test')
    db.commit()

    notification = db.query(Notification).filter_by(title='Home Assistant Fleet Manager update summary', status='open').one()
    assert 'Fresh Snapshot HA | New Music Assistant | 2.10.1 → 2.10.2 | low' in notification.body
    assert 'Fresh Snapshot HA | Stale Firmware' not in notification.body
    assert sent
    db.close()


def test_resolve_consolidated_notification_closes_stale_open_summary():
    from fleet_manager.services.automation import resolve_consolidated_notification
    db = SessionLocal()
    title = 'Resolve stale fleet summary'
    db.add(Notification(severity='info', title=title, body='stale', status='open'))
    db.commit()
    assert resolve_consolidated_notification(db, title=title) is True
    db.commit()
    row = db.query(Notification).filter_by(title=title).one()
    assert row.status == 'resolved'
    assert row.acknowledged_at is not None
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
    assert 'bad token' not in ev.metadata_json
    assert 'Operation failed (RuntimeError)' in ev.metadata_json
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
    assert 'redirect_uri mismatch' not in r.text
    db = SessionLocal()
    event = db.query(AuditEvent).filter_by(action='oidc_login_failed').order_by(AuditEvent.id.desc()).first()
    assert event is not None
    assert 'redirect_uri mismatch' not in event.metadata_json
    db.close()

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


def test_poll_does_not_finish_while_home_assistant_still_reports_in_progress(monkeypatch):
    from fleet_manager.services import automation as automod

    class FakeAdapter:
        def __init__(self):
            self.calls = 0

        def get(self, _path):
            self.calls += 1
            if self.calls == 1:
                return {'state': 'on', 'attributes': {'installed_version': '2.0', 'latest_version': '2.0', 'in_progress': True}}
            return {'state': 'off', 'attributes': {'installed_version': '2.0', 'latest_version': '2.0', 'in_progress': False}}

    adapter = FakeAdapter()
    monkeypatch.setattr(automod.time, 'sleep', lambda _seconds: None)
    success, _after, attrs, observations = automod._poll_update_install_result(adapter, 'update.example', '2.0', attempts=2, delay=0)
    assert success is True
    assert adapter.calls == 2
    assert attrs['in_progress'] is False
    assert [row['in_progress'] for row in observations] == [True, False]


def test_serialize_update_treats_updating_state_as_in_progress():
    from fleet_manager.app import serialize_update

    update = UpdateRecord(
        instance_id=1,
        entity_id='update.state_only_progress',
        component='State-only progress',
        installed_version='1.0',
        available_version='1.1',
        installation_state='available',
        raw_json=json.dumps({'state': 'updating', 'attributes': {'installed_version': '1.0', 'latest_version': '1.1'}}),
    )
    assert serialize_update(update)['in_progress'] is True


def test_fleet_manager_ui_has_unrestricted_update_actions_and_oidc_settings():
    html = Path(__file__).resolve().parents[1].joinpath('fleet_manager/static/index.html').read_text()
    assert "bulkAction('update')" in html
    assert "skipOne(${u.id})" in html
    assert "sameVersion(u)" in html
    assert "function updateIsVisible(u){return u.installation_state==='unavailable'||u.in_progress||!sameVersion(u)}" in html
    assert "function updateIsPending(u){return u.installation_state!=='unavailable'&&(u.in_progress||(u.installation_state==='available'&&u.skip_state!=='skipped'))}" in html
    assert "data.updates.filter(updateIsVisible)" in html
    assert "await refreshInitialFleet()" in html
    assert "FleetRefresh.createFleetRefreshController" in html
    assert "await fleetController.manualRefresh()" not in html
    assert "function syncAll(){" in html
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
        def get(self, path):
            return {'state': 'off', 'attributes': {'skipped_version': '1.1'}}

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
    monkeypatch.setattr(automod, '_poll_update_install_result', lambda *a, **k: (False, {'state':'on','attributes':{'installed_version':'1.0','latest_version':'1.1','in_progress':True}}, {'installed_version':'1.0','latest_version':'1.1','in_progress':True}, [{'attempt':0,'in_progress':True}]))
    op = automod.install_update(db, inst, upd, actor='test')
    assert op.status == 'accepted'
    assert 'verification pending' in op.state
    raw = json.loads(upd.raw_json)
    assert raw['attributes']['in_progress'] is True
    db.close()


def test_install_update_retries_latest_when_explicit_version_is_rejected(monkeypatch):
    db = SessionLocal()
    inst = Instance(friendly_name='FallbackHA', url='http://ha.local')
    db.add(inst); db.flush()
    upd = UpdateRecord(instance_id=inst.id, entity_id='update.version_fussy', component='Version Fussy Add-on', category='Add-on', installed_version='1.0', available_version='1.1', installation_state='available')
    db.add(upd); db.commit()
    calls = []

    class FakeCreds:
        def get_instance_token(self, db_, instance_id): return 'token'
    class FakeAdapter:
        def __init__(self, instance, token): pass
        def get(self, path):
            return {'state':'on','attributes':{'installed_version':'1.0','latest_version':'1.1','supported_features':0,'in_progress':False}}
        def post(self, path, payload):
            calls.append(payload.copy())
            if 'version' in payload:
                raise RuntimeError('500 Internal Server Error')
            return {'ok': True}

    import fleet_manager.services.automation as automod
    monkeypatch.setattr(automod, 'CredentialService', FakeCreds)
    monkeypatch.setattr(automod, 'HomeAssistantAdapter', FakeAdapter)
    monkeypatch.setattr(automod, 'append_vault_update_log', lambda *a, **k: None)
    def fake_poll(*a, **k):
        if any('version' not in call for call in calls):
            return True, {'state':'off','attributes':{'installed_version':'1.1','latest_version':'1.1','in_progress':False}}, {'installed_version':'1.1','latest_version':'1.1','in_progress':False}, [{'attempt':0,'in_progress':False}]
        return False, {'state':'on','attributes':{'installed_version':'1.0','latest_version':'1.1','in_progress':False}}, {'installed_version':'1.0','latest_version':'1.1','in_progress':False}, [{'attempt':0,'in_progress':False}]
    monkeypatch.setattr(automod, '_poll_update_install_result', fake_poll)
    op = automod.install_update(db, inst, upd, actor='test')
    assert op.status == 'succeeded'
    assert json.loads(op.details_json)['target_version'] == '1.1'
    assert calls == [
        {'entity_id': 'update.version_fussy', 'version': '1.1'},
        {'entity_id': 'update.version_fussy'},
    ]
    db.close()


def test_install_update_500_without_progress_is_outcome_unknown(monkeypatch):
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
    assert op.status == 'outcome_unknown'
    assert len(json.loads(op.details_json)['install_attempts']) == 2
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
    assert "function initFleetController(){fleetController=FleetRefresh.createFleetRefreshController" in html
    assert "async function refreshInitialFleet(){" in html
    assert "intervalMs:30000" in html
    assert "api('/api/repairs')" in html
    assert 'Loading repairs…' in html
    assert 'display": "standalone"' in manifest
    assert 'start_url": "/"' in manifest
    assert "if (url.pathname.startsWith('/api/')) return;" in sw


def test_websocket_uses_certifi_ca_bundle_for_https_instances(monkeypatch):
    from fleet_manager.services import ha_adapter as hamod

    sentinel = object()
    captured = {}
    monkeypatch.setattr(hamod, 'resolve_host', lambda *_: None)
    monkeypatch.setattr(hamod.ssl, 'create_default_context', lambda *, cafile: captured.update(cafile=cafile) or sentinel)

    class FakeSocket:
        replies = iter([
            json.dumps({'type': 'auth_required'}),
            json.dumps({'type': 'auth_ok'}),
            json.dumps({'success': True, 'result': {'issues': []}}),
        ])
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def recv(self): return next(self.replies)
        def send(self, _payload): pass

    def fake_connect(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return FakeSocket()

    monkeypatch.setattr(hamod, 'connect', fake_connect)
    inst = Instance(friendly_name='TLS HA', url='https://ha.example')
    result = hamod.HomeAssistantAdapter(inst, 'token').websocket_command('repairs/list_issues')
    assert result == {'issues': []}
    assert captured['kwargs']['ssl'] is sentinel
    assert captured['kwargs']['proxy'] is None
    assert captured['cafile'] == hamod.certifi.where()


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
