from __future__ import annotations
import json
from typing import Annotated
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from .db import Base, engine, get_db, SessionLocal
from .models import AuditEvent, DeploymentPlan, Instance, Operation, UpdateRecord, User, now
from .settings import SESSION_COOKIE, CSRF_HEADER
from .services.auth import create_session, ensure_admin, require_permission, validate_session, verify_password
from .services.audit import audit
from .services.credentials import CredentialService
from .services.ha_adapter import HomeAssistantAdapter, persist_instance_health, sync_updates, validate_instance_url

app = FastAPI(title='Home Assistant Fleet Manager', version='0.1.0')
ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
app.mount('/assets', StaticFiles(directory=ROOT / 'fleet_manager' / 'static'), name='assets')

class LoginIn(BaseModel):
    email: str
    password: str
class InstanceIn(BaseModel):
    friendly_name: str = Field(min_length=1, max_length=160)
    url: str
    token: str = Field(min_length=10)
    environment: str = 'Production'
    location: str | None = None
    tags: list[str] = []
class DeploymentPlanIn(BaseModel):
    name: str
    update_ids: list[int]

def startup_init():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        ensure_admin(db)
startup_init()

def current_session(request: Request, db: Session = Depends(get_db)):
    s=validate_session(db, request.cookies.get(SESSION_COOKIE))
    if not s: raise HTTPException(401, 'Authentication required')
    return s

def csrf(request: Request, s=Depends(current_session)):
    if request.method not in {'GET','HEAD','OPTIONS'}:
        if request.headers.get(CSRF_HEADER) != s.csrf_token:
            raise HTTPException(403, 'CSRF validation failed')
    return s

def serialize_instance(i: Instance):
    return {k:getattr(i,k) for k in ['id','friendly_name','url','environment','location','installation_type','ha_core_version','supervisor_version','ha_os_version','connectivity_state','health_state','available_updates','critical_updates','pending_approvals','backup_compliance_state','maintenance_window','update_policy','maintenance_hold'] } | {
        'tags':[t for t in i.tags.split(',') if t],
        'credential_configured': True,
        'credential_created_at': i.credential_created_at.isoformat() if i.credential_created_at else None,
        'credential_updated_at': i.credential_updated_at.isoformat() if i.credential_updated_at else None,
        'last_successful_auth': i.last_successful_auth.isoformat() if i.last_successful_auth else None,
        'last_auth_failure': i.last_auth_failure.isoformat() if i.last_auth_failure else None,
        'last_successful_connection': i.last_successful_connection.isoformat() if i.last_successful_connection else None,
        'last_failed_connection': i.last_failed_connection.isoformat() if i.last_failed_connection else None,
        'last_update_scan': i.last_update_scan.isoformat() if i.last_update_scan else None,
        'last_successful_backup': i.last_successful_backup.isoformat() if i.last_successful_backup else None,
    }

def serialize_update(u: UpdateRecord):
    inst = None
    return {'id':u.id,'instance_id':u.instance_id,'provider':u.provider,'entity_id':u.entity_id,'component':u.component,'category':u.category,'installed_version':u.installed_version,'available_version':u.available_version,'release_url':u.release_url,'release_title':u.release_title,'release_notes':u.release_notes,'breaking_excerpt':u.breaking_excerpt,'severity':u.severity,'risk_level':u.risk_level,'critical_state':u.critical_state,'breaking_state':u.breaking_state,'restart_required':u.restart_required,'manual_action_required':u.manual_action_required,'approval_state':u.approval_state,'installation_state':u.installation_state,'skip_state':u.skip_state,'last_discovered':u.last_discovered.isoformat() if u.last_discovered else None,'policy_decision':u.policy_decision,'policy_explanation':u.policy_explanation}

@app.get('/')
def index(): return FileResponse(ROOT / 'fleet_manager' / 'static' / 'index.html')
@app.get('/health/live')
def live(): return {'ok': True}
@app.get('/health/ready')
def ready(db: Session = Depends(get_db)):
    db.execute(text('select 1'))
    return {'ok': True, 'database': 'ok'}
@app.post('/api/auth/login')
def login(data: LoginIn, response: Response, request: Request, db: Session = Depends(get_db)):
    user=db.query(User).filter_by(email=data.email).one_or_none()
    if not user or not verify_password(user.password_hash, data.password):
        audit(db, action='failed_login', resource_type='auth', result='failed', metadata={'email': data.email}); db.commit()
        raise HTTPException(401, 'Invalid email or password')
    s=create_session(db,user); audit(db, action='login', resource_type='auth', actor_user_id=user.id); db.commit()
    response.set_cookie(SESSION_COOKIE, s.id, httponly=True, samesite='lax', secure=False, max_age=43200)
    return {'ok': True, 'user': {'email': user.email, 'role': user.role}, 'csrf_token': s.csrf_token}
@app.post('/api/auth/logout')
def logout(response: Response, s=Depends(csrf), db: Session = Depends(get_db)):
    s.revoked_at=now(); audit(db, action='logout', resource_type='auth', actor_user_id=s.user_id); db.commit(); response.delete_cookie(SESSION_COOKIE); return {'ok': True}
@app.get('/api/session')
def session(s=Depends(current_session)):
    return {'authenticated': True, 'user': {'email': s.user.email, 'role': s.user.role}, 'csrf_token': s.csrf_token}
@app.get('/api/dashboard')
def dashboard(s=Depends(current_session), db: Session = Depends(get_db)):
    instances=db.query(Instance).all(); updates=db.query(UpdateRecord).filter(UpdateRecord.installation_state=='available').all(); ops=db.query(Operation).order_by(Operation.id.desc()).limit(20).all()
    return {'totals': {'instances': len(instances),'healthy': sum(1 for i in instances if i.health_state=='healthy'),'warning': sum(1 for i in instances if i.health_state=='warning'),'critical': sum(1 for i in instances if i.health_state=='critical'),'offline': sum(1 for i in instances if i.health_state=='offline'),'updates_available': len(updates),'critical_updates': sum(1 for u in updates if u.critical_state),'pending_approvals': sum(1 for u in updates if u.approval_state=='required'),'backups_overdue': 0,'failed_jobs': 0,'running_operations': sum(1 for o in ops if o.status=='running')}, 'needs_attention': [serialize_update(u) for u in updates if u.critical_state or u.approval_state=='required'][:20]}
@app.get('/api/instances')
def instances(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_instances'); return [serialize_instance(i) for i in db.query(Instance).order_by(Instance.friendly_name).all()]
@app.post('/api/instances')
def add_instance(data: InstanceIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'add_instances'); url=validate_instance_url(data.url)
    inst=Instance(friendly_name=data.friendly_name, url=url, environment=data.environment, location=data.location, tags=','.join(data.tags), credential_created_at=now(), credential_updated_at=now())
    # Validate server-side before save; no token returned.
    meta=HomeAssistantAdapter(inst, data.token).test_connection()
    inst.ha_core_version=meta['config'].get('version'); inst.location=inst.location or meta['config'].get('location_name'); inst.connectivity_state='online'; inst.health_state='healthy'; inst.last_successful_connection=now(); inst.last_successful_auth=now()
    db.add(inst); db.flush(); CredentialService().set_instance_token(db, inst.id, data.token)
    audit(db, action='instance_added', resource_type='instance', actor_user_id=s.user_id, resource_id=inst.id, instance_id=inst.id, after={'name': inst.friendly_name, 'url_host': inst.url.split('//')[-1].split('/')[0]})
    db.commit(); return serialize_instance(inst)
@app.post('/api/instances/{instance_id}/replace-credential')
def replace_credential(instance_id:int, body:dict, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'replace_credentials'); inst=db.get(Instance, instance_id)
    if not inst: raise HTTPException(404,'Instance not found')
    token=body.get('token')
    if not token or len(token)<10: raise HTTPException(422,'token required')
    HomeAssistantAdapter(inst, token).test_connection(); CredentialService().set_instance_token(db, inst.id, token); inst.credential_updated_at=now(); inst.last_successful_auth=now()
    audit(db, action='credential_replaced', resource_type='instance', actor_user_id=s.user_id, resource_id=inst.id, instance_id=inst.id)
    db.commit(); return {'ok': True, 'credential_configured': True, 'credential_updated_at': inst.credential_updated_at.isoformat()}
@app.post('/api/instances/{instance_id}/sync')
def sync_instance(instance_id:int, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'view_updates'); inst=db.get(Instance, instance_id)
    if not inst: raise HTTPException(404,'Instance not found')
    token=CredentialService().get_instance_token(db, inst.id)
    try:
        persist_instance_health(db, inst, token); count=sync_updates(db, inst, token); audit(db, action='update_discovered', resource_type='instance', actor_user_id=s.user_id, resource_id=inst.id, instance_id=inst.id, metadata={'pending': count}); db.commit(); return {'ok': True, 'pending': count}
    except Exception as exc:
        audit(db, action='instance_sync_failed', resource_type='instance', actor_user_id=s.user_id, resource_id=inst.id, instance_id=inst.id, result='failed', metadata={'error': type(exc).__name__}); db.commit(); raise HTTPException(502, str(exc))
@app.post('/api/sync-all')
def sync_all(s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'view_updates'); out=[]
    for inst in db.query(Instance).all():
        try:
            token=CredentialService().get_instance_token(db, inst.id); persist_instance_health(db, inst, token); pending=sync_updates(db, inst, token); out.append({'instance_id':inst.id,'ok':True,'pending':pending})
        except Exception as exc: out.append({'instance_id':inst.id,'ok':False,'error':str(exc)[:160]})
    audit(db, action='job_executed', resource_type='sync_all', actor_user_id=s.user_id, metadata={'results':out}); db.commit(); return {'results':out}
@app.get('/api/updates')
def updates(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_updates'); inst={i.id:i for i in db.query(Instance).all()}
    rows=[]
    for u in db.query(UpdateRecord).order_by(UpdateRecord.critical_state.desc(), UpdateRecord.last_discovered.desc()).all():
        d=serialize_update(u); d['instance']=inst.get(u.instance_id).friendly_name if inst.get(u.instance_id) else None; rows.append(d)
    return rows
@app.post('/api/deployments')
def create_deployment(data: DeploymentPlanIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'create_deployment_plans')
    items=db.query(UpdateRecord).filter(UpdateRecord.id.in_(data.update_ids)).all()
    blockers=[]
    for u in items:
        if u.approval_state=='required': blockers.append(f'{u.component} {u.available_version} requires approval for this exact version')
    summary={'updates':len(items),'instances':len({u.instance_id for u in items}),'approval_required':len(blockers),'blockers':blockers,'target_versions':{str(u.id):u.available_version for u in items}}
    plan=DeploymentPlan(name=data.name,status='Awaiting Approval' if blockers else 'Ready',created_by=s.user_id,summary_json=json.dumps(summary)); db.add(plan); db.flush()
    audit(db, action='deployment_plan_created', resource_type='deployment_plan', actor_user_id=s.user_id, resource_id=plan.id, after=summary); db.commit()
    return {'id':plan.id,'name':plan.name,'status':plan.status,'summary':summary}
@app.get('/api/deployments')
def deployment_list(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'create_deployment_plans'); return [{'id':p.id,'name':p.name,'status':p.status,'created_at':p.created_at.isoformat(),'summary':json.loads(p.summary_json)} for p in db.query(DeploymentPlan).order_by(DeploymentPlan.id.desc()).all()]
@app.get('/api/audit')
def audit_log(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_audit_history'); return [{'timestamp':a.timestamp.isoformat(),'actor_user_id':a.actor_user_id,'action':a.action,'resource_type':a.resource_type,'resource_id':a.resource_id,'instance_id':a.instance_id,'result':a.result,'metadata':json.loads(a.metadata_json or '{}')} for a in db.query(AuditEvent).order_by(AuditEvent.id.desc()).limit(200).all()]
