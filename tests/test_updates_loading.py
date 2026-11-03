import json
import time
import uuid

from fastapi.testclient import TestClient
from fleet_manager import app as appmod
from fleet_manager.db import Base, SessionLocal, engine
from fleet_manager.models import Instance, UpdateRecord, User


def test_human_updates_read_does_not_wait_on_home_assistant_per_row(monkeypatch):
    Base.metadata.create_all(engine)
    db = SessionLocal()
    user = User(email=f'{uuid.uuid4()}@invalid.test', password_hash=appmod.ph.hash('fixture-password'), role='admin')
    inst = Instance(friendly_name='slow-ha', url='https://slow.invalid')
    db.add_all([user, inst]); db.flush()
    records = [UpdateRecord(instance_id=inst.id, entity_id=f'update.test_{i}', component=f'Update {i}', installation_state='available', raw_json='{}') for i in range(4)]
    db.add_all(records)
    session = appmod.create_session(db, user); db.commit()
    calls = []
    def slow_refresh(*args):
        calls.append(args[1].entity_id)
        time.sleep(0.05)
    monkeypatch.setattr(appmod, 'refresh_pending_update_snapshot', slow_refresh)
    client = TestClient(appmod.app, base_url='https://testserver', cookies={appmod.SESSION_COOKIE: session.id})
    started = time.perf_counter()
    try:
        response = client.get('/api/updates')
        elapsed = time.perf_counter() - started
    finally:
        client.close(); db.close()
    assert response.status_code == 200
    assert calls == [], f'UI read synchronously refreshed {len(calls)} rows'
    assert elapsed < 0.15, f'UI read blocked for {elapsed:.3f}s'
