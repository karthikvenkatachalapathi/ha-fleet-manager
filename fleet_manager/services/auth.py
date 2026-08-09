from __future__ import annotations
from datetime import datetime, timedelta, timezone
import secrets
from argon2 import PasswordHasher
from sqlalchemy.orm import Session
from .. import settings
from ..models import Session as AppSession, User, now

ph = PasswordHasher()
PERMISSIONS = {
  'admin': {'view_instances','add_instances','modify_instances','delete_instances','replace_credentials','view_updates','create_deployment_plans','approve_critical_updates','approve_updates','execute_deployment_plans','execute_updates','modify_update_policies','create_schedules','execute_backups','restore_backups','manage_notifications','view_audit_history','manage_users','manage_application_settings'},
  'maintainer': {'view_instances','modify_instances','view_updates','create_deployment_plans','execute_deployment_plans','execute_updates','create_schedules','execute_backups','view_audit_history'},
  'read_only': {'view_instances','view_updates','view_audit_history'}
}

def ensure_admin(db: Session):
    if db.query(User).count() == 0:
        password = settings.require_secret('FLEET_ADMIN_PASSWORD', settings.ADMIN_PASSWORD)
        user = User(email=settings.ADMIN_EMAIL, password_hash=ph.hash(password), role='admin')
        db.add(user); db.commit()

def verify_password(hash_: str, password: str) -> bool:
    try: return ph.verify(hash_, password)
    except Exception: return False

def create_session(db: Session, user: User) -> AppSession:
    s = AppSession(id=secrets.token_urlsafe(36), user_id=user.id, csrf_token=secrets.token_urlsafe(32), expires_at=datetime.now(timezone.utc)+timedelta(hours=12))
    db.add(s); return s

def validate_session(db: Session, sid: str | None) -> AppSession | None:
    if not sid: return None
    s=db.get(AppSession, sid)
    if not s or s.revoked_at: return None
    expires = s.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc): return None
    return s

def require_permission(session: AppSession, permission: str):
    if permission not in PERMISSIONS.get(session.user.role, set()):
        raise PermissionError('Permission denied')
