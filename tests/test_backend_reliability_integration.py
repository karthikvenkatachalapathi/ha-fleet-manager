import json
from datetime import timedelta

import pytest

from fleet_manager.db import Base, SessionLocal, engine
from fleet_manager.models import Instance, UpdateRecord, Operation, now
from fleet_manager.services import automation
from fleet_manager.services.reliability import migrate_operation_columns


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


def test_install_records_operation_before_home_assistant_mutation(db, monkeypatch):
    inst = Instance(friendly_name="HA", url="http://ha.local")
    upd = UpdateRecord(instance_id=1, entity_id="update.demo", component="Demo", available_version="2", installation_state="available")
    db.add(inst)
    db.flush()
    upd.instance_id = inst.id
    db.add(upd)
    db.flush()
    calls = []

    class Adapter:
        def __init__(self, *_): pass
        def get(self, path):
            calls.append(("get", path))
            return {"state": "on", "attributes": {"latest_version": "2", "installed_version": "1", "supported_features": 0}}
        def post(self, path, payload):
            assert db.query(Operation).count() == 1
            calls.append(("post", path))
            return {}

    monkeypatch.setattr(automation, "HomeAssistantAdapter", Adapter)
    monkeypatch.setattr(automation, "CredentialService", lambda: type("C", (), {"get_instance_token": lambda self, db, iid: "token"})())
    monkeypatch.setattr(automation, "_poll_update_install_result", lambda *a, **k: (True, {"state": "on"}, {"installed_version": "2", "latest_version": "2"}, []))
    monkeypatch.setattr(automation, "append_vault_update_log", lambda *a, **k: None)

    op = automation.install_update(db, inst, upd)
    assert op.idempotency_key == f"update_install:{inst.id}:{upd.entity_id}:2"
    assert calls[0][0] == "get"


def test_skip_does_not_claim_success_when_state_cannot_be_verified(db, monkeypatch):
    inst = Instance(friendly_name="HA", url="http://ha.local")
    db.add(inst)
    db.flush()
    upd = UpdateRecord(instance_id=inst.id, entity_id="update.demo", component="Demo", available_version="2", installation_state="available")
    db.add(upd)
    db.flush()

    class Adapter:
        def __init__(self, *_): pass
        def post(self, *_): return {}
        def get(self, *_): raise TimeoutError("ambiguous")

    monkeypatch.setattr(automation, "HomeAssistantAdapter", Adapter)
    monkeypatch.setattr(automation, "CredentialService", lambda: type("C", (), {"get_instance_token": lambda self, db, iid: "token"})())
    monkeypatch.setattr(automation, "append_vault_update_log", lambda *a, **k: None)
    op = automation.skip_update(db, inst, upd)
    assert op.status in {"verification_pending", "outcome_unknown"}
    assert upd.skip_state != "skipped"


def test_migration_creates_idempotency_index_idempotently(db):
    migrate_operation_columns(engine)
    migrate_operation_columns(engine)
    indexes = {row[1] for row in engine.connect().exec_driver_sql("PRAGMA index_list(operations)").all()}
    assert "uq_operations_active_idempotency" in indexes
