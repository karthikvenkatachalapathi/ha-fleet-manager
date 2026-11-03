"""Legacy history must not silently introduce a new fleet-wide update prohibition."""
import pytest
from test_bulk_repair import lab
from fleet_manager.models import Instance, Operation, UpdateRecord
from fleet_manager.services import bulk
from fleet_manager.services.reliability import create_operation


def test_legacy_unknown_unrelated_target_preserves_previous_eligibility(lab):
    ids = lab.seed((1,))
    with lab.factory() as db:
        u = db.get(UpdateRecord, ids[0]); inst = db.get(Instance, u.instance_id)
        old = create_operation(db, kind='update_install', instance=inst, idempotency_key='legacy-old-target',
              details={'entity_id': u.entity_id, 'target_version': 'old-version', 'update_id': u.id})
        old.status = 'outcome_unknown'; db.commit()
    result = lab.queue(ids); bulk.run_cycle(lab.factory)
    assert len(lab.posts) == 1
    with lab.factory() as db:
        import json
        new = db.query(Operation).filter(Operation.batch_id == result['batch_id']).one()
        assert json.loads(new.details_json)['dispatch_protocol'] == 2


def test_legacy_unknown_exact_target_is_never_reposted(lab):
    ids = lab.seed((1,))
    with lab.factory() as db:
        u = db.get(UpdateRecord, ids[0]); inst = db.get(Instance, u.instance_id)
        old = create_operation(db, kind='update_install', instance=inst,
              idempotency_key=f'update_install:{inst.id}:{u.entity_id}:2',
              details={'entity_id': u.entity_id, 'target_version': '2', 'update_id': u.id})
        old.status = 'outcome_unknown'; db.commit(); oid = old.id
    result = lab.queue(ids); bulk.run_cycle(lab.factory)
    assert lab.posts == []
    row = lab.summary(result['job_id'])['results'][0]
    assert row['operation'] == oid and row['status'] == 'outcome_unknown'



def test_archived_terminal_history_does_not_reacquire_active_constraints_or_network(lab):
    from fleet_manager.services.reliability import reconcile_startup
    ids = lab.seed((1,))
    with lab.factory() as db:
        u = db.get(UpdateRecord, ids[0]); inst = db.get(Instance, u.instance_id)
        for n, status in enumerate(['action_required', 'action_required', 'outcome_unknown']):
            op = create_operation(db, kind='update_install', instance=inst,
                 idempotency_key=f'archived-{n}', details={'entity_id': u.entity_id, 'target_version': f'old-{n}'})
            op.status = status; op.deadline_at = None; db.flush()
        db.commit()
        def forbidden(_): raise AssertionError('archived terminal rows must not fetch HA on startup')
        result = reconcile_startup(db, adapter_factory=forbidden)
        assert result['operations'] == 0 and result['diagnostics'] == []
        assert [op.status for op in db.query(Operation).order_by(Operation.id)] == ['action_required', 'action_required', 'outcome_unknown']
    result = lab.queue(ids); bulk.run_cycle(lab.factory)
    assert len(lab.posts) == 1 and not lab.summary(result['job_id'])['blocked']


@pytest.mark.parametrize('state', ['on', 'updating'])
def test_terminal_durable_unknown_never_reacquires_a_held_active_slot(lab, state):
    from fleet_manager.services.reliability import reconcile_operation
    ids = lab.seed((2,))
    with lab.factory() as db:
        u = db.get(UpdateRecord, ids[0]); inst = db.get(Instance, u.instance_id)
        old = create_operation(db, kind='update_install', instance=inst,
             details={'entity_id': u.entity_id, 'target_version': '2', 'dispatch_protocol': 2})
        old.status = 'outcome_unknown'; db.flush()
        active = create_operation(db, kind='update_install', instance=inst,
             details={'entity_id': 'update.h0_1', 'target_version': '2', 'dispatch_protocol': 2})
        active.status = 'accepted'; db.commit()
        lab.state[u.entity_id] = {'state': state, 'attributes': {'installed_version': '1', 'latest_version': '2'}}
        reconcile_operation(db, old, adapter=lab.Adapter(inst, 'synthetic'))
        db.commit()  # a false active transition would violate the real unique index
        assert old.status == 'outcome_unknown' and active.status == 'accepted'
