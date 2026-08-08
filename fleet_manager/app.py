from __future__ import annotations
import json
import time
import secrets
from typing import Annotated
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from urllib.parse import urlencode
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy import or_
from sqlalchemy.orm import Session
from .db import Base, engine, get_db, SessionLocal
from .models import Approval, AuditEvent, BackupRecord, DeploymentPlan, Instance, InstanceCredential, JobRun, Notification, Operation, PolicySetting, Schedule, UpdateRecord, User, now
from .settings import SESSION_COOKIE, CSRF_HEADER
from .services.auth import create_session, ensure_admin, ph, require_permission, validate_session, verify_password
from .services.audit import audit
from .services.credentials import CredentialService
from .services.ha_adapter import HomeAssistantAdapter, persist_instance_health, sync_updates, validate_instance_url
from .services.automation import create_instance_backup, install_update, load_auto_policy, monitor_and_act, review_update_for_auto, skip_update, update_matches_stack

app = FastAPI(title='Home Assistant Fleet Manager', version='0.1.0')
ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]
app.mount('/assets', StaticFiles(directory=ROOT / 'fleet_manager' / 'static'), name='assets')
LOGIN_FAILURES: dict[str, list[datetime]] = defaultdict(list)
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = timedelta(minutes=15)

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
class InstanceUpdateIn(BaseModel):
    friendly_name: str | None = Field(default=None, min_length=1, max_length=160)
    url: str | None = None
    token: str | None = None
    environment: str | None = None
    location: str | None = None
    tags: list[str] | None = None
class DeploymentPlanIn(BaseModel):
    name: str
    update_ids: list[int]
class ApprovalIn(BaseModel):
    reason: str | None = None
class AutomationRunIn(BaseModel):
    auto_execute: bool = False
class BulkUpdateIn(BaseModel):
    update_ids: list[int]
    action: str
    reason: str | None = None
class RepairFixIn(BaseModel):
    instance_id: int
    domain: str
    issue_id: str
    action: str | None = None
class UserSettingsIn(BaseModel):
    email: str | None = None
    username: str | None = None
    display_name: str | None = None
    current_password: str | None = None
    new_password: str | None = None
class OidcSettingsIn(BaseModel):
    enabled: bool | None = None
    issuer_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    clear_client_secret: bool | None = None
    scopes: str | None = None
    button_label: str | None = None
class ScheduleIn(BaseModel):
    name: str | None = None
    kind: str | None = None
    cron: str | None = None
    enabled: bool | None = None
class PolicyIn(BaseModel):
    value: dict
    description: str | None = None

def ensure_app_defaults(db: Session):
    if engine.url.get_backend_name() == 'sqlite':
        existing = {row[1] for row in db.execute(text('PRAGMA table_info(users)')).all()}
        if 'username' not in existing:
            db.execute(text('ALTER TABLE users ADD COLUMN username VARCHAR(120)'))
        if 'display_name' not in existing:
            db.execute(text('ALTER TABLE users ADD COLUMN display_name VARCHAR(200)'))
    if db.query(Schedule).count() == 0:
        db.add(Schedule(name='Default monitor cadence', kind='monitor_and_act', cron='0 */6 * * *', enabled=True))
    defaults = {
        'auto_update_policy': {'auto_execute_enabled': True, 'excluded_categories': ['Core','OS','Supervisor','Firmware'], 'excluded_stacks': ['router','zigbee','z-wave','matter','thread'], 'safe_categories': ['Add-on','HACS','Update Entity'], 'requires_public_release_notes': True, 'block_on_breaking_or_action_required': True, 'core_haos_manual_only': True},
        'oidc_settings': {'enabled': False, 'issuer_url': '', 'client_id': '', 'client_secret': '', 'scopes': 'openid email profile', 'button_label': 'Sign in with SSO'},
    }
    for key, value in defaults.items():
        if not db.query(PolicySetting).filter_by(key=key).one_or_none():
            db.add(PolicySetting(key=key, value_json=json.dumps(value), description='Fleet Manager deterministic update safety policy'))
    db.commit()

def startup_init():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        ensure_app_defaults(db)
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
    raw = {}
    attrs = {}
    try:
        raw = json.loads(u.raw_json or '{}')
        attrs = raw.get('attributes') or {}
    except Exception:
        raw = {}; attrs = {}
    progress = attrs.get('progress') or attrs.get('update_percentage') or attrs.get('percent')
    try:
        progress = None if progress is None else max(0, min(100, int(float(progress))))
    except Exception:
        progress = None
    in_progress = bool(attrs.get('in_progress')) or raw.get('state') == 'installing'
    return {'id':u.id,'instance_id':u.instance_id,'provider':u.provider,'entity_id':u.entity_id,'component':u.component,'category':u.category,'installed_version':u.installed_version,'available_version':u.available_version,'release_url':u.release_url,'release_title':u.release_title,'release_notes':u.release_notes,'breaking_excerpt':u.breaking_excerpt,'severity':u.severity,'risk_level':u.risk_level,'critical_state':u.critical_state,'breaking_state':u.breaking_state,'restart_required':u.restart_required,'manual_action_required':u.manual_action_required,'approval_state':u.approval_state,'installation_state':u.installation_state,'skip_state':u.skip_state,'last_discovered':u.last_discovered.isoformat() if u.last_discovered else None,'policy_decision':u.policy_decision,'policy_explanation':u.policy_explanation,'in_progress':in_progress,'progress':progress}

def serialize_user(u: User):
    return {'id': u.id, 'email': u.email, 'username': u.username or '', 'display_name': u.display_name or '', 'role': u.role}

def load_json_setting(db: Session, key: str, default: dict | None = None) -> dict:
    row = db.query(PolicySetting).filter_by(key=key).one_or_none()
    if not row:
        return dict(default or {})
    try:
        value = json.loads(row.value_json or '{}')
        return value if isinstance(value, dict) else dict(default or {})
    except json.JSONDecodeError:
        return dict(default or {})

def save_json_setting(db: Session, key: str, value: dict, description: str):
    row = db.query(PolicySetting).filter_by(key=key).one_or_none()
    if not row:
        row = PolicySetting(key=key)
        db.add(row)
    row.value_json = json.dumps(value)
    row.description = description
    return row

def default_oidc_settings() -> dict:
    return {'enabled': False, 'issuer_url': '', 'client_id': '', 'client_secret': '', 'scopes': 'openid email profile', 'button_label': 'Sign in with SSO'}

def serialize_oidc_settings(value: dict) -> dict:
    return {k: value.get(k, default_oidc_settings()[k]) for k in ['enabled','issuer_url','client_id','scopes','button_label']} | {'client_secret_configured': bool(value.get('client_secret'))}

def oidc_discovery(issuer_url: str) -> dict:
    issuer = issuer_url.rstrip('/')
    with httpx.Client(follow_redirects=True, timeout=15, trust_env=False) as client:
        r = client.get(f'{issuer}/.well-known/openid-configuration')
        r.raise_for_status()
        meta = r.json()
    for key in ['authorization_endpoint', 'token_endpoint', 'userinfo_endpoint']:
        if not meta.get(key):
            raise HTTPException(502, f'OIDC discovery missing {key}')
    return meta

def external_base_url(request: Request) -> str:
    proto = request.headers.get('x-forwarded-proto') or request.url.scheme
    host = request.headers.get('x-forwarded-host') or request.headers.get('host')
    return f'{proto}://{host}'

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
    key=f'{request.client.host if request.client else "unknown"}:{data.email.lower()}'
    cutoff=datetime.now(timezone.utc)-LOGIN_WINDOW
    LOGIN_FAILURES[key]=[t for t in LOGIN_FAILURES[key] if t>cutoff]
    if len(LOGIN_FAILURES[key]) >= LOGIN_MAX_FAILURES:
        audit(db, action='login_rate_limited', resource_type='auth', result='blocked', metadata={'email': data.email}); db.commit()
        raise HTTPException(429, 'Too many failed login attempts; try again later')
    login_id = data.email.strip().lower()
    user=db.query(User).filter(or_(User.email == login_id, User.username == data.email.strip())).one_or_none()
    if not user or not verify_password(user.password_hash, data.password):
        LOGIN_FAILURES[key].append(datetime.now(timezone.utc))
        audit(db, action='failed_login', resource_type='auth', result='failed', metadata={'email': data.email}); db.commit()
        raise HTTPException(401, 'Invalid email or password')
    LOGIN_FAILURES.pop(key, None)
    s=create_session(db,user); audit(db, action='login', resource_type='auth', actor_user_id=user.id); db.commit()
    response.set_cookie(SESSION_COOKIE, s.id, httponly=True, samesite='lax', secure=False, max_age=43200)
    return {'ok': True, 'user': serialize_user(user), 'csrf_token': s.csrf_token}
@app.post('/api/auth/logout')
def logout(response: Response, s=Depends(csrf), db: Session = Depends(get_db)):
    s.revoked_at=now(); audit(db, action='logout', resource_type='auth', actor_user_id=s.user_id); db.commit(); response.delete_cookie(SESSION_COOKIE); return {'ok': True}
@app.get('/api/session')
def session(s=Depends(current_session)):
    return {'authenticated': True, 'user': serialize_user(s.user), 'csrf_token': s.csrf_token}

@app.get('/api/user/settings')
def user_settings(s=Depends(current_session)):
    return serialize_user(s.user)

@app.patch('/api/user/settings')
def update_user_settings(data: UserSettingsIn, s=Depends(csrf), db: Session = Depends(get_db)):
    user = db.get(User, s.user_id)
    if not user:
        raise HTTPException(404, 'User not found')
    before = serialize_user(user)
    password_related = data.new_password is not None or (data.email is not None and data.email != user.email)
    if password_related and not verify_password(user.password_hash, data.current_password or ''):
        raise HTTPException(401, 'Current password is required')
    if data.email is not None:
        email = data.email.strip().lower()
        if not email or '@' not in email:
            raise HTTPException(422, 'Valid email is required')
        if db.query(User).filter(User.email == email, User.id != user.id).one_or_none():
            raise HTTPException(409, 'Email is already in use')
        user.email = email
    if data.username is not None:
        username = data.username.strip()
        if username and db.query(User).filter(User.username == username, User.id != user.id).one_or_none():
            raise HTTPException(409, 'Username is already in use')
        user.username = username or None
    if data.display_name is not None:
        user.display_name = data.display_name.strip() or None
    if data.new_password is not None:
        if len(data.new_password) < 12:
            raise HTTPException(422, 'Password must be at least 12 characters')
        user.password_hash = ph.hash(data.new_password)
    after = serialize_user(user)
    audit(db, action='user_settings_updated', resource_type='user', actor_user_id=user.id, resource_id=user.id, before=before, after=after)
    db.commit(); return after

@app.get('/api/oidc/settings')
def get_oidc_settings(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s, 'manage_application_settings')
    return serialize_oidc_settings(load_json_setting(db, 'oidc_settings', default_oidc_settings()))

@app.patch('/api/oidc/settings')
def update_oidc_settings(data: OidcSettingsIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s, 'manage_application_settings')
    value = load_json_setting(db, 'oidc_settings', default_oidc_settings())
    before = serialize_oidc_settings(value)
    for key in ['enabled', 'issuer_url', 'client_id', 'scopes', 'button_label']:
        incoming = getattr(data, key)
        if incoming is not None:
            value[key] = incoming.strip() if isinstance(incoming, str) else incoming
    if data.clear_client_secret:
        value['client_secret'] = ''
    elif data.client_secret:
        value['client_secret'] = data.client_secret
    if value.get('enabled') and (not value.get('issuer_url') or not value.get('client_id') or not value.get('client_secret')):
        raise HTTPException(422, 'Issuer URL, client ID, and client secret are required before enabling OIDC')
    save_json_setting(db, 'oidc_settings', value, 'OIDC single sign-on settings')
    after = serialize_oidc_settings(value)
    audit(db, action='oidc_settings_updated', resource_type='application_settings', actor_user_id=s.user_id, before=before, after=after)
    db.commit(); return after

@app.get('/api/auth/oidc/config')
def oidc_public_config(db: Session = Depends(get_db)):
    value = load_json_setting(db, 'oidc_settings', default_oidc_settings())
    return {'enabled': bool(value.get('enabled')), 'button_label': value.get('button_label') or 'Sign in with SSO'}

@app.get('/api/auth/oidc/start')
def oidc_start(request: Request, response: Response, db: Session = Depends(get_db)):
    value = load_json_setting(db, 'oidc_settings', default_oidc_settings())
    if not value.get('enabled'):
        raise HTTPException(404, 'OIDC is not enabled')
    meta = oidc_discovery(value.get('issuer_url') or '')
    state = secrets.token_urlsafe(24)
    redirect_uri = external_base_url(request) + '/api/auth/oidc/callback'
    params = {'client_id': value['client_id'], 'response_type': 'code', 'scope': value.get('scopes') or 'openid email profile', 'redirect_uri': redirect_uri, 'state': state}
    redirect = RedirectResponse(meta['authorization_endpoint'] + '?' + urlencode(params), status_code=302)
    redirect.set_cookie('hafm_oidc_state', state, httponly=True, samesite='lax', secure=request.url.scheme == 'https', max_age=600)
    return redirect

@app.get('/api/auth/oidc/callback')
def oidc_callback(request: Request, code: str | None = None, state: str | None = None, db: Session = Depends(get_db)):
    if not code or not state or request.cookies.get('hafm_oidc_state') != state:
        raise HTTPException(400, 'Invalid OIDC callback')
    value = load_json_setting(db, 'oidc_settings', default_oidc_settings())
    if not value.get('enabled'):
        raise HTTPException(404, 'OIDC is not enabled')
    meta = oidc_discovery(value.get('issuer_url') or '')
    redirect_uri = external_base_url(request) + '/api/auth/oidc/callback'
    with httpx.Client(follow_redirects=True, timeout=20, trust_env=False) as client:
        token_resp = client.post(meta['token_endpoint'], data={'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri, 'client_id': value['client_id'], 'client_secret': value['client_secret']}, headers={'Accept': 'application/json'})
        token_resp.raise_for_status()
        token = token_resp.json()
        user_resp = client.get(meta['userinfo_endpoint'], headers={'Authorization': f"Bearer {token.get('access_token')}", 'Accept': 'application/json'})
        user_resp.raise_for_status()
        profile = user_resp.json()
    email = (profile.get('email') or profile.get('preferred_username') or '').strip().lower()
    if not email or '@' not in email:
        raise HTTPException(403, 'OIDC profile did not provide an email address')
    user = db.query(User).filter_by(email=email).one_or_none()
    if not user:
        user = User(email=email, username=profile.get('preferred_username') or None, display_name=profile.get('name') or None, password_hash=ph.hash(secrets.token_urlsafe(32)), role='admin')
        db.add(user); db.flush()
    elif profile.get('name') and not user.display_name:
        user.display_name = profile.get('name')
    sess = create_session(db, user)
    audit(db, action='oidc_login', resource_type='auth', actor_user_id=user.id, metadata={'issuer': value.get('issuer_url')})
    db.commit()
    redirect = RedirectResponse('/', status_code=302)
    redirect.set_cookie(SESSION_COOKIE, sess.id, httponly=True, samesite='lax', secure=request.url.scheme == 'https', max_age=43200)
    redirect.delete_cookie('hafm_oidc_state')
    return redirect
@app.get('/api/dashboard')
def dashboard(s=Depends(current_session), db: Session = Depends(get_db)):
    instances=db.query(Instance).all(); updates=db.query(UpdateRecord).filter(UpdateRecord.installation_state=='available', UpdateRecord.skip_state!='skipped').all(); ops=db.query(Operation).order_by(Operation.id.desc()).limit(20).all()
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

@app.patch('/api/instances/{instance_id}')
def update_instance(instance_id:int, data: InstanceUpdateIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'add_instances')
    inst=db.get(Instance, instance_id)
    if not inst: raise HTTPException(404,'Instance not found')
    before={'name': inst.friendly_name, 'url_host': inst.url.split('//')[-1].split('/')[0], 'environment': inst.environment, 'location': inst.location, 'tags': inst.tags}
    candidate_url = validate_instance_url(data.url) if data.url is not None else inst.url
    candidate_token = data.token.strip() if data.token is not None and data.token.strip() else None
    if candidate_token:
        temp=Instance(friendly_name=data.friendly_name or inst.friendly_name, url=candidate_url, environment=data.environment or inst.environment, location=data.location if data.location is not None else inst.location, tags=','.join(data.tags or [t for t in inst.tags.split(',') if t]))
        meta=HomeAssistantAdapter(temp, candidate_token).test_connection()
        CredentialService().set_instance_token(db, inst.id, candidate_token)
        inst.credential_updated_at=now(); inst.last_successful_auth=now(); inst.ha_core_version=meta['config'].get('version') or inst.ha_core_version
    elif data.url is not None and candidate_url != inst.url:
        token=CredentialService().get_instance_token(db, inst.id)
        temp=Instance(friendly_name=data.friendly_name or inst.friendly_name, url=candidate_url, environment=data.environment or inst.environment, location=data.location if data.location is not None else inst.location, tags=','.join(data.tags or [t for t in inst.tags.split(',') if t]))
        meta=HomeAssistantAdapter(temp, token).test_connection()
        inst.ha_core_version=meta['config'].get('version') or inst.ha_core_version; inst.last_successful_auth=now()
    if data.friendly_name is not None: inst.friendly_name=data.friendly_name
    if data.url is not None: inst.url=candidate_url
    if data.environment is not None: inst.environment=data.environment
    if data.location is not None: inst.location=data.location
    if data.tags is not None: inst.tags=','.join(data.tags)
    inst.last_successful_connection=now(); inst.connectivity_state='online'; inst.health_state='healthy'
    after={'name': inst.friendly_name, 'url_host': inst.url.split('//')[-1].split('/')[0], 'environment': inst.environment, 'location': inst.location, 'tags': inst.tags, 'token_replaced': bool(candidate_token)}
    audit(db, action='instance_updated', resource_type='instance', actor_user_id=s.user_id, resource_id=inst.id, instance_id=inst.id, before=before, after=after)
    db.commit(); return serialize_instance(inst)

@app.delete('/api/instances/{instance_id}')
def delete_instance(instance_id:int, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'add_instances')
    inst=db.get(Instance, instance_id)
    if not inst: raise HTTPException(404,'Instance not found')
    before={'name': inst.friendly_name, 'url_host': inst.url.split('//')[-1].split('/')[0]}
    db.query(Approval).filter_by(instance_id=inst.id).delete(synchronize_session=False)
    db.query(Notification).filter_by(instance_id=inst.id).delete(synchronize_session=False)
    db.query(BackupRecord).filter_by(instance_id=inst.id).delete(synchronize_session=False)
    db.query(Operation).filter_by(instance_id=inst.id).delete(synchronize_session=False)
    db.query(UpdateRecord).filter_by(instance_id=inst.id).delete(synchronize_session=False)
    db.query(InstanceCredential).filter_by(instance_id=inst.id).delete(synchronize_session=False)
    db.delete(inst)
    audit(db, action='instance_deleted', resource_type='instance', actor_user_id=s.user_id, resource_id=instance_id, instance_id=instance_id, before=before)
    db.commit(); return {'ok': True, 'id': instance_id}

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

@app.post('/api/instances/{instance_id}/restart')
def restart_instance(instance_id:int, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'execute_updates')
    inst=db.get(Instance, instance_id)
    if not inst: raise HTTPException(404,'Instance not found')
    token=CredentialService().get_instance_token(db, inst.id)
    try:
        HomeAssistantAdapter(inst, token).post('/api/services/homeassistant/restart', {})
        audit(db, action='instance_restart_requested', resource_type='instance', actor_user_id=s.user_id, resource_id=inst.id, instance_id=inst.id, metadata={'name': inst.friendly_name})
        db.commit(); return {'ok': True, 'message': 'Restart requested'}
    except Exception as exc:
        audit(db, action='instance_restart_failed', resource_type='instance', actor_user_id=s.user_id, resource_id=inst.id, instance_id=inst.id, result='failed', metadata={'error': type(exc).__name__})
        db.commit(); raise HTTPException(502, str(exc))

@app.post('/api/sync-all')
def sync_all(s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'view_updates'); out=[]
    for inst in db.query(Instance).all():
        try:
            token=CredentialService().get_instance_token(db, inst.id); persist_instance_health(db, inst, token); pending=sync_updates(db, inst, token); out.append({'instance_id':inst.id,'ok':True,'pending':pending})
        except Exception as exc: out.append({'instance_id':inst.id,'ok':False,'error':str(exc)[:160]})
    audit(db, action='job_executed', resource_type='sync_all', actor_user_id=s.user_id, metadata={'results':out}); db.commit(); return {'results':out}


def humanize_repair_text(value: str | None) -> str:
    text = str(value or 'Repair issue').replace('_', ' ').replace('-', ' ').strip()
    known = {
        'issue system reboot required': 'System reboot required',
        'deprecated yaml': 'Deprecated YAML configuration',
        'api password deprecated': 'ESPHome API password deprecated',
    }
    return known.get(text.lower(), text[:1].upper() + text[1:])

def repair_action_for_issue(issue: dict) -> str | None:
    key = str(issue.get('translation_key') or '').lower()
    text = f"{issue.get('domain') or ''} {key} {issue.get('issue_id') or ''}".lower()
    if issue.get('domain') == 'hassio' and ('system_reboot_required' in text or 'reboot' in text):
        return 'host_reboot'
    return None

def serialize_repair_issue(issue: dict, inst: Instance) -> dict:
    placeholders = issue.get('translation_placeholders') or {}
    title = placeholders.get('name') or issue.get('translation_key') or issue.get('issue_id') or 'Repair issue'
    title = humanize_repair_text(title)
    detail_bits = []
    if issue.get('translation_key'): detail_bits.append(humanize_repair_text(issue.get('translation_key')))
    for k, v in list(placeholders.items())[:4]:
        if k != 'name': detail_bits.append(f'{k}: {v}')
    action = repair_action_for_issue(issue)
    return {
        'instance_id': inst.id,
        'instance': inst.friendly_name,
        'domain': issue.get('domain') or issue.get('issue_domain') or 'homeassistant',
        'issue_id': issue.get('issue_id'),
        'title': str(title),
        'severity': issue.get('severity') or 'warning',
        'is_fixable': bool(issue.get('is_fixable')),
        'action': action,
        'ignored': bool(issue.get('ignored')),
        'created': issue.get('created'),
        'learn_more_url': issue.get('learn_more_url'),
        'details': ' · '.join(detail_bits) or 'Open Home Assistant Repairs for details',
    }

@app.get('/api/repairs')
def repairs(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_instances')
    rows=[]
    for inst in db.query(Instance).order_by(Instance.friendly_name).all():
        try:
            token=CredentialService().get_instance_token(db, inst.id)
            for issue in HomeAssistantAdapter(inst, token).list_repairs():
                r=serialize_repair_issue(issue, inst)
                if not r['ignored']:
                    rows.append(r)
        except Exception as exc:
            rows.append({'instance_id': inst.id, 'instance': inst.friendly_name, 'domain': 'fleet_manager', 'issue_id': 'repairs_unavailable', 'title': 'Repairs unavailable', 'severity': 'error', 'is_fixable': False, 'ignored': False, 'created': None, 'learn_more_url': None, 'details': str(exc)[:180]})
    return rows


@app.post('/api/repairs/fix')
def fix_repair(data: RepairFixIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'execute_updates')
    inst=db.get(Instance, data.instance_id)
    if not inst: raise HTTPException(404,'Instance not found')
    if not data.domain.strip() or not data.issue_id.strip():
        raise HTTPException(422,'Repair domain and issue ID are required')
    token=CredentialService().get_instance_token(db, inst.id)
    try:
        requested_action = (data.action or '').strip()
        adapter = HomeAssistantAdapter(inst, token)
        if requested_action == 'host_reboot':
            adapter.trigger_shutdown_automations('host_reboot')
            time.sleep(3)
            adapter.post('/api/services/hassio/host_reboot', {})
        else:
            raise HTTPException(422, 'No Fleet Manager action for this repair')
        audit(db, action='repair_fix_requested', resource_type='repair', actor_user_id=s.user_id, resource_id=data.issue_id, instance_id=inst.id, metadata={'domain': data.domain, 'issue_id': data.issue_id, 'action': requested_action})
        db.commit(); return {'ok': True, 'message': 'Repair action started'}
    except HTTPException:
        raise
    except Exception as exc:
        audit(db, action='repair_fix_failed', resource_type='repair', actor_user_id=s.user_id, resource_id=data.issue_id, instance_id=inst.id, result='failed', metadata={'domain': data.domain, 'issue_id': data.issue_id, 'error': type(exc).__name__})
        db.commit(); raise HTTPException(502, str(exc))

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

@app.get('/api/approvals')
def approvals(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'create_deployment_plans')
    rows=[]
    updates={u.id:u for u in db.query(UpdateRecord).all()}
    instances={i.id:i for i in db.query(Instance).all()}
    for a in db.query(Approval).order_by(Approval.id.desc()).all():
        u=updates.get(a.update_record_id); inst=instances.get(a.instance_id)
        rows.append({'id':a.id,'status':a.status,'target_version':a.target_version,'reason':a.reason,'created_at':a.created_at.isoformat(),'decided_at':a.decided_at.isoformat() if a.decided_at else None,'update':serialize_update(u) if u else None,'instance':inst.friendly_name if inst else None})
    return rows

@app.post('/api/updates/{update_id}/approve')
def approve_update(update_id:int, data: ApprovalIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'approve_updates')
    u=db.get(UpdateRecord, update_id)
    if not u: raise HTTPException(404,'Update not found')
    if not u.available_version: raise HTTPException(422,'Update has no target version')
    a=db.query(Approval).filter_by(update_record_id=u.id, target_version=u.available_version).one_or_none()
    if not a:
        a=Approval(update_record_id=u.id, instance_id=u.instance_id, target_version=u.available_version, requested_by=s.user_id)
        db.add(a)
    a.status='approved'; a.approved_by=s.user_id; a.reason=data.reason; a.decided_at=now(); u.approval_state='approved'
    audit(db, action='update_approved', resource_type='update_record', actor_user_id=s.user_id, resource_id=u.id, instance_id=u.instance_id, metadata={'target_version':u.available_version})
    db.commit(); return {'ok':True,'approval_id':a.id,'target_version':a.target_version}

@app.post('/api/updates/{update_id}/reject')
def reject_update(update_id:int, data: ApprovalIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'approve_updates')
    u=db.get(UpdateRecord, update_id)
    if not u: raise HTTPException(404,'Update not found')
    a=db.query(Approval).filter_by(update_record_id=u.id, target_version=u.available_version).one_or_none()
    if not a:
        a=Approval(update_record_id=u.id, instance_id=u.instance_id, target_version=u.available_version or 'unknown', requested_by=s.user_id)
        db.add(a)
    a.status='rejected'; a.approved_by=s.user_id; a.reason=data.reason; a.decided_at=now(); u.approval_state='rejected'
    audit(db, action='update_rejected', resource_type='update_record', actor_user_id=s.user_id, resource_id=u.id, instance_id=u.instance_id, metadata={'target_version':u.available_version})
    db.commit(); return {'ok':True,'approval_id':a.id}

@app.post('/api/updates/{update_id}/review')
def review_update(update_id:int, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'view_updates')
    u=db.get(UpdateRecord, update_id)
    if not u: raise HTTPException(404,'Update not found')
    policy = load_auto_policy(db)
    ok,reasons,_notes=review_update_for_auto(u, policy)
    audit(db, action='release_notes_reviewed', resource_type='update_record', actor_user_id=s.user_id, resource_id=u.id, instance_id=u.instance_id, metadata={'eligible':ok,'reasons':reasons})
    db.commit(); return {'eligible_for_auto':ok,'reasons':reasons,'update':serialize_update(u)}

@app.post('/api/automation/run')
def automation_run(data: AutomationRunIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'execute_updates')
    summary=monitor_and_act(db, auto_execute=data.auto_execute, actor=f'user:{s.user.email}')
    db.commit(); return summary

def update_is_hard_manual(update: UpdateRecord, policy: dict | None = None) -> bool:
    policy = policy or {}
    return (
        update.category in set(policy.get('excluded_categories') or {'Core', 'OS', 'Supervisor', 'Firmware'})
        or update.entity_id in {'update.home_assistant_core_update', 'update.home_assistant_operating_system_update'}
        or bool(update_matches_stack(update, policy.get('excluded_stacks') or []))
    )

@app.post('/api/updates/{update_id}/install')
def install_single_update(update_id:int, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'execute_updates')
    upd=db.get(UpdateRecord, update_id)
    if not upd: raise HTTPException(404,'Update not found')
    inst=db.get(Instance, upd.instance_id)
    if not inst: raise HTTPException(404,'Instance not found')
    if upd.skip_state == 'skipped':
        raise HTTPException(422,'Skipped update — re-enable in HA first')
    if upd.installation_state != 'available':
        raise HTTPException(422,'Update is not pending')
    op=install_update(db, inst, upd, actor=f'user:{s.user.email}')
    db.commit(); return {'ok': op.status in {'succeeded','accepted'}, 'status': op.status, 'state': op.state, 'operation_id': op.id, 'update': serialize_update(upd)}

@app.post('/api/updates/bulk')
def bulk_updates(data: BulkUpdateIn, s=Depends(csrf), db: Session = Depends(get_db)):
    if not data.update_ids:
        raise HTTPException(422, 'Select at least one update')
    action = data.action.lower().strip()
    updates = db.query(UpdateRecord).filter(UpdateRecord.id.in_(data.update_ids)).all()
    instances = {i.id: i for i in db.query(Instance).filter(Instance.id.in_({u.instance_id for u in updates})).all()}
    policy = load_auto_policy(db)
    summary = {'action': action, 'selected': len(data.update_ids), 'found': len(updates), 'updated': 0, 'skipped': 0, 'reviewed': 0, 'blocked': [], 'failed': []}
    if action == 'skip':
        require_permission(s, 'view_updates')
        for upd in updates:
            inst = instances.get(upd.instance_id)
            if not inst:
                summary['failed'].append({'id': upd.id, 'component': upd.component, 'reason': 'instance_not_found'}); continue
            if upd.installation_state != 'available':
                summary['blocked'].append({'id': upd.id, 'instance': inst.friendly_name, 'component': upd.component, 'reason': 'not_pending'}); continue
            op = skip_update(db, inst, upd, actor=f'user:{s.user.email}')
            if op.status == 'succeeded':
                summary['skipped'] += 1
            else:
                summary['failed'].append({'id': upd.id, 'instance': inst.friendly_name, 'component': upd.component, 'reason': op.state})
    elif action == 'review':
        require_permission(s, 'view_updates')
        for upd in updates:
            ok, reasons, _notes = review_update_for_auto(upd, policy)
            summary['reviewed'] += 1
            if not ok:
                inst = instances.get(upd.instance_id)
                summary['blocked'].append({'id': upd.id, 'instance': inst.friendly_name if inst else None, 'component': upd.component, 'reason': ','.join(reasons[:5])})
            audit(db, action='update_reviewed', resource_type='update_record', actor_user_id=s.user_id, resource_id=upd.id, instance_id=upd.instance_id, metadata={'eligible': ok, 'reasons': reasons})
    elif action == 'update':
        require_permission(s, 'execute_updates')
        for upd in updates:
            inst = instances.get(upd.instance_id)
            if not inst:
                summary['failed'].append({'id': upd.id, 'component': upd.component, 'reason': 'instance_not_found'}); continue
            if upd.installation_state != 'available' or upd.skip_state == 'skipped':
                summary['blocked'].append({'id': upd.id, 'instance': inst.friendly_name, 'component': upd.component, 'reason': 'not_pending'}); continue
            op = install_update(db, inst, upd, actor=f'user:{s.user.email}')
            if op.status in {'succeeded','accepted'}:
                summary['updated'] += 1
            else:
                summary['blocked'].append({'id': upd.id, 'instance': inst.friendly_name, 'component': upd.component, 'reason': op.state})
    else:
        raise HTTPException(422, 'Action must be update, skip, or review')
    db.commit(); return summary

@app.get('/api/notifications')
def notifications(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_audit_history')
    return [{'id':n.id,'severity':n.severity,'title':n.title,'body':n.body,'status':n.status,'instance_id':n.instance_id,'update_record_id':n.update_record_id,'created_at':n.created_at.isoformat(),'acknowledged_at':n.acknowledged_at.isoformat() if n.acknowledged_at else None} for n in db.query(Notification).order_by(Notification.id.desc()).limit(200).all()]

@app.get('/api/operations')
def operations(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_audit_history')
    return [{'id':o.id,'kind':o.kind,'instance_id':o.instance_id,'deployment_plan_id':o.deployment_plan_id,'state':o.state,'status':o.status,'started_at':o.started_at.isoformat() if o.started_at else None,'ended_at':o.ended_at.isoformat() if o.ended_at else None,'details':json.loads(o.details_json or '{}')} for o in db.query(Operation).order_by(Operation.id.desc()).limit(200).all()]

@app.get('/api/jobs')
def jobs(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_audit_history')
    return [{'id':j.id,'kind':j.kind,'status':j.status,'started_at':j.started_at.isoformat(),'ended_at':j.ended_at.isoformat() if j.ended_at else None,'details':json.loads(j.details_json or '{}')} for j in db.query(JobRun).order_by(JobRun.id.desc()).limit(100).all()]

@app.get('/api/backups')
def backups(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_audit_history')
    return [{'id':b.id,'instance_id':b.instance_id,'provider':b.provider,'status':b.status,'backup_id':b.backup_id,'name':b.name,'started_at':b.started_at.isoformat() if b.started_at else None,'completed_at':b.completed_at.isoformat() if b.completed_at else None,'details':json.loads(b.details_json or '{}')} for b in db.query(BackupRecord).order_by(BackupRecord.id.desc()).limit(100).all()]

@app.get('/api/schedules')
def schedules(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_audit_history')
    return [{'id':x.id,'name':x.name,'kind':x.kind,'cron':x.cron,'enabled':x.enabled,'last_run_at':x.last_run_at.isoformat() if x.last_run_at else None,'created_at':x.created_at.isoformat()} for x in db.query(Schedule).order_by(Schedule.id.desc()).all()]


@app.post('/api/notifications/{notification_id}/ack')
def ack_notification(notification_id:int, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'manage_notifications')
    n=db.get(Notification, notification_id)
    if not n: raise HTTPException(404,'Notification not found')
    n.status='acknowledged'; n.acknowledged_at=now()
    audit(db, action='notification_acknowledged', resource_type='notification', actor_user_id=s.user_id, resource_id=n.id)
    db.commit(); return {'ok':True,'id':n.id,'status':n.status}

@app.post('/api/notifications/ack')
def ack_notifications_bulk(body:dict, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'manage_notifications')
    ids = body.get('ids') or []
    q = db.query(Notification).filter(Notification.status == 'open')
    if ids:
        q = q.filter(Notification.id.in_([int(x) for x in ids]))
    rows = q.all()
    ts = now()
    for n in rows:
        n.status='acknowledged'; n.acknowledged_at=ts
    audit(db, action='notifications_acknowledged_bulk', resource_type='notification', actor_user_id=s.user_id, metadata={'count': len(rows), 'ids': [n.id for n in rows[:50]], 'all_open': not bool(ids)})
    db.commit(); return {'ok': True, 'count': len(rows)}

@app.post('/api/instances/{instance_id}/backup')
def backup_instance(instance_id:int, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'execute_backups')
    inst=db.get(Instance, instance_id)
    if not inst: raise HTTPException(404,'Instance not found')
    rec=create_instance_backup(db, inst, actor=f'user:{s.user.email}')
    db.commit(); return {'id':rec.id,'status':rec.status,'backup_id':rec.backup_id,'details':json.loads(rec.details_json or '{}')}

@app.patch('/api/schedules/{schedule_id}')
def update_schedule(schedule_id:int, data: ScheduleIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'create_schedules')
    sched=db.get(Schedule, schedule_id)
    if not sched: raise HTTPException(404,'Schedule not found')
    before={'name':sched.name,'kind':sched.kind,'cron':sched.cron,'enabled':sched.enabled}
    if data.name is not None: sched.name=data.name
    if data.kind is not None: sched.kind=data.kind
    if data.cron is not None: sched.cron=data.cron
    if data.enabled is not None: sched.enabled=data.enabled
    after={'name':sched.name,'kind':sched.kind,'cron':sched.cron,'enabled':sched.enabled}
    audit(db, action='schedule_updated', resource_type='schedule', actor_user_id=s.user_id, resource_id=sched.id, before=before, after=after)
    db.commit(); return {'id':sched.id,**after}

@app.get('/api/policies')
def policies(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'modify_update_policies')
    return [{'id':p.id,'key':p.key,'value':json.loads(p.value_json or '{}'),'description':p.description,'updated_at':p.updated_at.isoformat()} for p in db.query(PolicySetting).order_by(PolicySetting.key).all()]

@app.put('/api/policies/{key}')
def put_policy(key:str, data: PolicyIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'modify_update_policies')
    p=db.query(PolicySetting).filter_by(key=key).one_or_none()
    if not p:
        p=PolicySetting(key=key); db.add(p)
    before=json.loads(p.value_json or '{}')
    p.value_json=json.dumps(data.value, sort_keys=True)
    if data.description is not None: p.description=data.description
    audit(db, action='policy_updated', resource_type='policy_setting', actor_user_id=s.user_id, resource_id=key, before=before, after=data.value)
    db.commit(); return {'key':p.key,'value':json.loads(p.value_json),'description':p.description,'updated_at':p.updated_at.isoformat()}
