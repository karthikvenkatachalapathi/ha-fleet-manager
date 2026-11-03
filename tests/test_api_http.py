import hashlib,json,secrets,uuid
from datetime import timedelta
import pytest
from fastapi.testclient import TestClient
from fleet_manager import app as appmod
from fleet_manager.db import SessionLocal,Base,engine
from fleet_manager.models import ApiPrincipal,ApiToken,Instance,UpdateRecord,AuditEvent,User,now

@pytest.fixture
def access_client(monkeypatch):
    Base.metadata.create_all(engine)
    # Preserve other test modules' fixtures but remove every row created here.
    from sqlalchemy import select, delete
    with engine.connect() as conn:
        original={table: list(conn.scalars(select(list(table.primary_key)[0])))
                  for table in Base.metadata.sorted_tables if len(table.primary_key)==1}
    db=SessionLocal()
    user=User(email=f'{uuid.uuid4()}@invalid.test',password_hash=appmod.ph.hash('fixture-password'),role='admin')
    a=Instance(friendly_name='allowed',url='https://a.invalid',ha_core_version='2026.1')
    b=Instance(friendly_name='hidden',url='https://b.invalid',ha_core_version='2026.1')
    db.add_all([user,a,b]);db.flush()
    updates=[UpdateRecord(instance_id=x.id,entity_id='update.fixture',component=x.friendly_name,installed_version='1',available_version='2') for x in [a,b]]
    db.add_all(updates)
    p=ApiPrincipal(name='fixture',owner='test',purpose='offline tests',role='read',instance_ids_json=json.dumps([a.id]),all_instances=False,include_future_instances=False,enabled=True,created_by=user.id)
    db.add(p);db.flush()
    tid=secrets.token_hex(12);raw=f'fleet.{tid}.{secrets.token_urlsafe(32)}'
    t=ApiToken(id=tid,principal_id=p.id,verifier=hashlib.sha256(raw.encode()).hexdigest(),expires_at=now()+timedelta(days=1),created_by=user.id)
    db.add(t);db.commit()
    def forbidden(*a,**kw):raise AssertionError('No HA requests permitted by this fixture')
    monkeypatch.setattr(appmod,'HomeAssistantAdapter',forbidden)
    monkeypatch.setattr(appmod,'refresh_pending_update_snapshot',forbidden)
    c=TestClient(appmod.app,base_url='https://testserver',headers={'Authorization':f'Bearer {raw}'})
    try:yield c,db,p,t,a,b,updates,user
    finally:
        c.close();db.close()
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                if table in original:
                    conn.execute(delete(table).where(list(table.primary_key)[0].not_in(original[table])))


def test_bearer_reads_are_scoped_and_cached(access_client):
    c,db,p,t,a,b,updates,user=access_client
    r=c.get('/api/auth/whoami');assert r.status_code==200,r.text
    assert r.json()['principal_id']==p.id
    assert r.json()['role']=='read'
    assert r.json()['instance_ids']==[a.id]
    assert [i['id'] for i in c.get('/api/instances').json()]==[a.id]
    assert [u['id'] for u in c.get('/api/updates').json()]==[updates[0].id]
    assert c.get('/api/dashboard').json()['totals']['instances']==1
    # Unshipped risk/override APIs are absent from the bearer allowlist.
    assert c.get(f'/api/updates/{updates[0].id}/risk').status_code==403
    assert c.get(f'/api/updates/{updates[1].id}/risk').status_code==403
    assert c.get(f'/api/instances/{a.id}/overrides').status_code==403
    assert c.get('/api/access/identities').status_code==403
    db.expire_all();assert db.get(ApiToken,t.id).last_used_at is not None


def test_read_never_mutates_even_on_read_named_post(access_client):
    c,db,p,t,a,b,updates,user=access_client
    for path,body in [(f'/api/instances/{a.id}/sync',{}),('/api/sync-all',{}),('/api/updates/bulk',{'action':'review','update_ids':[updates[0].id]}),(f'/api/updates/{updates[0].id}/risk/review',{}),(f'/api/instances/{a.id}/restart',{}),(f'/api/instances/{a.id}/backup',{}),('/api/notifications/ack',{})]:
        assert c.post(path,json=body).status_code==403,path


def test_invalid_bearer_never_falls_back_to_valid_cookie(access_client):
    c,db,p,t,a,b,updates,user=access_client
    session=appmod.create_session(db,user);db.commit();c.cookies.set('hafm_session',session.id)
    c.headers['Authorization']='Bearer definitely-invalid'
    assert c.get('/api/instances').status_code==401
    c.headers.pop('Authorization')
    assert c.get('/api/instances').status_code==200
    assert c.patch(f'/api/instances/{a.id}',json={'friendly_name':'x'}).status_code==403


def test_admin_scope_and_attribution_and_revocation(access_client):
    c,db,p,t,a,b,updates,user=access_client
    p.role='admin';db.commit()
    assert c.patch(f'/api/instances/{b.id}',json={'friendly_name':'escape'}).status_code==404
    assert c.patch(f'/api/instances/{a.id}',json={'friendly_name':'changed'}).status_code==200
    db.expire_all()
    event=db.query(AuditEvent).filter_by(action='instance_updated',instance_id=a.id).order_by(AuditEvent.id.desc()).first()
    assert event.actor_user_id is None
    assert json.loads(event.metadata_json)['api_principal_id']==p.id
    assert c.put('/api/policies/auto_update_policy',json={'value':{}}).status_code==403
    t.revoked_at=now();db.commit()
    assert c.get('/api/instances').status_code==401


def test_unexpected_handler_failure_is_sanitized_and_audited(access_client,monkeypatch):
    c,db,p,t,a,b,*_=access_client
    def broken(*args):raise RuntimeError('private diagnostic must not leak')
    monkeypatch.setattr(appmod,'serialize_instance',broken)
    r=c.get('/api/instances')
    assert r.status_code==500
    assert 'private diagnostic' not in r.text
    assert r.headers['cache-control']=='no-store'
    event=db.query(AuditEvent).filter_by(action='api_token_request').order_by(AuditEvent.id.desc()).first()
    assert json.loads(event.metadata_json)['status_code']==500


def test_tokens_require_verified_https_not_forged_forwarded_header(access_client):
    c,*_=access_client
    with TestClient(appmod.app,base_url='http://testserver',headers=dict(c.headers)) as plain:
        r=plain.get('/api/instances',headers={'x-forwarded-proto':'https'})
        assert r.status_code==403


def test_new_or_unmapped_routes_are_not_token_accessible(access_client):
    c,db,p,t,a,b,updates,user=access_client
    p.role='admin';p.all_instances=True;p.include_future_instances=True;db.commit()
    for path in ['/api/user/settings','/api/oidc/settings','/api/policies','/api/access/identities','/api/session']:
        assert c.get(path).status_code==403,path
    assert c.post('/api/access/reauth',json={'password':'fixture-password'}).status_code==403
