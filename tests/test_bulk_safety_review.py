import json
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from fleet_manager.models import JobRun, Operation, UpdateRecord, User
from fleet_manager.services import automation
from fleet_manager.services.reliability import create_operation, reconcile_operation
from test_bulk_repair import lab  # share the isolated bulk fixture and its explicit fake HA adapter


@pytest.mark.parametrize('repair_result', ['raise', 'malformed'])
def test_restart_hold_survives_unavailable_or_malformed_repair_inspection(lab, monkeypatch, repair_result):
    ids = lab.seed((2,))
    result = lab.queue([ids[1]])
    with lab.factory() as db:
        update = db.get(UpdateRecord, ids[0])
        inst = db.get(__import__('fleet_manager.models', fromlist=['Instance']).Instance, update.instance_id)
        update.restart_required = True
        op = create_operation(db, kind='update_install', instance=inst,
                              details={'update_id': update.id, 'entity_id': update.entity_id, 'target_version': '2', 'dispatch_protocol': 2})
        op.status = 'action_required'
        op.deadline_at = None
        opid, iid = op.id, inst.id
        db.commit()
    lab.state['update.h0_0'] = {'state': 'off', 'attributes': {'installed_version': '2', 'latest_version': '2', 'in_progress': False}}

    def unavailable(self):
        if repair_result == 'raise':
            raise TimeoutError('synthetic repair lookup unavailable')
        return {'issues': []}

    monkeypatch.setattr(lab.Adapter, 'list_repairs', unavailable)
    with lab.factory() as db:
        op = db.get(Operation, opid)
        reconcile_operation(db, op, adapter=lab.Adapter(SimpleNamespace(id=op.instance_id), 'synthetic'))
        assert op.status == 'action_required'
        assert db.get(UpdateRecord, ids[0]).restart_required is True
        db.commit()
    from fleet_manager.services import bulk
    bulk.run_cycle(lab.factory)
    assert lab.posts == []
    assert lab.summary(result['job_id'])['queued'] == 1
    monkeypatch.setattr(lab.Adapter, 'list_repairs', lambda self: [])
    with lab.factory() as db:
        reconcile_operation(db, db.get(Operation, opid), adapter=lab.Adapter(SimpleNamespace(id=iid), 'synthetic'))
        db.commit()
    bulk.run_cycle(lab.factory)
    assert len(lab.posts) == 1


@pytest.mark.parametrize('action', ['update', 'skip'])
@pytest.mark.parametrize('revocation', ['lease', 'human'])
def test_post_reservation_revocation_fences_install_and_skip(lab, monkeypatch, action, revocation):
    ids = lab.seed((1,))
    result = lab.queue(ids, action=action)
    def forbidden_post(self, endpoint, payload):
        lab.posts.append((endpoint, dict(payload)))
        raise AssertionError('a downstream POST crossed a revoked dispatch boundary')
    monkeypatch.setattr(lab.Adapter, 'post', forbidden_post)
    original_commit = Session.commit
    changed = False

    def commit(session):
        nonlocal changed
        value = original_commit(session)
        if changed:
            return value
        row = session.query(Operation).filter(Operation.status.in_(['accepted', 'verification_pending'])).first()
        if row and json.loads(row.details_json or '{}').get('dispatch_reserved'):
            changed = True
            if revocation == 'human':
                with lab.factory() as other:
                    other.get(User, lab.actor.user_id).is_active = False
                    other.commit()
            else:
                with lab.factory() as other:
                    job = other.get(JobRun, result['job_id'])
                    summary = json.loads(job.details_json)
                    summary['_lease'] = {'id': 'replacement-after-reservation', 'until': '2099-01-01T00:00:00+00:00'}
                    job.details_json = json.dumps(summary)
                    other.commit()
        return value

    monkeypatch.setattr(Session, 'commit', commit)
    bulk = __import__('fleet_manager.services.bulk', fromlist=['run_cycle'])
    bulk.run_cycle(lab.factory)
    assert changed, 'test did not reach durable post-reservation boundary'
    assert lab.posts == []
    summary = lab.summary(result['job_id'])
    assert summary['results'][0]['status'] != 'succeeded'
    if revocation == 'human':
        assert summary['results'][0]['status'] == 'blocked'
    else:
        with lab.factory() as db:
            raw = json.loads(db.get(JobRun, result['job_id']).details_json)
            assert raw['_lease']['id'] == 'replacement-after-reservation'
            assert raw['results'][0]['status'] == 'dispatching'


def test_reconciliation_does_not_complete_matching_version_while_entity_updating(lab):
    ids = lab.seed((1,))
    with lab.factory() as db:
        update = db.get(UpdateRecord, ids[0])
        inst = db.get(__import__('fleet_manager.models', fromlist=['Instance']).Instance, update.instance_id)
        op = create_operation(db, kind='update_install', instance=inst,
                              details={'update_id': update.id, 'entity_id': update.entity_id, 'target_version': '2'})
        op.status = 'accepted'
        class Adapter:
            def get(self, path):
                return {'state': 'updating', 'attributes': {'installed_version': '2'}}
        reconcile_operation(db, op, adapter=Adapter())
        assert op.status != 'succeeded'
        assert op.status == 'reconnecting'
