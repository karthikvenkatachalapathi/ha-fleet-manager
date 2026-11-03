import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from fleet_manager.db import Base
from fleet_manager.models import ApiPrincipal, ApiToken, Instance, JobRun, Operation, PolicySetting, UpdateRecord, User, now
from fleet_manager.services import automation, bulk
from fleet_manager.services.api_auth import _principal_snapshot, CURRENT_API_PRINCIPAL
from fleet_manager.services.reliability import create_operation, migrate_operation_columns, reconcile_operation, reconcile_startup


@pytest.fixture
def lab(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path/'fleet.db'), connect_args={'check_same_thread': False, 'timeout': 1})
    @event.listens_for(engine, 'connect')
    def pragmas(conn, _):
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=1000')
    Base.metadata.create_all(engine); migrate_operation_columns(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as db:
        user = User(email='admin@test.invalid', password_hash='synthetic', role='admin', is_active=True)
        db.add(user); db.commit(); uid = user.id
    actor = SimpleNamespace(user_id=uid, user=SimpleNamespace(email='admin@test.invalid', role='admin'))
    state, posts, probes = {}, [], []
    class Adapter:
        def __init__(self, inst, token): self.iid = inst.id
        def probe(self):
            # A real, separate SQLite writer must work during EVERY network step.
            with factory() as other:
                other.add(PolicySetting(key='probe-' + str(len(probes)), value_json='{}'))
                other.commit(); probes.append(True)
        def get(self, path):
            self.probe()
            entity = path.rsplit('/', 1)[-1]
            return json.loads(json.dumps(state[entity]))
        def post(self, path, payload):
            self.probe(); assert path in {'/api/services/update/install', '/api/services/update/skip'}
            posts.append((self.iid, dict(payload)))
            if path.endswith('/install'):
                state[payload['entity_id']]['attributes']['in_progress'] = True
            return {}
        def list_repairs(self): self.probe(); return []
    monkeypatch.setattr(automation, 'HomeAssistantAdapter', Adapter)
    monkeypatch.setattr(automation, 'CredentialService', lambda: SimpleNamespace(get_instance_token=lambda *_: 'synthetic'))
    monkeypatch.setattr(automation, 'append_vault_update_log', lambda *a, **k: None)
    def seed(counts=(2, 1)):
        ids = []
        with factory() as db:
            for home, count in enumerate(counts):
                inst = Instance(friendly_name=f'Home-{home}', url='http://ha.invalid')
                db.add(inst); db.flush()
                for n in range(count):
                    entity = f'update.h{home}_{n}'
                    u = UpdateRecord(instance_id=inst.id, entity_id=entity, component=entity, category='Add-on', installed_version='1', available_version='2', installation_state='available')
                    db.add(u); db.flush(); ids.append(u.id)
                    state[entity] = {'state': 'on', 'attributes': {'installed_version': '1', 'latest_version': '2', 'in_progress': False}}
            db.commit()
        return ids
    def queue(ids, action='update', submitter=None):
        with factory() as db: return bulk.enqueue(db, update_ids=ids, action=action, submitter=submitter or actor)
    def summary(jobid):
        with factory() as db: return bulk.read_summary(db, db.get(JobRun, jobid))
    def finish():
        for _, payload in posts:
            state[payload['entity_id']] = {'state': 'off', 'attributes': {'installed_version': '2', 'latest_version': '2', 'in_progress': False}}
        with factory() as db:
            reconcile_startup(db, adapter_factory=lambda iid: Adapter(SimpleNamespace(id=iid), 'synthetic'))
    yield SimpleNamespace(factory=factory, actor=actor, state=state, posts=posts, probes=probes, Adapter=Adapter, seed=seed, queue=queue, summary=summary, finish=finish)
    engine.dispose()


def test_enqueue_is_durable_fast_and_does_not_dispatch(lab):
    ids = lab.seed(); result = lab.queue([ids[0], ids[0], 999999])
    assert result['selected'] == 2 and result['queued'] == 1 and len(result['failed']) == 1
    assert lab.posts == [] and result['updated'] == 0
    assert '_submitter' not in result and '_lease' not in result
    assert lab.summary(result['job_id'])['results'][0]['status'] == 'queued'


def test_thirteen_items_across_three_homes_queue_not_conflict_and_finish(lab):
    ids = lab.seed((5, 4, 4)); result = lab.queue(ids); jobid = result['job_id']
    bulk.run_cycle(lab.factory)
    first = lab.summary(jobid)
    assert len(lab.posts) == 3 and len({iid for iid, _ in lab.posts}) == 3
    assert first['updated'] == 0 and first['running'] == 3 and first['queued'] == 10
    assert first['blocked'] == first['failed'] == []
    bulk.run_cycle(lab.factory)  # still in progress: no duplicate POST, no conflict failure
    assert len(lab.posts) == 3
    assert lab.summary(jobid)['blocked'] == []
    for _ in range(5):
        lab.finish(); bulk.run_cycle(lab.factory)
    lab.finish(); bulk.run_cycle(lab.factory)
    end = lab.summary(jobid)
    assert end['updated'] == end['selected'] == 13
    assert end['pending'] == 0 and end['status'] == 'succeeded'
    assert len(lab.posts) == len({p['entity_id'] for _, p in lab.posts}) == 13
    assert all(p['version'] == '2' for _, p in lab.posts)
    assert lab.probes
    with lab.factory() as db:
        assert all(u.installation_state == 'current' for u in db.query(UpdateRecord).all())


def test_concurrent_workers_do_not_duplicate_and_slow_home_releases_db(lab, monkeypatch):
    ids = lab.seed((1,)); result = lab.queue(ids)
    entered, release = threading.Event(), threading.Event()
    original = lab.Adapter.get
    def slow(self, path):
        entered.set(); assert release.wait(4)
        return original(self, path)
    monkeypatch.setattr(lab.Adapter, 'get', slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bulk.run_cycle, lab.factory)
        assert entered.wait(2)
        second = pool.submit(bulk.run_cycle, lab.factory)
        try:
            second.result(timeout=2)
            with lab.factory() as db:
                assert db.get(JobRun, result['job_id']) is not None
                db.add(PolicySetting(key='concurrent-ui-write', value_json='{}')); db.commit()
        finally:
            release.set()
        first.result(timeout=3)
    assert len(lab.posts) == 1


def test_target_version_change_and_revoked_human_block_without_dispatch(lab):
    ids = lab.seed((2,)); result = lab.queue(ids)
    with lab.factory() as db:
        db.get(UpdateRecord, ids[0]).available_version = '3'
        db.get(User, lab.actor.user_id).is_active = False; db.commit()
    bulk.run_cycle(lab.factory)
    final = lab.summary(result['job_id'])
    assert not lab.posts and len(final['blocked']) == 2


def test_api_token_revoked_while_queued_cannot_dispatch(lab):
    ids = lab.seed((1,))
    with lab.factory() as db:
        iid = db.get(UpdateRecord, ids[0]).instance_id
        p = ApiPrincipal(name='test', owner='test', purpose='test', role='admin', enabled=True, instance_ids_json=json.dumps([iid]))
        db.add(p); db.flush()
        token = ApiToken(id='synthetic-token-id', principal_id=p.id, verifier='0'*64, expires_at=now()+timedelta(days=1))
        db.add(token); db.commit(); principal = _principal_snapshot(db, p, token)
    result = lab.queue(ids, submitter=principal)
    with lab.factory() as db:
        db.get(ApiToken, token.id).revoked_at = now(); db.commit()
    bulk.run_cycle(lab.factory)
    assert not lab.posts and len(lab.summary(result['job_id'])['blocked']) == 1
    assert CURRENT_API_PRINCIPAL.get() is None


def test_expired_worker_recovers_existing_intent_without_post_replay(lab):
    ids = lab.seed((1,)); result = lab.queue(ids)
    with lab.factory() as db:
        job = db.get(JobRun, result['job_id']); summary = json.loads(job.details_json)
        inst = db.get(Instance, summary['results'][0]['instance'])
        op = create_operation(db, kind='update_install', instance=inst, batch_id=summary['batch_id'], details={'update_id': ids[0], 'entity_id': 'update.h0_0', 'target_version': '2'})
        op.status = 'accepted'; op.started_at = now()
        summary['results'][0]['status'] = 'dispatching'
        summary['_lease'] = {'id': 'lost-worker', 'until': (now()-timedelta(seconds=1)).isoformat()}
        job.status = 'running'; job.details_json = json.dumps(summary); db.commit(); operation_id = op.id
    bulk.run_cycle(lab.factory)
    row = lab.summary(result['job_id'])['results'][0]
    assert row['operation'] == operation_id and row['status'] == 'accepted'
    assert not lab.posts


def test_startup_keeps_old_but_resumable_bulk_intents(lab):
    ids = lab.seed((1,)); result = lab.queue(ids)
    with lab.factory() as db:
        job = db.get(JobRun, result['job_id']); job.status = 'running'; job.started_at = now()-timedelta(hours=1); db.commit()
        reconcile_startup(db, stale_after_seconds=1)
        assert db.get(JobRun, result['job_id']).status == 'running'
        assert json.loads(job.details_json)['results'][0]['target_version'] == '2'
    bulk.run_cycle(lab.factory)
    assert len(lab.posts) == 1


def test_post_timeout_never_falls_back_to_latest_or_replays(lab, monkeypatch):
    ids = lab.seed((1,))
    def failed(self, path, payload):
        lab.posts.append((self.iid, dict(payload))); raise TimeoutError('synthetic lost response')
    monkeypatch.setattr(lab.Adapter, 'post', failed)
    result = lab.queue(ids); bulk.run_cycle(lab.factory); bulk.run_cycle(lab.factory)
    assert len(lab.posts) == 1 and lab.posts[0][1]['version'] == '2'
    assert lab.summary(result['job_id'])['updated'] == 0


def test_pending_http_success_is_not_completed_and_deadline_is_bounded(lab):
    ids = lab.seed((1,)); result = lab.queue(ids); bulk.run_cycle(lab.factory)
    with lab.factory() as db:
        op = db.query(Operation).one(); assert op.status == 'accepted' and op.ended_at is None
        op.deadline_at = now()-timedelta(seconds=1)
        reconcile_operation(db, op, adapter=lab.Adapter(SimpleNamespace(id=op.instance_id), 'synthetic'))
        assert op.status == 'outcome_unknown'
        db.commit()
    assert lab.summary(result['job_id'])['updated'] == 0


def test_historical_batch_counters_follow_actual_operations(lab):
    ids = lab.seed((1,)); result = lab.queue(ids); bulk.run_cycle(lab.factory); lab.finish()
    with lab.factory() as db:
        job = db.get(JobRun, result['job_id']); summary = json.loads(job.details_json)
        summary.pop('schema_version'); summary['results'][0]['status'] = 'accepted'; summary['updated'] = 0
        job.details_json = json.dumps(summary); db.commit()
    assert lab.summary(result['job_id'])['updated'] == 1


def test_skip_does_not_write_or_post_when_queued_target_changed(lab):
    ids = lab.seed((1,)); result = lab.queue(ids, action='skip')
    lab.state['update.h0_0']['attributes']['latest_version'] = '3'
    bulk.run_cycle(lab.factory)
    assert not lab.posts and lab.summary(result['job_id'])['results'][0]['status'] == 'blocked'


def test_previous_unknown_blocks_only_its_home_not_other_homes(lab):
    ids = lab.seed((1, 1))
    with lab.factory() as db:
        inst = db.get(Instance, db.get(UpdateRecord, ids[0]).instance_id)
        op = create_operation(db, kind='update_install', instance=inst, details={'update_id': ids[0], 'dispatch_protocol': 2})
        op.status = 'outcome_unknown'; db.commit()
    result = lab.queue(ids); bulk.run_cycle(lab.factory)
    final = lab.summary(result['job_id'])
    assert len(lab.posts) == 1 and len(final['blocked']) == 1
    assert final['results'][0]['reason'].startswith('previous_outcome_unverified:')


def test_lost_lease_fences_stale_worker_before_post_and_progress_write(lab, monkeypatch):
    ids = lab.seed((1,)); result = lab.queue(ids)
    entered, release = threading.Event(), threading.Event()
    original = lab.Adapter.get
    def slow(self, path):
        entered.set(); assert release.wait(4)
        return original(self, path)
    monkeypatch.setattr(lab.Adapter, 'get', slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(bulk.run_cycle, lab.factory)
        assert entered.wait(2)
        try:
            with lab.factory() as db:
                job = db.get(JobRun, result['job_id']); summary = json.loads(job.details_json)
                summary['_lease'] = {'id': 'replacement-worker', 'until': (now()+timedelta(minutes=5)).isoformat()}
                job.details_json = json.dumps(summary); db.commit()
        finally:
            release.set()
        old.result(timeout=3)
    assert not lab.posts
    with lab.factory() as db:
        summary = json.loads(db.get(JobRun, result['job_id']).details_json)
        assert summary['_lease']['id'] == 'replacement-worker'
        assert summary['results'][0]['status'] == 'dispatching'


def test_two_batches_racing_for_same_home_wait_instead_of_failing(lab, monkeypatch):
    ids = lab.seed((2,)); one, two = lab.queue([ids[0]]), lab.queue([ids[1]])
    gate = threading.Barrier(2); original = automation.create_operation
    def race(*a, **k):
        gate.wait(timeout=4)
        return original(*a, **k)
    monkeypatch.setattr(automation, 'create_operation', race)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(bulk.run_cycle, lab.factory) for _ in range(2)]
        for f in futures: f.result(timeout=8)
    summaries = [lab.summary(j['job_id']) for j in [one, two]]
    assert len(lab.posts) == 1
    assert all(not s['failed'] and not s['blocked'] for s in summaries)
    assert sum(s['queued'] for s in summaries) == 1


def test_human_permission_revoked_during_preflight_prevents_post(lab, monkeypatch):
    ids = lab.seed((1,)); result = lab.queue(ids); original = lab.Adapter.get
    def revoke(self, path):
        with lab.factory() as db:
            db.get(User, lab.actor.user_id).is_active = False; db.commit()
        return original(self, path)
    monkeypatch.setattr(lab.Adapter, 'get', revoke)
    bulk.run_cycle(lab.factory)
    assert not lab.posts
    assert lab.summary(result['job_id'])['results'][0]['status'] == 'blocked'


def test_restart_required_holds_same_home_queue_until_verified_clear(lab, monkeypatch):
    ids = lab.seed((2,)); result = lab.queue(ids)
    repairs = [{'domain': 'homeassistant', 'issue_id': 'restart_required'}]
    monkeypatch.setattr(lab.Adapter, 'list_repairs', lambda self: list(repairs))
    bulk.run_cycle(lab.factory); lab.finish(); bulk.run_cycle(lab.factory)
    assert len(lab.posts) == 1
    assert lab.summary(result['job_id'])['action_required'] == 1
    repairs.clear(); lab.finish(); bulk.run_cycle(lab.factory)
    assert len(lab.posts) == 2


def test_http_202_acknowledgement_live_progress_and_read_ui_stay_responsive(lab):
    from fastapi.testclient import TestClient
    from fleet_manager import app as appmod
    ids = lab.seed((1,))
    def db_override():
        with lab.factory() as db:
            yield db
    old = dict(appmod.app.dependency_overrides)
    appmod.app.dependency_overrides[appmod.get_db] = db_override
    appmod.app.dependency_overrides[appmod.csrf] = lambda: lab.actor
    appmod.app.dependency_overrides[appmod.current_session] = lambda: lab.actor
    try:
        client = TestClient(appmod.app)
        response = client.post('/api/updates/bulk', json={'action': 'update', 'update_ids': ids})
        assert response.status_code == 202
        accepted = response.json()
        assert accepted['queued'] == 1 and accepted['updated'] == 0 and lab.posts == []
        assert client.get('/api/updates').status_code == 200
        bulk.run_cycle(lab.factory)
        running = next(j for j in client.get('/api/jobs').json() if j['id'] == accepted['job_id'])
        assert running['details']['updated'] == 0 and running['details']['pending'] == 1
        assert '_submitter' not in running['details'] and '_lease' not in running['details']
        lab.finish()
        verified = next(j for j in client.get('/api/jobs').json() if j['id'] == accepted['job_id'])
        assert verified['details']['updated'] == 1 and verified['status'] == 'succeeded'
        updates = client.get('/api/updates').json()
        assert updates[0]['installed_version'] == '2'
    finally:
        appmod.app.dependency_overrides.clear(); appmod.app.dependency_overrides.update(old)

