"""Fail-closed bearer route matrix and per-request ORM instance scope."""
from __future__ import annotations
import re
from contextvars import ContextVar
CURRENT_API_ROUTE = ContextVar('api_route', default=None)
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria
from .. import models

_READ = {
    '/api/auth/whoami','/api/dashboard','/api/instances','/api/updates',
    '/api/repairs','/api/audit','/api/operations','/api/backups','/api/notifications','/api/approvals',
}
_GLOBAL_READ = {'/api/jobs','/api/schedules','/api/deployments','/api/policies/auto_update_policy'}
_ADMIN = {
    'POST': [r'/api/updates/[1-9][0-9]*/(?:approve|reject|install)',
             r'/api/updates/bulk',r'/api/instances/[1-9][0-9]*/(?:sync|restart|backup|replace-credential)',
             r'/api/repairs/fix',r'/api/operations/[1-9][0-9]*/retry',r'/api/notifications/(?:[1-9][0-9]*/)?ack'],
    'PATCH': [r'/api/instances/[1-9][0-9]*'],
    'DELETE': [r'/api/instances/[1-9][0-9]*'],
}
_GLOBAL_ADMIN = {
    ('POST','/api/instances'),('POST','/api/sync-all'),('POST','/api/deployments'),
    ('PUT','/api/policies/auto_update_policy'),
}

def full_fleet(principal):
    return principal.all_instances is True and principal.include_future_instances is True


def authorize_route(principal, method, path):
    """No implicit GET privilege and no future route automatically exposed."""
    if principal.role not in {'read','admin'}:
        raise HTTPException(403,'Permission denied')
    if method=='GET':
        if path in _READ:return
        if full_fleet(principal) and path in _GLOBAL_READ:
            if path.startswith('/api/policies') and principal.role!='admin':
                raise HTTPException(403,'Permission denied')
            return
    elif principal.role=='admin':
        if any(re.fullmatch(p,path) for p in _ADMIN.get(method,())):return
        if full_fleet(principal) and ((method,path) in _GLOBAL_ADMIN or
                (method=='PATCH' and re.fullmatch(r'/api/schedules/[1-9][0-9]*',path))):return
    raise HTTPException(403,'API token is not authorized for this operation')


_SCOPED = (models.Instance,models.InstanceCredential,models.UpdateRecord,models.Approval,
           models.Operation,models.BackupRecord,models.Notification,models.AuditEvent)


def bind_scope(db, principal):
    # Must happen before any target resource is loaded into this session.
    db.info['api_principal']=principal


@event.listens_for(Session,'do_orm_execute')
def _scope_queries(state):
    principal=state.session.info.get('api_principal')
    if principal is None or not state.is_select or full_fleet(principal):return
    ids=sorted(principal.instance_ids)
    for model in _SCOPED:
        column=model.id if model is models.Instance else model.instance_id
        state.statement=state.statement.options(with_loader_criteria(model,column.in_(ids),include_aliases=True))


def assert_live_access(instance_id=None, *, write=False):
    """Fresh separate DB read immediately before downstream dispatch. No new grants."""
    from .api_auth import CURRENT_API_PRINCIPAL, revalidate_principal
    from ..db import SessionLocal
    principal=CURRENT_API_PRINCIPAL.get()
    if principal is None:return
    with SessionLocal() as db:
        current=revalidate_principal(db,principal,instance_id=instance_id,
                             permission='execute_updates' if write else None)
        route=CURRENT_API_ROUTE.get()
        if write and route is not None:authorize_route(current,*route)


@event.listens_for(Session,'before_flush')
def _guard_flush(db, flush_context, instances):
    principal=db.info.get('api_principal')
    if principal is None:return
    changed=set(db.new)|set(db.dirty)|set(db.deleted)
    changed={x for x in changed if not isinstance(x,models.AuditEvent)}
    if not changed:return
    if principal.role!='admin':raise HTTPException(403,'Read token cannot modify Fleet state')
    assert_live_access(write=True)
    for obj in changed:
        if isinstance(obj,_SCOPED):
            iid=obj.id if isinstance(obj,models.Instance) else obj.instance_id
            if not full_fleet(principal) and iid not in principal.instance_ids:
                raise HTTPException(404,'Resource not found')
            assert_live_access(iid,write=True)
