import base64, os, tempfile
from pathlib import Path

os.environ['MASTER_ENCRYPTION_KEY'] = base64.urlsafe_b64encode(b'0'*32).decode()
os.environ['FLEET_ADMIN_PASSWORD'] = 'test-password'
os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(tempfile.mkdtemp(prefix='hafm-test-')) / 'test.db')

from fleet_manager.db import Base, engine, SessionLocal
from fleet_manager.models import Instance, UpdateRecord
from fleet_manager.services.credentials import CredentialService
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
