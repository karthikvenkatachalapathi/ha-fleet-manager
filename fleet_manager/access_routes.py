"""Session-authenticated API credential lifecycle management."""
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Literal
from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy import select, update
from .db import get_db
from .models import ApiPrincipal, ApiToken, AuditEvent, Instance, now
from .services.api_auth import hash_token
from .services.auth import verify_password

_PROOFS = {}
_FAILURES = {}
_LOCK = threading.Lock()
_MAX_STATE = 10000

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class CreateIdentity(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    owner: str | None = Field(default=None, min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=500)
    role: Literal['read','admin'] = 'read'
    instance_ids: list[StrictInt] = Field(default_factory=list)
    all_instances: StrictBool = False
    include_future_instances: StrictBool = False
    expires_in_days: StrictInt | None = None

class EditIdentity(StrictModel):
    enabled: StrictBool = None
    role: Literal['read','admin'] = None
    instance_ids: list[StrictInt] = None
    all_instances: StrictBool = None
    include_future_instances: StrictBool = None

class Reauth(StrictModel):
    password: str = Field(min_length=1, max_length=1024)

class RotateToken(StrictModel):
    expires_in_days: StrictInt | None = None

def _public(p, tokens):
    return {'id':p.id,'name':p.name,'owner':p.owner,'purpose':p.purpose,'role':p.role,'instance_ids':json.loads(p.instance_ids_json or '[]'),'all_instances':p.all_instances,'include_future_instances':p.include_future_instances,'enabled':p.enabled,'created_at':p.created_at.isoformat() if p.created_at else None,'tokens':[{'id':t.id,'created_at':t.created_at.isoformat() if t.created_at else None,'expires_at':t.expires_at.isoformat(),'revoked_at':t.revoked_at.isoformat() if t.revoked_at else None,'last_used_at':t.last_used_at.isoformat() if t.last_used_at else None} for t in tokens]}

def register_access_routes(app, current_session, csrf):
    def session_dep(request: Request, s=Depends(current_session)):
        if 'authorization' in request.headers: raise HTTPException(400, 'Authorization header is not allowed')
        if not s or not getattr(s,'user',None) or s.user.role != 'admin' or not s.user.is_active: raise HTTPException(403, 'Active admin session required')
        return s
    def secure(request):
        if request.url.scheme != 'https': raise HTTPException(400, 'HTTPS required')
    def write_guard(request, session):
        secure(request)
        result=csrf(request, session)
        if result is False: raise HTTPException(403, 'CSRF validation failed')
    def proof(request, session):
        key=(session.id, request.client.host if request.client else '')
        with _LOCK: ts=_PROOFS.pop(key, None)
        if ts and ts.tzinfo is None: ts=ts.replace(tzinfo=timezone.utc)
        if not ts or (datetime.now(timezone.utc)-ts).total_seconds()>300: raise HTTPException(401,'Recent password verification required')
        return key
    def audit(db, session, action, pid):
        db.add(AuditEvent(actor_user_id=session.user.id,action=action,resource_type='api_principal',resource_id=str(pid),result='success',after_summary='{}'))
    def issue(db, principal, session, days):
        days=days if days is not None else (30 if principal.role=='admin' else 90)
        if days < 1 or days > 365: raise HTTPException(422,'expires_in_days must be between 1 and 365')
        token_id=secrets.token_urlsafe(12); raw=f'fleet.{token_id}.{secrets.token_urlsafe(32)}'; expires=datetime.now(timezone.utc)+timedelta(days=days)
        db.add(ApiToken(id=token_id,principal_id=principal.id,verifier=hash_token(raw),created_by=session.user.id,expires_at=expires))
        return raw, token_id, expires
    def validate_scope(db, ids, all_instances, future):
        if future and not all_instances: raise HTTPException(422,'Future scope requires all_instances')
        if all_instances:
            if ids: raise HTTPException(422,'Do not combine all_instances and instance_ids')
            return
        if not ids: raise HTTPException(422,'Select at least one instance')
        existing=set(db.scalars(select(Instance.id).where(Instance.id.in_(ids))))
        if len(existing)!=len(set(ids)): raise HTTPException(422,'Unknown instance id')
    @app.get('/api/access/identities')
    def list_identities(request: Request, response: Response, session=Depends(session_dep), db=Depends(get_db)):
        response.headers['Cache-Control']='no-store'
        result=[]
        for p in db.scalars(select(ApiPrincipal).order_by(ApiPrincipal.id)):
            result.append(_public(p,list(db.scalars(select(ApiToken).where(ApiToken.principal_id==p.id).order_by(ApiToken.created_at)))))
        return {'identities':result}
    @app.post('/api/access/reauth')
    def reauth(body: Reauth, request: Request, response: Response, session=Depends(session_dep), db=Depends(get_db)):
        response.headers['Cache-Control']='no-store'
        write_guard(request,session); key=(session.id,request.client.host if request.client else '')
        stamp=datetime.now(timezone.utc)
        limits=((('session',session.id),5),(('ip',key[1]),20))
        with _LOCK:
            for bucket,(started,_) in list(_FAILURES.items()):
                if (stamp-started).total_seconds()>=900: _FAILURES.pop(bucket,None)
            for bucket,limit in limits:
                if _FAILURES.get(bucket,(stamp,0))[1]>=limit:
                    raise HTTPException(429,'Too many password attempts',headers={'Retry-After':'900'})
            if len(_FAILURES)+sum(bucket not in _FAILURES for bucket,_ in limits)>_MAX_STATE:
                raise HTTPException(429,'Reauthentication capacity reached')
            # Reserve before password verification: parallel requests cannot bypass the cap.
            for bucket,_ in limits:
                started,count=_FAILURES.get(bucket,(stamp,0))
                _FAILURES[bucket]=(started,count+1)
            _PROOFS.pop(key,None)
        if not verify_password(session.user.password_hash,body.password):
            raise HTTPException(401,'Invalid password')
        with _LOCK:
            for bucket,_ in limits:
                started,count=_FAILURES.get(bucket,(stamp,1))
                if count>1: _FAILURES[bucket]=(started,count-1)
                else: _FAILURES.pop(bucket,None)
            for proof_key,created in list(_PROOFS.items()):
                if (stamp-created).total_seconds()>300: _PROOFS.pop(proof_key,None)
            if len(_PROOFS)>=_MAX_STATE: raise HTTPException(429,'Reauthentication capacity reached')
            _PROOFS[key]=datetime.now(timezone.utc)
        return {'reauthenticated':True,'expires_in_seconds':300}
    @app.post('/api/access/identities')
    def create(body: CreateIdentity, request: Request, response: Response, session=Depends(session_dep), db=Depends(get_db)):
        response.headers['Cache-Control']='no-store'
        write_guard(request,session); key=proof(request,session)
        validate_scope(db,body.instance_ids,body.all_instances,body.include_future_instances)
        ids=sorted(set(body.instance_ids))
        if body.all_instances and not body.include_future_instances: ids=list(db.scalars(select(Instance.id)))
        p=ApiPrincipal(name=body.name,owner=session.user.email,purpose=body.purpose,role=body.role,instance_ids_json=json.dumps(ids),all_instances=body.all_instances,include_future_instances=body.include_future_instances,enabled=True,created_by=session.user.id)
        db.add(p); db.flush(); raw,tid,exp=issue(db,p,session,body.expires_in_days); audit(db,session,'api_identity_created',p.id); db.commit()
        return {'identity':_public(p,[db.get(ApiToken,tid)]),'token':raw,'token_id':tid,'expires_at':exp.isoformat()}
    @app.patch('/api/access/identities/{identity_id}')
    def edit(identity_id:int, body:EditIdentity, request:Request, response:Response, session=Depends(session_dep), db=Depends(get_db)):
        response.headers['Cache-Control']='no-store'
        write_guard(request,session); p=db.get(ApiPrincipal,identity_id)
        if not p: raise HTTPException(404,'Identity not found')
        data=body.model_dump(exclude_unset=True)
        scope_changed=any(k in data for k in ('instance_ids','all_instances','include_future_instances'))
        key=(session.id,request.client.host if request.client else '')
        sensitive=data.get('enabled') is True or 'role' in data or scope_changed
        if sensitive:
            key=proof(request,session)
        future=data.get('include_future_instances',p.include_future_instances); all_=data.get('all_instances',p.all_instances); ids=data.get('instance_ids',json.loads(p.instance_ids_json or '[]'))
        if scope_changed:
            # Stored snapshot IDs are not an explicit simultaneous all+selected grant.
            explicit_ids=data.get('instance_ids', []) if all_ else ids
            validate_scope(db,explicit_ids,all_,future)
            if all_:
                if future:
                    ids=[]
                elif p.all_instances and not p.include_future_instances:
                    ids=json.loads(p.instance_ids_json or '[]')
                else:
                    ids=list(db.scalars(select(Instance.id)))
            p.instance_ids_json=json.dumps(sorted(set(ids)))
        for k in ('enabled','role','all_instances','include_future_instances'):
            if k in data: setattr(p,k,data[k])
        audit(db,session,'api_identity_updated',p.id); db.commit()

        return {'identity':_public(p,list(db.scalars(select(ApiToken).where(ApiToken.principal_id==p.id))))}
    @app.post('/api/access/tokens/{token_id}/revoke')
    def revoke(token_id:str, request:Request, response:Response, session=Depends(session_dep), db=Depends(get_db)):
        response.headers['Cache-Control']='no-store'
        write_guard(request,session); t=db.get(ApiToken,token_id)
        if not t: raise HTTPException(404,'Token not found')
        if t.revoked_at: return {'revoked':True}
        t.revoked_at=now(); audit(db,session,'api_token_revoked',t.principal_id); db.commit(); return {'revoked':True}
    @app.post('/api/access/tokens/{token_id}/rotate')
    def rotate(token_id:str, body:RotateToken, request:Request, response:Response, session=Depends(session_dep), db=Depends(get_db)):
        response.headers['Cache-Control']='no-store'
        days=body.expires_in_days
        write_guard(request,session); key=proof(request,session); old=db.get(ApiToken,token_id)
        if not old: raise HTTPException(404,'Token not found')
        p=db.get(ApiPrincipal,old.principal_id)
        expiry=old.expires_at if old.expires_at.tzinfo else old.expires_at.replace(tzinfo=timezone.utc)
        if old.revoked_at or expiry <= now() or not p.enabled: raise HTTPException(409,'Token is revoked, expired, or disabled')
        stamp=now()
        changed=db.execute(update(ApiToken).where(ApiToken.id==token_id,ApiToken.revoked_at.is_(None),ApiToken.expires_at>stamp).values(revoked_at=stamp).execution_options(synchronize_session=False)).rowcount
        if changed != 1: db.rollback(); raise HTTPException(409,'Token already revoked')
        raw,tid,exp=issue(db,p,session,days); audit(db,session,'api_token_rotated',p.id); db.commit()
        db.refresh(old)
        return {'identity':_public(p,list(db.scalars(select(ApiToken).where(ApiToken.principal_id==p.id)))),'token':raw,'token_id':tid,'expires_at':exp.isoformat(),'old_token_revoked':True}
    return app
