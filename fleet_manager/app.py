from __future__ import annotations
import asyncio
import json
import re
import time
import secrets
import smtplib
from email.message import EmailMessage
from typing import Annotated
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from urllib.parse import urlencode
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy import or_
from sqlalchemy.orm import Session
from .db import Base, engine, get_db, SessionLocal
from .models import Approval, AuditEvent, BackupRecord, DeploymentPlan, Instance, InstanceCredential, JobRun, Notification, Operation, PolicySetting, Schedule, UpdateRecord, User, Session as AppSession, now
from .settings import SESSION_COOKIE, CSRF_HEADER
from .services.auth import create_session, ensure_admin, ph, require_permission, validate_session, verify_password
from .services.audit import audit
from .services.credentials import CredentialService
from .services.ha_adapter import HomeAssistantAdapter, persist_instance_health, sync_updates, validate_instance_url
from .services.automation import create_instance_backup, install_update, load_auto_policy, monitor_and_act, review_update_for_auto, skip_update, update_matches_stack
from .services.reliability import create_operation, migrate_operation_columns, public_error_message, reconcile_operation, reconcile_startup, safe_append_diagnostic, safe_exception_message, _safe

app = FastAPI(title='Home Assistant Fleet Manager', version='0.1.0')
app.add_middleware(GZipMiddleware, minimum_size=1024)
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
class NotificationSettingsIn(BaseModel):
    ntfy_enabled: bool | None = None
    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None
    ntfy_clear_token: bool | None = None
    pushover_enabled: bool | None = None
    pushover_user_key: str | None = None
    pushover_app_token: str | None = None
    pushover_clear_secrets: bool | None = None
    email_enabled: bool | None = None
    email_to: str | None = None
    email_from: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_clear_password: bool | None = None
    telegram_enabled: bool | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_clear_token: bool | None = None

class TestNotificationIn(NotificationSettingsIn):
    channel: str

def ensure_app_defaults(db: Session):
    migrate_operation_columns(engine)
    if engine.url.get_backend_name() == 'sqlite':
        existing = {row[1] for row in db.execute(text('PRAGMA table_info(users)')).all()}
        if 'username' not in existing:
            db.execute(text('ALTER TABLE users ADD COLUMN username VARCHAR(120)'))
        if 'display_name' not in existing:
            db.execute(text('ALTER TABLE users ADD COLUMN display_name VARCHAR(200)'))
    if db.query(Schedule).count() == 0:
        db.add(Schedule(name='Fleet update check', kind='monitor_and_act', cron='every 6h', enabled=True))
    else:
        for sched in db.query(Schedule).filter(Schedule.name.in_(['Default monitor cadence','Fleet update check'])).all():
            sched.name = 'Fleet update check'
            if sched.cron == '0 */6 * * *':
                sched.cron = 'every 6h'
    defaults = {
        'auto_update_policy': {'auto_execute_enabled': True, 'excluded_categories': ['Core','OS','Supervisor','Firmware'], 'excluded_stacks': ['router','zigbee','z-wave','matter','thread'], 'safe_categories': ['Add-on','HACS','Update Entity'], 'requires_public_release_notes': True, 'block_on_breaking_or_action_required': True, 'core_haos_manual_only': True},
        'oidc_settings': {'enabled': False, 'issuer_url': '', 'client_id': '', 'client_secret': '', 'scopes': 'openid email profile', 'button_label': 'Sign in with SSO'},
        'notification_settings': {'ntfy_enabled': False, 'ntfy_url': '', 'ntfy_topic': '', 'ntfy_token': '', 'pushover_enabled': False, 'pushover_user_key': '', 'pushover_app_token': '', 'email_enabled': False, 'email_to': '', 'email_from': '', 'smtp_host': '', 'smtp_port': 587, 'smtp_username': '', 'smtp_password': '', 'telegram_enabled': False, 'telegram_bot_token': '', 'telegram_chat_id': ''},
    }
    for key, value in defaults.items():
        if not db.query(PolicySetting).filter_by(key=key).one_or_none():
            db.add(PolicySetting(key=key, value_json=json.dumps(value), description='Fleet Manager deterministic update safety policy'))
    db.commit()

def parse_schedule_seconds(value: str | None) -> int:
    text_value = (value or '').strip().lower()
    m = re.fullmatch(r'every\s+(\d+)\s*(m|min|minute|minutes|h|hr|hour|hours|d|day|days)', text_value)
    if m:
        n = int(m.group(1)); unit = m.group(2)
        if unit.startswith('m'): return max(300, n * 60)
        if unit.startswith('h') or unit == 'hr': return max(300, n * 3600)
        if unit.startswith('d'): return max(300, n * 86400)
    if text_value == 'hourly': return 3600
    if text_value == 'daily': return 86400
    # Support the default cron-like pattern: 0 */6 * * *
    m = re.fullmatch(r'0\s+\*/(\d+)\s+\*\s+\*\s+\*', text_value)
    if m: return max(300, int(m.group(1)) * 3600)
    return 6 * 3600

def run_update_discovery(db: Session, *, actor: str = 'scheduler') -> dict:
    job = JobRun(kind='update_discovery', status='running', started_at=now(), details_json='{}')
    db.add(job); db.flush()
    summary = {'instances': 0, 'sync_ok': 0, 'sync_failed': 0, 'results': []}
    try:
        for inst in db.query(Instance).order_by(Instance.friendly_name).all():
            summary['instances'] += 1
            try:
                token = CredentialService().get_instance_token(db, inst.id)
                persist_instance_health(db, inst, token)
                pending = sync_updates(db, inst, token)
                summary['sync_ok'] += 1
                summary['results'].append({'instance_id': inst.id, 'ok': True, 'pending': pending})
            except Exception as exc:
                summary['sync_failed'] += 1
                summary['results'].append({'instance_id': inst.id, 'ok': False, 'error': safe_exception_message(exc)})
        job.status='partial_failed' if summary['sync_failed'] else 'succeeded'; job.ended_at=now(); job.details_json=json.dumps(summary)
        audit(db, action='scheduled_update_check_completed', resource_type='job_run', resource_id=job.id, result=job.status, metadata=summary)
        return summary
    except Exception as exc:
        job.status='failed'; job.ended_at=now(); job.details_json=json.dumps({'error': type(exc).__name__, **summary})
        audit(db, action='scheduled_update_check_failed', resource_type='job_run', resource_id=job.id, result='failed', metadata={'error': type(exc).__name__})
        raise

async def schedule_loop():
    await asyncio.sleep(10)
    while True:
        try:
            # Reconcile durable operations continuously, not only after a
            # process restart. This is read-only against Home Assistant.
            with SessionLocal() as reconcile_db:
                def reconcile_adapter_factory(instance_id):
                    instance = reconcile_db.get(Instance, instance_id)
                    if not instance:
                        raise LookupError('instance not found')
                    token = CredentialService().get_instance_token(reconcile_db, instance_id)
                    return HomeAssistantAdapter(instance, token)
                reconcile_startup(reconcile_db, adapter_factory=reconcile_adapter_factory)
            with SessionLocal() as db:
                sched = db.query(Schedule).filter(Schedule.enabled == True, Schedule.kind.in_(['update_discovery','monitor_and_act'])).order_by(Schedule.id).first()
                if sched:
                    interval = parse_schedule_seconds(sched.cron)
                    last_run_at = sched.last_run_at
                    if last_run_at and last_run_at.tzinfo is None:
                        last_run_at = last_run_at.replace(tzinfo=timezone.utc)
                    due = not last_run_at or (now() - last_run_at).total_seconds() >= interval
                    if due:
                        if sched.kind == 'monitor_and_act':
                            summary = monitor_and_act(db, auto_execute=False, actor='scheduler')
                        else:
                            summary = run_update_discovery(db, actor='scheduler')
                        sched.last_run_at = now()
                        db.commit()
        except Exception as exc:
            # Never silently lose scheduler failures: persist a diagnostic run.
            try:
                with SessionLocal() as error_db:
                    failed = JobRun(kind='scheduler_loop', status='failed', started_at=now(), ended_at=now(),
                                    details_json=json.dumps({'error': type(exc).__name__, 'message': safe_exception_message(exc)}))
                    error_db.add(failed); error_db.flush()
                    audit(error_db, action='scheduler_loop_failed', resource_type='job_run', resource_id=failed.id,
                          result='failed', metadata={'error': type(exc).__name__})
                    error_db.commit()
            except Exception:
                pass
        await asyncio.sleep(60)

def startup_init():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        ensure_app_defaults(db)
        ensure_admin(db)
        def adapter_factory(instance_id):
            instance = db.get(Instance, instance_id)
            if not instance:
                raise LookupError('instance not found')
            token = CredentialService().get_instance_token(db, instance_id)
            return HomeAssistantAdapter(instance, token)
        reconcile_startup(db, adapter_factory=adapter_factory)
startup_init()

@app.on_event('startup')
async def start_scheduler_task():
    asyncio.create_task(schedule_loop())

def current_session(request: Request, db: Session = Depends(get_db)):
    s=validate_session(db, request.cookies.get(SESSION_COOKIE))
    if not s: raise HTTPException(401, 'Authentication required')
    return s

def csrf(request: Request, s=Depends(current_session)):
    if request.method not in {'GET','HEAD','OPTIONS'}:
        if request.headers.get(CSRF_HEADER) != s.csrf_token:
            raise HTTPException(403, 'CSRF validation failed')
    return s


@app.get('/manifest.webmanifest')
def pwa_manifest():
    return FileResponse(ROOT / 'fleet_manager' / 'static' / 'manifest.webmanifest', media_type='application/manifest+json')

@app.get('/sw.js')
def service_worker():
    return FileResponse(ROOT / 'fleet_manager' / 'static' / 'sw.js', media_type='application/javascript', headers={'Cache-Control': 'no-cache', 'Service-Worker-Allowed': '/'})

@app.middleware('http')
async def audit_mutating_api_requests(request: Request, call_next):
    should_audit = request.method in {'POST', 'PATCH', 'PUT', 'DELETE'} and request.url.path.startswith('/api/')
    actor_user_id = None
    if should_audit:
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            try:
                with SessionLocal() as audit_db:
                    sess = audit_db.get(AppSession, session_id)
                    if sess and not sess.revoked_at and sess.expires_at > now():
                        actor_user_id = sess.user_id
            except Exception:
                actor_user_id = None
    response = await call_next(request)
    if should_audit:
        try:
            with SessionLocal() as audit_db:
                audit_db.add(AuditEvent(
                    actor_user_id=actor_user_id,
                    action='ui_api_action',
                    resource_type='api_request',
                    resource_id=request.url.path[:120],
                    result='success' if response.status_code < 400 else 'failed',
                    metadata_json=json.dumps({
                        'method': request.method,
                        'path': request.url.path,
                        'status_code': response.status_code,
                        'page': request.headers.get('x-fleet-page') or 'unknown',
                        'menu': request.headers.get('x-fleet-menu') or 'unknown',
                    }),
                ))
                audit_db.commit()
        except Exception:
            pass
    return response

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
    progress = next((attrs.get(k) for k in ('progress','update_percentage','percent') if attrs.get(k) is not None), None)
    try:
        progress = None if progress is None else max(0, min(100, int(float(progress))))
    except Exception:
        progress = None
    in_progress = bool(attrs.get('in_progress')) or raw.get('state') in {'installing', 'updating'}
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

def default_notification_settings() -> dict:
    return {'ntfy_enabled': False, 'ntfy_url': '', 'ntfy_topic': '', 'ntfy_token': '', 'pushover_enabled': False, 'pushover_user_key': '', 'pushover_app_token': '', 'email_enabled': False, 'email_to': '', 'email_from': '', 'smtp_host': '', 'smtp_port': 587, 'smtp_username': '', 'smtp_password': '', 'telegram_enabled': False, 'telegram_bot_token': '', 'telegram_chat_id': ''}

def serialize_notification_settings(value: dict) -> dict:
    d = default_notification_settings()
    merged = {k: value.get(k, d[k]) for k in d}
    for secret in ['ntfy_token','pushover_user_key','pushover_app_token','smtp_password','telegram_bot_token']:
        merged.pop(secret, None)
        merged[f'{secret}_configured'] = bool(value.get(secret))
    return merged


def merged_notification_settings(saved: dict, incoming: NotificationSettingsIn | None = None) -> dict:
    value = dict(default_notification_settings())
    value.update(saved or {})
    if incoming:
        for key in default_notification_settings():
            if hasattr(incoming, key):
                val = getattr(incoming, key)
                if val is not None and val != '':
                    value[key] = val.strip() if isinstance(val, str) else val
    return value

def send_test_notification(channel: str, cfg: dict) -> dict:
    channel = (channel or '').strip().lower()
    title = 'Home Assistant Fleet Manager test notification'
    body = 'This is a test notification from Home Assistant Fleet Manager.'
    if channel == 'ntfy':
        url = (cfg.get('ntfy_url') or 'https://ntfy.sh').rstrip('/')
        topic = (cfg.get('ntfy_topic') or '').strip('/')
        if not topic: raise HTTPException(422, 'ntfy topic is required')
        headers = {'Title': title}
        if cfg.get('ntfy_token'): headers['Authorization'] = f"Bearer {cfg['ntfy_token']}"
        with httpx.Client(timeout=15, trust_env=False) as client:
            r = client.post(f'{url}/{topic}', content=body.encode(), headers=headers)
            r.raise_for_status()
        return {'ok': True, 'channel': channel}
    if channel == 'pushover':
        if not cfg.get('pushover_user_key') or not cfg.get('pushover_app_token'):
            raise HTTPException(422, 'Pushover user key and application token are required')
        with httpx.Client(timeout=15, trust_env=False) as client:
            r = client.post('https://api.pushover.net/1/messages.json', data={'token': cfg['pushover_app_token'], 'user': cfg['pushover_user_key'], 'title': title, 'message': body})
            r.raise_for_status()
        return {'ok': True, 'channel': channel}
    if channel == 'telegram':
        if not cfg.get('telegram_bot_token') or not cfg.get('telegram_chat_id'):
            raise HTTPException(422, 'Telegram bot token and chat ID are required')
        url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
        with httpx.Client(timeout=15, trust_env=False) as client:
            r = client.post(url, json={'chat_id': cfg['telegram_chat_id'], 'text': f'{title}\n\n{body}'})
            r.raise_for_status()
        return {'ok': True, 'channel': channel}
    if channel == 'email':
        for key in ['email_to','email_from','smtp_host']:
            if not cfg.get(key): raise HTTPException(422, f'{key} is required')
        msg = EmailMessage(); msg['Subject'] = title; msg['From'] = cfg['email_from']; msg['To'] = cfg['email_to']; msg.set_content(body)
        port = int(cfg.get('smtp_port') or 587)
        with smtplib.SMTP(cfg['smtp_host'], port, timeout=15) as smtp:
            smtp.starttls()
            if cfg.get('smtp_username') or cfg.get('smtp_password'):
                smtp.login(cfg.get('smtp_username') or '', cfg.get('smtp_password') or '')
            smtp.send_message(msg)
        return {'ok': True, 'channel': channel}
    raise HTTPException(422, 'Channel must be ntfy, pushover, email, or telegram')

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

def oidc_error_page(message: str, *, status_code: int = 502) -> HTMLResponse:
    safe = re.sub(r'[<>&]', lambda m: {'<':'&lt;','>':'&gt;','&':'&amp;'}[m.group(0)], message)[:1000]
    return HTMLResponse(
        f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign-in failed</title><style>body{{font:15px system-ui;margin:0;background:#0a0a0a;color:#f2f2f2;display:grid;place-items:center;min-height:100vh}}.card{{max-width:680px;margin:24px;padding:24px;border:1px solid #333;border-radius:14px;background:#141414}}a{{color:#58a6ff}}</style></head><body><main class="card"><h1>Sign-in failed</h1><p>{safe}</p><p><a href="/">Return to Fleet Manager</a></p></main></body></html>''',
        status_code=status_code,
    )

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

@app.get('/api/notification-settings')
def get_notification_settings(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s, 'modify_update_policies')
    return serialize_notification_settings(load_json_setting(db, 'notification_settings', default_notification_settings()))

@app.patch('/api/notification-settings')
def update_notification_settings(data: NotificationSettingsIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s, 'modify_update_policies')
    value = load_json_setting(db, 'notification_settings', default_notification_settings())
    before = serialize_notification_settings(value)
    text_fields = ['ntfy_url','ntfy_topic','email_to','email_from','smtp_host','smtp_username','telegram_chat_id']
    bool_fields = ['ntfy_enabled','pushover_enabled','email_enabled','telegram_enabled']
    for key in bool_fields:
        incoming = getattr(data, key)
        if incoming is not None:
            value[key] = bool(incoming)
    for key in text_fields:
        incoming = getattr(data, key)
        if incoming is not None:
            value[key] = incoming.strip()
    if data.smtp_port is not None:
        if data.smtp_port < 1 or data.smtp_port > 65535:
            raise HTTPException(422, 'SMTP port must be between 1 and 65535')
        value['smtp_port'] = data.smtp_port
    secret_pairs = [('ntfy_token', data.ntfy_token, data.ntfy_clear_token), ('pushover_user_key', data.pushover_user_key, data.pushover_clear_secrets), ('pushover_app_token', data.pushover_app_token, data.pushover_clear_secrets), ('smtp_password', data.smtp_password, data.smtp_clear_password), ('telegram_bot_token', data.telegram_bot_token, data.telegram_clear_token)]
    for key, incoming, clear in secret_pairs:
        if clear:
            value[key] = ''
        elif incoming:
            value[key] = incoming
    save_json_setting(db, 'notification_settings', value, 'Outbound notification channel settings')
    after = serialize_notification_settings(value)
    audit(db, action='notification_settings_updated', resource_type='application_settings', actor_user_id=s.user_id, before=before, after=after)
    db.commit(); return after


@app.post('/api/notification-settings/test')
def test_notification_settings(data: TestNotificationIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s, 'modify_update_policies')
    saved = load_json_setting(db, 'notification_settings', default_notification_settings())
    cfg = merged_notification_settings(saved, data)
    try:
        result = send_test_notification(data.channel, cfg)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f'Test notification failed: HTTP {exc.response.status_code}')
    except httpx.HTTPError as exc:
        raise HTTPException(502, f'Test notification failed: {type(exc).__name__}')
    except smtplib.SMTPException as exc:
        raise HTTPException(502, f'Test email failed: {type(exc).__name__}')
    audit(db, action='test_notification_sent', resource_type='application_settings', actor_user_id=s.user_id, metadata={'channel': data.channel})
    db.commit(); return result

@app.get('/api/auth/oidc/config')
def oidc_public_config(db: Session = Depends(get_db)):
    value = load_json_setting(db, 'oidc_settings', default_oidc_settings())
    return {'enabled': bool(value.get('enabled')), 'button_label': value.get('button_label') or 'Sign in with SSO'}

@app.get('/api/auth/oidc/start')
def oidc_start(request: Request, response: Response, db: Session = Depends(get_db)):
    value = load_json_setting(db, 'oidc_settings', default_oidc_settings())
    if not value.get('enabled'):
        raise HTTPException(404, 'OIDC is not enabled')
    try:
        meta = oidc_discovery(value.get('issuer_url') or '')
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        audit(db, action='oidc_login_failed', resource_type='auth', result='failed', metadata={'issuer': value.get('issuer_url'), 'error': type(exc).__name__})
        db.commit()
        return oidc_error_page('SSO provider is currently unavailable. Use local sign-in or ask an administrator to verify the OIDC configuration.', status_code=503)
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
    try:
        meta = oidc_discovery(value.get('issuer_url') or '')
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        audit(db, action='oidc_login_failed', resource_type='auth', result='failed', metadata={'issuer': value.get('issuer_url'), 'error': type(exc).__name__})
        db.commit()
        return oidc_error_page('SSO provider is currently unavailable. Return to Fleet Manager and use local sign-in.', status_code=503)
    redirect_uri = external_base_url(request) + '/api/auth/oidc/callback'
    with httpx.Client(follow_redirects=True, timeout=20, trust_env=False) as client:
        try:
            token_resp = client.post(meta['token_endpoint'], data={'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri, 'client_id': value['client_id'], 'client_secret': value['client_secret']}, headers={'Accept': 'application/json'})
            token_resp.raise_for_status()
            token = token_resp.json()
            user_resp = client.get(meta['userinfo_endpoint'], headers={'Authorization': f"Bearer {token.get('access_token')}", 'Accept': 'application/json'})
            user_resp.raise_for_status()
            profile = user_resp.json()
        except httpx.HTTPStatusError as exc:
            audit(db, action='oidc_login_failed', resource_type='auth', result='failed', metadata={'issuer': value.get('issuer_url'), 'status_code': exc.response.status_code, 'redirect_uri': redirect_uri, 'provider_error': f'Provider returned HTTP {exc.response.status_code}'})
            db.commit()
            hint = 'OIDC token exchange failed. Check Authentik redirect URI/client settings.' if str(exc.request.url) == meta.get('token_endpoint') else 'OIDC profile lookup failed.'
            return oidc_error_page(f'{hint} Provider returned HTTP {exc.response.status_code}.')
        except (httpx.HTTPError, ValueError) as exc:
            audit(db, action='oidc_login_failed', resource_type='auth', result='failed', metadata={'issuer': value.get('issuer_url'), 'error': type(exc).__name__, 'redirect_uri': redirect_uri})
            db.commit()
            return oidc_error_page(f'OIDC sign-in failed before Fleet Manager could create a session: {type(exc).__name__}')
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
        audit(db, action='instance_sync_failed', resource_type='instance', actor_user_id=s.user_id, resource_id=inst.id, instance_id=inst.id, result='failed', metadata={'error': type(exc).__name__}); db.commit(); raise HTTPException(502, safe_exception_message(exc))

@app.post('/api/instances/{instance_id}/restart')
def restart_instance(instance_id:int, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'execute_updates')
    inst=db.get(Instance, instance_id)
    if not inst: raise HTTPException(404,'Instance not found')
    request_id = secrets.token_urlsafe(12)
    try:
        op = create_operation(db, kind='restart', instance=inst, request_id=request_id,
                              idempotency_key=f'restart:{inst.id}', recovery_action='retry_restart',
                              details={'instance_id': inst.id, 'actor': s.user.email})
    except ValueError:
        raise HTTPException(409, 'Another incompatible operation is already active for this instance')
    if op.status == 'succeeded':
        return {'ok': True, 'message': 'Restart already verified', 'state': op.state, 'operation_id': op.id,
                'status': op.status, 'recovery': op.recovery_action}
    token=CredentialService().get_instance_token(db, inst.id)
    try:
        HomeAssistantAdapter(inst, token).post('/api/services/homeassistant/restart', {})
        op.status='restarting'; op.state='restarting'; op.started_at=now(); op.attempt_count=(op.attempt_count or 0)+1
        inst.connectivity_state='restarting'; inst.health_state='restarting'; inst.last_failed_connection=now()
        audit(db, action='instance_restart_requested', resource_type='instance', actor_user_id=s.user_id, resource_id=inst.id, instance_id=inst.id, request_id=op.request_id, metadata={'name': inst.friendly_name, 'state': 'restarting', 'operation_id': op.id})
        db.commit(); return {'ok': True, 'message': 'Restart requested', 'state': 'restarting', 'status': op.status, 'operation_id': op.id, 'recovery': op.recovery_action}
    except Exception as exc:
        op.status='outcome_unknown'; op.state='outcome_unknown'; op.error_code=type(exc).__name__; op.error_message=safe_exception_message(exc); op.ended_at=now()
        audit(db, action='instance_restart_failed', resource_type='instance', actor_user_id=s.user_id, resource_id=inst.id, instance_id=inst.id, request_id=op.request_id, result='failed', metadata={'error': type(exc).__name__, 'operation_id': op.id})
        db.commit(); raise HTTPException(502, f'Restart request failed ({type(exc).__name__})')

@app.post('/api/sync-all')
def sync_all(s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'view_updates')
    summary = run_update_discovery(db, actor=f'user:{s.user.email}')
    db.commit(); return summary


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
    placeholders = issue.get('translation_placeholders') or {}
    text = ' '.join(str(x or '') for x in [issue.get('domain'), key, issue.get('issue_id'), placeholders.get('name')]).lower().replace('_', ' ').replace('-', ' ')
    if issue.get('domain') == 'hassio' and ('system_reboot_required' in text or 'reboot required' in text):
        return 'host_reboot'
    if 'restart required' in text or 'reboot required' in text:
        return 'ha_restart'
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
            rows.append({'instance_id': inst.id, 'instance': inst.friendly_name, 'domain': 'fleet_manager', 'issue_id': 'repairs_unavailable', 'title': 'Repairs unavailable', 'severity': 'error', 'is_fixable': False, 'ignored': False, 'created': None, 'learn_more_url': None, 'details': safe_exception_message(exc)})
    return rows


@app.post('/api/repairs/fix')
def fix_repair(data: RepairFixIn, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s,'execute_updates')
    inst=db.get(Instance, data.instance_id)
    if not inst: raise HTTPException(404,'Instance not found')
    if not data.domain.strip() or not data.issue_id.strip():
        raise HTTPException(422,'Repair domain and issue ID are required')
    requested_action = (data.action or '').strip()
    if requested_action not in {'host_reboot', 'ha_restart'}:
        raise HTTPException(422, 'No Fleet Manager action for this repair')
    token=CredentialService().get_instance_token(db, inst.id)
    op = None
    try:
        adapter = HomeAssistantAdapter(inst, token)
        kind = 'repair_reboot' if requested_action == 'host_reboot' else 'repair_restart'
        try:
            op = create_operation(db, kind=kind, instance=inst,
                                  idempotency_key=f'{kind}:{inst.id}:{data.issue_id}',
                                  recovery_action='retry_restart',
                                  details={'issue_id': data.issue_id, 'domain': data.domain, 'actor': s.user.email})
        except ValueError:
            raise HTTPException(409, 'Another incompatible operation is already active for this instance')
        if op.status == 'succeeded':
            return {'ok': True, 'message': 'Repair action already verified', 'operation_id': op.id, 'status': op.status, 'recovery': op.recovery_action}
        reboot_result = {}
        if requested_action == 'host_reboot':
            adapter.trigger_shutdown_automations('host_reboot')
            inst.connectivity_state='restarting'; inst.health_state='restarting'; inst.last_failed_connection=now()
            db.flush()
            time.sleep(3)
            reboot_result = adapter.host_reboot()
        elif requested_action == 'ha_restart':
            adapter.post('/api/services/homeassistant/restart', {})
            inst.connectivity_state='restarting'; inst.health_state='restarting'; inst.last_failed_connection=now()
        else:
            raise HTTPException(422, 'No Fleet Manager action for this repair')
        op.status='restarting'; op.state='restarting'; op.started_at=now(); op.attempt_count=(op.attempt_count or 0)+1
        audit(db, action='repair_fix_requested', resource_type='repair', actor_user_id=s.user_id, resource_id=data.issue_id, instance_id=inst.id, request_id=op.request_id, metadata={'domain': data.domain, 'issue_id': data.issue_id, 'action': requested_action, 'state': inst.connectivity_state, 'reboot_endpoint': reboot_result.get('endpoint'), 'attempts': reboot_result.get('attempts'), 'operation_id': op.id})
        db.commit(); return {'ok': True, 'message': 'Repair action started', 'operation_id': op.id, 'status': op.status, 'recovery': op.recovery_action}
    except HTTPException:
        raise
    except Exception as exc:
        if op is not None:
            op.status='outcome_unknown'; op.state='outcome_unknown'; op.error_code=type(exc).__name__; op.error_message=safe_exception_message(exc); op.ended_at=now()
        audit(db, action='repair_fix_failed', resource_type='repair', actor_user_id=s.user_id, resource_id=data.issue_id, instance_id=inst.id, result='failed', metadata={'domain': data.domain, 'issue_id': data.issue_id, 'error': type(exc).__name__, 'operation_id': op.id if op is not None else None})
        db.commit(); raise HTTPException(502, f'Repair action failed ({type(exc).__name__})')

def refresh_pending_update_snapshot(db: Session, update: UpdateRecord, inst: Instance | None):
    """Refresh one pending update entity so progress/version shown in the UI is not stale DB data."""
    if not inst or update.installation_state not in {'available', 'installed'}:
        return
    try:
        token = CredentialService().get_instance_token(db, inst.id)
        raw = HomeAssistantAdapter(inst, token).get(f'/api/states/{update.entity_id}')
        attrs = raw.get('attributes') or {}
        installed = attrs.get('installed_version')
        latest = attrs.get('latest_version')
        in_progress = bool(attrs.get('in_progress')) or raw.get('state') in {'installing', 'updating'}
        same_version = installed and latest and installed == latest
        update.raw_json = json.dumps(raw or {}, default=str)[:20000]
        update.installed_version = installed or update.installed_version
        update.available_version = latest or update.available_version
        if same_version and not in_progress:
            update.installation_state = 'current'
            update.approval_state = 'not_required'
            update.skip_state = 'none'
        elif raw.get('state') == 'on' and not same_version:
            update.installation_state = 'available'
    except Exception:
        return

@app.get('/api/updates')
def updates(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_updates'); inst={i.id:i for i in db.query(Instance).all()}
    rows=[]
    records = db.query(UpdateRecord).order_by(UpdateRecord.critical_state.desc(), UpdateRecord.last_discovered.desc()).all()
    for u in records:
        if u.installation_state == 'available' or '"in_progress": true' in (u.raw_json or '').lower():
            refresh_pending_update_snapshot(db, u, inst.get(u.instance_id))
    db.flush(); db.commit()
    for u in records:
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
    require_permission(s,'create_deployment_plans'); return [{'id':p.id,'name':p.name,'status':p.status,'created_at':p.created_at.isoformat(),'summary':_safe(json.loads(p.summary_json))} for p in db.query(DeploymentPlan).order_by(DeploymentPlan.id.desc()).all()]
@app.get('/api/audit')
def audit_log(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_audit_history'); return [{'timestamp':a.timestamp.isoformat(),'actor_user_id':a.actor_user_id,'action':a.action,'resource_type':a.resource_type,'resource_id':a.resource_id,'instance_id':a.instance_id,'result':a.result,'metadata':_safe(json.loads(a.metadata_json or '{}'))} for a in db.query(AuditEvent).order_by(AuditEvent.id.desc()).limit(200).all()]

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
    db.commit(); return {'ok': op.status in {'succeeded','accepted','action_required'}, 'status': op.status, 'state': op.state, 'operation_id': op.id, 'update': serialize_update(upd), 'details': _safe(json.loads(op.details_json or '{}'))}

@app.post('/api/updates/bulk')
def bulk_updates(data: BulkUpdateIn, s=Depends(csrf), db: Session = Depends(get_db)):
    unique_ids = list(dict.fromkeys(int(x) for x in data.update_ids))
    if not unique_ids:
        raise HTTPException(422, 'Select at least one update')
    action = data.action.lower().strip()
    if action not in {'update', 'skip', 'review'}:
        raise HTTPException(422, 'Action must be update, skip, or review')
    require_permission(s, 'execute_updates' if action == 'update' else 'view_updates')
    found = {u.id: u for u in db.query(UpdateRecord).filter(UpdateRecord.id.in_(unique_ids)).all()}
    updates = [found[i] for i in unique_ids if i in found]
    instances = {i.id: i for i in db.query(Instance).filter(Instance.id.in_({u.instance_id for u in updates})).all()}
    policy = load_auto_policy(db)
    batch_id = secrets.token_urlsafe(18)
    job = JobRun(kind='bulk_update', status='running', started_at=now(), details_json='{}')
    db.add(job); db.flush()
    summary = {'action': action, 'batch_id': batch_id, 'job_id': job.id, 'selected': len(unique_ids), 'found': len(updates), 'updated': 0, 'action_required': 0, 'skipped': 0, 'reviewed': 0, 'blocked': [], 'failed': [], 'results': []}
    for missing_id in [i for i in unique_ids if i not in found]:
        item = {'update': missing_id, 'instance': None, 'operation': None, 'status': 'failed', 'reason': 'update_not_found'}
        summary['failed'].append(item); summary['results'].append(item)
    for upd in updates:
        inst = instances.get(upd.instance_id)
        item = {'update': upd.id, 'instance': inst.id if inst else None, 'operation': None, 'status': 'failed', 'reason': None}
        try:
            if not inst:
                raise LookupError('instance_not_found')
            if action in {'update', 'skip'} and (upd.installation_state != 'available' or (action == 'update' and upd.skip_state == 'skipped')):
                item.update(status='blocked', reason='not_pending'); summary['blocked'].append(dict(item)); summary['results'].append(item); continue
            if action == 'review':
                ok, reasons, _notes = review_update_for_auto(upd, policy)
                summary['reviewed'] += 1
                item.update(status='reviewed' if ok else 'blocked', reason=None if ok else ','.join(reasons[:5]))
                if not ok: summary['blocked'].append(dict(item))
                audit(db, action='update_reviewed', resource_type='update_record', actor_user_id=s.user_id, resource_id=upd.id, instance_id=upd.instance_id, metadata={'eligible': ok, 'reasons': reasons, 'batch_id': batch_id})
            else:
                op = install_update(db, inst, upd, actor=f'user:{s.user.email}') if action == 'update' else skip_update(db, inst, upd, actor=f'user:{s.user.email}')
                op.batch_id = op.batch_id or batch_id
                item.update(operation=op.id, status=op.status, reason=None if op.status == 'succeeded' else op.state)
                if op.status == 'succeeded':
                    if action == 'update': summary['updated'] += 1
                    else: summary['skipped'] += 1
                elif op.status == 'action_required': summary['action_required'] += 1
                else: summary['blocked'].append(dict(item))
            summary['results'].append(item)
        except Exception as exc:
            item.update(status='failed', reason=safe_exception_message(exc))
            summary['failed'].append(dict(item)); summary['results'].append(item)
    job.status = 'partial_failed' if summary['failed'] or summary['blocked'] else 'succeeded'
    job.ended_at = now(); job.details_json = json.dumps(summary, default=str)
    audit(db, action='bulk_update_completed', resource_type='job_run', actor_user_id=s.user_id, resource_id=job.id, result=job.status, metadata={'batch_id': batch_id, 'selected': len(unique_ids), 'failed': len(summary['failed']), 'blocked': len(summary['blocked'])})
    db.commit(); return summary

@app.get('/api/notifications')
def notifications(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_audit_history')
    return [{'id':n.id,'severity':n.severity,'title':n.title,'body':n.body,'status':n.status,'instance_id':n.instance_id,'update_record_id':n.update_record_id,'created_at':n.created_at.isoformat(),'acknowledged_at':n.acknowledged_at.isoformat() if n.acknowledged_at else None} for n in db.query(Notification).order_by(Notification.id.desc()).limit(200).all()]

@app.get('/api/operations')
def operations(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_audit_history')
    rows=[]
    for o in db.query(Operation).order_by(Operation.id.desc()).limit(200).all():
        rows.append({'id':o.id,'kind':o.kind,'instance_id':o.instance_id,'deployment_plan_id':o.deployment_plan_id,
                     'state':o.state,'status':o.status,'started_at':o.started_at.isoformat() if o.started_at else None,
                     'ended_at':o.ended_at.isoformat() if o.ended_at else None,'correlation_id':o.correlation_id,
                     'request_id':o.request_id,'batch_id':o.batch_id,'deadline_at':o.deadline_at.isoformat() if o.deadline_at else None,
                     'error_code':o.error_code,'error_message':public_error_message(o.error_code) if o.error_code else None,'recovery':o.recovery_action,
                     'attempt_count':o.attempt_count,'max_attempts':o.max_attempts,
                     'details':_safe(json.loads(o.details_json or '{}'))})
    return rows

@app.post('/api/operations/{operation_id}/retry')
def retry_operation(operation_id:int, s=Depends(csrf), db: Session = Depends(get_db)):
    require_permission(s, 'execute_updates')
    op = db.get(Operation, operation_id)
    if not op:
        raise HTTPException(404, 'Operation not found')
    if op.status not in {'failed', 'outcome_unknown', 'verification_pending', 'accepted'}:
        raise HTTPException(409, f'Operation is not retryable while {op.status}')
    inst = db.get(Instance, op.instance_id) if op.instance_id else None
    if not inst:
        raise HTTPException(422, 'Operation has no instance')
    token = CredentialService().get_instance_token(db, inst.id)
    adapter = HomeAssistantAdapter(inst, token)
    # Always reconcile first. A retry must never duplicate a completed or in-flight HA action.
    reconcile_operation(db, op, adapter=adapter)
    if op.status == 'succeeded':
        db.commit()
        return {'ok': True, 'status': op.status, 'state': op.state, 'operation_id': op.id, 'recovery': op.recovery_action}
    if op.kind == 'backup_create':
        db.commit()
        raise HTTPException(409, 'Backup outcome is still unverified. Inspect Home Assistant backup history; create a new backup only after confirming no backup was created.')
    details = json.loads(op.details_json or '{}')
    entity = details.get('entity_id')
    if op.kind in {'restart', 'repair_restart', 'repair_reboot'}:
        raise HTTPException(422, 'Restart/reboot operations cannot be resubmitted; verify Home Assistant and use recovery action: retry_restart')
    if entity:
        state = adapter.get(f'/api/states/{entity}')
        attrs = state.get('attributes') or {}
        if attrs.get('in_progress') or state.get('state') in {'installing', 'updating'}:
            op.status='reconnecting'; op.state='reconnecting'; db.commit()
            return {'ok': True, 'status': op.status, 'state': op.state, 'operation_id': op.id, 'recovery': op.recovery_action}
    if (op.attempt_count or 0) >= (op.max_attempts or 3):
        raise HTTPException(409, 'Retry limit reached; manual intervention required')
    if db.query(Operation).filter(Operation.instance_id == inst.id, Operation.id != op.id,
                                  Operation.status.in_({'queued','running','accepted','verification_pending','restarting','reconnecting'})).first():
        raise HTTPException(409, 'Another active operation blocks retry')
    op.status='queued'; op.state='queued'; op.error_code=None; op.error_message=None
    update = db.get(UpdateRecord, details.get('update_id'))
    if not update:
        raise HTTPException(422, 'Retry target update no longer exists')
    result = install_update(db, inst, update, actor=f'user:{s.user.email}') if op.kind == 'update_install' else skip_update(db, inst, update, actor=f'user:{s.user.email}')
    audit(db, action='operation_retried', resource_type='operation', actor_user_id=s.user_id, resource_id=op.id, instance_id=inst.id, metadata={'operation_id': op.id})
    db.commit()
    return {'ok': result.status in {'succeeded','accepted','action_required'}, 'status':result.status, 'state':result.state, 'operation_id':result.id, 'recovery':result.recovery_action}

@app.get('/api/jobs')
def jobs(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_audit_history')
    return [{'id':j.id,'kind':j.kind,'status':j.status,'started_at':j.started_at.isoformat(),'ended_at':j.ended_at.isoformat() if j.ended_at else None,'details':_safe(json.loads(j.details_json or '{}'))} for j in db.query(JobRun).order_by(JobRun.id.desc()).limit(100).all()]

@app.get('/api/backups')
def backups(s=Depends(current_session), db: Session = Depends(get_db)):
    require_permission(s,'view_audit_history')
    return [{'id':b.id,'instance_id':b.instance_id,'provider':b.provider,'status':b.status,'backup_id':b.backup_id,'name':b.name,'started_at':b.started_at.isoformat() if b.started_at else None,'completed_at':b.completed_at.isoformat() if b.completed_at else None,'details':_safe(json.loads(b.details_json or '{}'))} for b in db.query(BackupRecord).order_by(BackupRecord.id.desc()).limit(100).all()]

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
    details = json.loads(rec.details_json or '{}')
    operation_id = details.get('operation_id')
    op = db.get(Operation, operation_id) if operation_id else None
    db.commit(); return {'id':rec.id,'status':rec.status,'operation_id': operation_id,'backup_id':rec.backup_id,'details':_safe(details)}

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
