import json
import sys
from pathlib import Path
from datetime import timedelta
sys.path.insert(0, str(Path(__file__).parent))
from test_api_http import access_client
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from fleet_manager import app as appmod
from fleet_manager import access_routes as routes
from fleet_manager.models import Instance, ApiToken, ApiPrincipal, now
from fleet_manager.services.api_auth import hash_token

@pytest.fixture
def session_access(access_client):
    c,db,p,t,a,b,updates,user=access_client
    c.headers.pop('Authorization',None)
    session=appmod.create_session(db,user);db.commit()
    c.cookies.set('hafm_session',session.id)
    c.headers['x-csrf-token']=session.csrf_token
    with routes._LOCK:
        routes._PROOFS.clear();routes._FAILURES.clear()
    return c,db,p,t,a,b,updates,user

def reauth(c,password='fixture-password'):
    return c.post('/api/access/reauth',json={'password':password})

def create(c,ids=None,**kw):
    assert reauth(c).status_code==200
    r=c.post('/api/access/identities',json={'name':'new','purpose':'offline tests','instance_ids':ids or [],**kw})
    assert r.status_code==200,r.text
    assert r.headers['cache-control']=='no-store'
    return r.json()

def auth(c,token):
    return c.get('/api/auth/whoami',headers={'Authorization':'Bearer '+token})

def test_create_requires_single_use_reauth_and_never_lists_secret(session_access):
    c,db,p,t,a,b,updates,user=session_access
    payload={'name':'new','purpose':'tests','instance_ids':[a.id]}
    denied=c.post('/api/access/identities',json=payload)
    assert denied.status_code==401
    assert denied.headers.get('cache-control')=='no-store'
    assert reauth(c,'wrong').status_code==401
    result=create(c,[a.id]);token=result['token']
    assert token.startswith('fleet.')
    assert result['identity']['owner']==user.email
    assert db.get(ApiPrincipal,result['identity']['id']).created_by==user.id
    assert hash_token(token)==db.get(ApiToken,result['token_id']).verifier
    assert c.post('/api/access/identities',json=payload).status_code==401
    listing=c.get('/api/access/identities')
    assert listing.headers['cache-control']=='no-store'
    assert token not in listing.text and 'verifier' not in listing.text
    assert auth(c,token).status_code==200
    assert auth(c,token).json()['role']=='read'

def test_spoofed_legacy_owner_is_ignored(session_access):
    c,db,p,t,a,b,updates,user=session_access
    assert reauth(c).status_code==200
    r=c.post('/api/access/identities',json={'name':'spoof','owner':'attacker@example.invalid','purpose':'tests','instance_ids':[a.id]})
    assert r.status_code==200,r.text
    created=db.get(ApiPrincipal,r.json()['identity']['id'])
    assert created.owner==user.email
    assert created.created_by==user.id

def test_rotate_revokes_old_and_revoke_denies_new_immediately(session_access):
    c,db,p,t,a,b,*_=session_access
    result=create(c,[a.id]);old=result['token'];tid=result['token_id']
    assert reauth(c).status_code==200
    r=c.post(f'/api/access/tokens/{tid}/rotate',json={})
    assert r.status_code==200,r.text
    new=r.json()['token'];assert new!=old
    assert auth(c,old).status_code==401 and auth(c,new).status_code==200
    assert reauth(c).status_code==200
    assert c.post(f'/api/access/tokens/{tid}/rotate',json={}).status_code==409
    assert c.post(f"/api/access/tokens/{r.json()['token_id']}/revoke").status_code==200
    assert auth(c,new).status_code==401

def test_disable_needs_no_proof_enable_does_and_cannot_smuggle_escalation(session_access):
    c,db,p,t,a,b,*_=session_access
    result=create(c,[a.id]);url=f"/api/access/identities/{result['identity']['id']}";token=result['token']
    assert c.patch(url,json={'enabled':False,'role':'admin'}).status_code==401
    assert c.patch(url,json={'enabled':False}).status_code==200
    assert auth(c,token).status_code==401
    assert c.patch(url,json={'enabled':True}).status_code==401
    assert reauth(c).status_code==200
    assert c.patch(url,json={'enabled':True}).status_code==200
    assert auth(c,token).status_code==200

def test_all_current_scope_excludes_new_instance_and_preserves_snapshot_on_role_edit(session_access):
    c,db,p,t,a,b,*_=session_access
    expected=set(db.scalars(select(Instance.id)))
    result=create(c,all_instances=True);url=f"/api/access/identities/{result['identity']['id']}"
    assert set(result['identity']['instance_ids'])==expected
    added=Instance(friendly_name='later',url='https://later.invalid');db.add(added);db.commit()
    assert added.id not in auth(c,result['token']).json()['instance_ids']
    assert reauth(c).status_code==200
    r=c.patch(url,json={'role':'admin'});assert r.status_code==200,r.text
    assert set(r.json()['identity']['instance_ids'])==expected
    assert reauth(c).status_code==200
    r=c.patch(url,json={'include_future_instances':True});assert r.status_code==200,r.text
    assert added.id in auth(c,result['token']).json()['instance_ids']

def test_selected_to_snapshot_to_selected_transitions(session_access):
    c,db,p,t,a,b,*_=session_access
    result=create(c,[a.id]);url=f"/api/access/identities/{result['identity']['id']}"
    assert reauth(c).status_code==200
    r=c.patch(url,json={'all_instances':True});assert r.status_code==200,r.text
    assert set(r.json()['identity']['instance_ids'])==set(db.scalars(select(Instance.id)))
    assert reauth(c).status_code==200
    r=c.patch(url,json={'all_instances':False,'include_future_instances':False,'instance_ids':[b.id]});assert r.status_code==200,r.text
    assert auth(c,result['token']).json()['instance_ids']==[b.id]

def test_expiry_invalid_days_and_disabled_rotation(session_access):
    c,db,p,t,a,b,*_=session_access
    result=create(c,[a.id]);url=f"/api/access/tokens/{result['token_id']}/rotate"
    assert reauth(c).status_code==200
    assert c.post(url,json={'expires_in_days':0}).status_code==422
    assert auth(c,result['token']).status_code==200
    row=db.get(ApiToken,result['token_id']);row.expires_at=now()-timedelta(seconds=1);db.commit()
    assert auth(c,result['token']).status_code==401
    assert reauth(c).status_code==200
    assert c.post(url,json={}).status_code==409

def test_null_fields_and_invalid_scope_denied(session_access):
    c,db,p,t,a,b,*_=session_access
    for key in ['enabled','role','instance_ids','all_instances','include_future_instances']:
        assert c.patch(f'/api/access/identities/{p.id}',json={key:None}).status_code==422
    assert reauth(c).status_code==200
    assert c.post('/api/access/identities',json={'name':'n','owner':'o','purpose':'p','instance_ids':[a.id],'all_instances':True}).status_code==422

def test_http_csrf_nonadmin_and_beareradmin_denied(session_access):
    c,db,p,t,a,b,updates,user=session_access
    with TestClient(appmod.app,base_url='http://testserver') as plain:
        assert plain.post('/api/access/reauth',json={'password':'fixture-password'}).status_code==401
        plain.cookies.update(c.cookies);plain.headers['x-csrf-token']=c.headers['x-csrf-token']
        assert plain.post('/api/access/reauth',json={'password':'fixture-password'},headers={'x-forwarded-proto':'https'}).status_code==400
    assert c.post('/api/access/reauth',json={'password':'fixture-password'},headers={'x-csrf-token':'bad'}).status_code==403
    result=create(c,[a.id],role='admin')
    assert c.get('/api/access/identities',headers={'Authorization':'Bearer '+result['token']}).status_code==403
    user.role='read_only';db.commit()
    assert c.get('/api/access/identities').status_code==403
    assert reauth(c).status_code==403

def test_reauth_rate_limit_and_single_use_proof(session_access):
    c,db,p,t,a,b,*_=session_access
    for _ in range(5):assert reauth(c,'wrong').status_code==401
    assert reauth(c).status_code==429

def test_expired_failure_buckets_are_pruned_before_capacity_check(session_access,monkeypatch):
    c,*_=session_access
    monkeypatch.setattr(routes,'_MAX_STATE',2)
    with routes._LOCK:
        routes._FAILURES.update({('session','expired'):(now()-timedelta(minutes=16),5),('ip','expired'):(now()-timedelta(minutes=16),20)})
    assert reauth(c).status_code==200
    assert not routes._FAILURES


def test_parallel_password_failures_are_reserved_atomically(session_access, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    c,*_=session_access
    calls=[]
    def invalid(*args):
        calls.append(1);time.sleep(.1);return False
    monkeypatch.setattr(routes,'verify_password',invalid)
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses=list(pool.map(lambda _:reauth(c,'wrong').status_code,range(8)))
    assert statuses.count(401)==5 and statuses.count(429)==3
    assert len(calls)==5


def test_scope_contraction_applies_to_existing_token(session_access):
    c,db,p,t,a,b,*_=session_access
    result=create(c,[a.id,b.id],role='admin')
    assert reauth(c).status_code==200
    assert c.patch(f"/api/access/identities/{result['identity']['id']}",json={'role':'read','instance_ids':[b.id]}).status_code==200
    current=auth(c,result['token']);assert current.json()['role']=='read' and current.json()['instance_ids']==[b.id]
