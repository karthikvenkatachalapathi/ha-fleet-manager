from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from fleet_manager.db import Base
from fleet_manager.models import Instance, UpdateRecord, AuditEvent, Operation


def principal(role='read', full=False):
    return SimpleNamespace(role=role,instance_ids={1},all_instances=full,include_future_instances=full)


def test_read_route_allowlist_is_fail_closed():
    from fleet_manager.services.api_policy import authorize_route
    p=principal()
    for path in ['/api/dashboard','/api/instances','/api/updates','/api/repairs','/api/auth/whoami']:
        authorize_route(p,'GET',path)
    for method,path in [('GET','/api/updates/1/risk'),('GET','/api/instances/1/overrides'),('POST','/api/instances/1/sync'),('POST','/api/updates/bulk'),('GET','/api/policies'),('GET','/api/user/settings'),('GET','/api/access/identities'),('GET','/api/unknown'),('GET','/api/jobs')]:
        with pytest.raises(HTTPException):authorize_route(p,method,path)


def test_admin_global_controls_require_deliberate_future_all_fleet_scope():
    from fleet_manager.services.api_policy import authorize_route
    authorize_route(principal('admin'),'POST','/api/updates/1/install')
    with pytest.raises(HTTPException):authorize_route(principal('admin'),'PUT','/api/policies/auto_update_policy')
    authorize_route(principal('admin',True),'PUT','/api/policies/auto_update_policy')
    for path in ['/api/policies/oidc_settings','/api/access/identities','/api/user/settings','/api/oidc/settings']:
        with pytest.raises(HTTPException):authorize_route(principal('admin',True),'PUT',path)


def test_query_scope_filters_rows_counts_gets_and_null_global_audit():
    from fleet_manager.services.api_policy import bind_scope
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Instance(id=1,friendly_name='allowed',url='https://a.invalid'),Instance(id=2,friendly_name='private',url='https://b.invalid')])
        db.add_all([UpdateRecord(id=i,instance_id=i,entity_id='update.x',component='test') for i in (1,2)])
        db.add_all([AuditEvent(action='test',resource_type='instance',instance_id=i) for i in (None,1,2)])
        db.commit()
    with Session(engine) as db:
        bind_scope(db,principal())
        assert [x.id for x in db.query(Instance)]==[1]
        assert db.scalar(select(func.count()).select_from(UpdateRecord))==1
        assert db.get(Instance,2) is None
        assert db.get(UpdateRecord,2) is None
        assert [x.instance_id for x in db.query(AuditEvent)]==[1]
        assert db.query(UpdateRecord).filter(UpdateRecord.id.in_([1,2])).count()==1
