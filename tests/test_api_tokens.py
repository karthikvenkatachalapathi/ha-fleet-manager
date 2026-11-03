from datetime import timedelta
import secrets
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session
from fleet_manager.db import Base, SessionLocal
from fleet_manager.models import ApiPrincipal, ApiToken, Instance, now
from fleet_manager.services.api_auth import authenticate_token, revalidate_principal, hash_token

@pytest.fixture
def db_session():
    Base.metadata.create_all(bind=SessionLocal.kw['bind'])
    db=SessionLocal()
    try:
        yield db
    finally:
        db.rollback(); db.close()

def test_token_authentication_and_revalidation(db_session):
    instance = Instance(friendly_name='One', url='http://ha.local')
    db_session.add(instance); db_session.flush()
    principal = ApiPrincipal(name='test', owner='owner', purpose='tests', role='read', instance_ids_json=f'[{instance.id}]', all_instances=False, include_future_instances=False, enabled=True)
    db_session.add(principal); db_session.flush()
    token_id='public-id'; raw=f'fleet.{token_id}.{secrets.token_urlsafe(32)}'
    token = ApiToken(id=token_id, principal_id=principal.id, verifier=hash_token(raw), created_by=1, expires_at=now()+timedelta(days=1))
    db_session.add(token); db_session.commit()
    snapshot = authenticate_token(db_session, raw)
    assert snapshot.instance_ids == {instance.id}
    assert snapshot.user.id == -principal.id
    assert revalidate_principal(db_session, snapshot, instance_id=instance.id).role == 'read'
    principal.enabled = False; db_session.commit()
    with pytest.raises(HTTPException) as exc:
        revalidate_principal(db_session, snapshot)
    assert exc.value.status_code == 401

def test_authentication_rejects_bad_token(db_session):
    with pytest.raises(HTTPException) as exc:
        authenticate_token(db_session, 'bad')
    assert exc.value.status_code == 401
