"""Synthetic-data safety regressions; never contacts any managed Home Assistant."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from test_bulk_repair import lab
from fleet_manager.models import ApiPrincipal, ApiToken, Instance, Operation, UpdateRecord, now
from fleet_manager.services import automation, bulk, ha_adapter
from fleet_manager.services.api_auth import CURRENT_API_PRINCIPAL, _principal_snapshot
from fleet_manager.services.api_policy import CURRENT_API_ROUTE
from fleet_manager.services.reliability import create_operation, reconcile_operation


@pytest.mark.parametrize('response', [None, {}, {'issues': {}}, {'issues': [None]}, {'issues': [{}]}, {'issues': [{'domain':'x', 'issue_id':'r', 'translation_placeholders': 'invalid'}]}])
def test_native_malformed_repair_responses_are_never_verified_empty(response, monkeypatch):
    adapter = object.__new__(ha_adapter.HomeAssistantAdapter)
    monkeypatch.setattr(adapter, 'websocket_command', lambda _: response)
    assert automation._restart_repair_issues(adapter) is None


@pytest.mark.parametrize('failure', ['timeout', 'malformed'])
def test_immediate_install_cannot_clear_unknown_restart_inspection(lab, monkeypatch, failure):
    ids = lab.seed((2,)); result = lab.queue(ids)
    def complete(self, endpoint, payload):
        assert endpoint == '/api/services/update/install'
        self.probe(); lab.posts.append((self.iid, dict(payload)))
        lab.state[payload['entity_id']] = {'state': 'off', 'attributes': {'installed_version': '2', 'latest_version': '2'}}
        return {}
    def repairs(self):
        self.probe()
        if failure == 'timeout': raise TimeoutError('synthetic unavailable')
        return {'invalid': 'response'}
    monkeypatch.setattr(lab.Adapter, 'post', complete)
    monkeypatch.setattr(lab.Adapter, 'list_repairs', repairs)
    bulk.run_cycle(lab.factory); bulk.run_cycle(lab.factory)
    assert len(lab.posts) == 1
    assert lab.summary(result['job_id'])['updated'] == 0
    with lab.factory() as db:
        op = db.query(Operation).one()
        assert op.status == 'verification_pending' and op.ended_at is None
    monkeypatch.setattr(lab.Adapter, 'list_repairs', lambda self: [])
    lab.finish(); bulk.run_cycle(lab.factory)
    assert len(lab.posts) == 2 and lab.summary(result['job_id'])['updated'] == 2


@pytest.mark.parametrize('missing_row', [False, True])
def test_missing_restart_evidence_remains_fail_closed_past_deadline(lab, monkeypatch, missing_row):
    ids = lab.seed((1,))
    with lab.factory() as db:
        u = db.get(UpdateRecord, ids[0]); inst = db.get(Instance, u.instance_id)
        op = create_operation(db, kind='update_install', instance=inst,
             details={'update_id': u.id, 'entity_id': u.entity_id, 'target_version': '2'})
        op.status = 'accepted'; op.deadline_at = now()-timedelta(seconds=1)
        if missing_row: db.delete(u)
        db.commit(); opid = op.id
        monkeypatch.setattr(lab.Adapter, 'list_repairs', lambda self: None)
        lab.state['update.h0_0'] = {'state':'off','attributes':{'installed_version':'2','latest_version':'2'}}
        reconcile_operation(db, op, adapter=lab.Adapter(SimpleNamespace(id=inst.id),'synthetic'))
        assert op.status == 'outcome_unknown'
        assert json.loads(op.details_json)['verified'] is False
        assert op.error_code == 'restart_clearance_unverified'


@pytest.mark.parametrize('action', ['install', 'skip'])
@pytest.mark.parametrize('change', ['none', 'revoke', 'disable', 'read', 'scope'])
def test_real_native_post_boundary_revalidates_actual_bearer_records(lab, monkeypatch, action, change):
    """Use real token/scope validation, but stub HTTP before any socket can open."""
    from fleet_manager import db as dbmod
    monkeypatch.setattr(dbmod, 'SessionLocal', lab.factory)
    ids = lab.seed((1,))
    with lab.factory() as db:
        iid = db.get(UpdateRecord, ids[0]).instance_id
        p = ApiPrincipal(name='synthetic', owner='synthetic', purpose='test', role='admin', enabled=True, instance_ids_json=json.dumps([iid]))
        db.add(p); db.flush()
        t = ApiToken(id='synthetic-native-boundary', principal_id=p.id, verifier='0'*64, expires_at=now()+timedelta(days=1))
        db.add(t); db.commit(); principal = _principal_snapshot(db, p, t)
        if change == 'revoke': t.revoked_at = now()
        elif change == 'disable': p.enabled = False
        elif change == 'read': p.role = 'read'
        elif change == 'scope': p.instance_ids_json = '[]'
        db.commit()
    calls = []
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            calls.append((url, kwargs['json']))
            return SimpleNamespace(status_code=200, text='', raise_for_status=lambda: None)
    monkeypatch.setattr(ha_adapter.httpx, 'Client', lambda **kwargs: Client())
    adapter = object.__new__(ha_adapter.HomeAssistantAdapter)
    adapter.instance = SimpleNamespace(id=iid); adapter.base = 'https://synthetic.invalid'; adapter.token = 'synthetic'
    ctx = CURRENT_API_PRINCIPAL.set(principal); route = CURRENT_API_ROUTE.set(('POST', '/api/updates/bulk'))
    try:
        payload = {'entity_id': 'update.synthetic', **({'version': '2'} if action == 'install' else {})}
        if change == 'none':
            adapter.post('/api/services/update/'+action, payload)
            assert calls == [('https://synthetic.invalid/api/services/update/'+action, payload)]
        else:
            with pytest.raises(HTTPException): adapter.post('/api/services/update/'+action, payload)
            assert calls == []
    finally:
        CURRENT_API_PRINCIPAL.reset(ctx); CURRENT_API_ROUTE.reset(route)
    assert CURRENT_API_PRINCIPAL.get() is None
