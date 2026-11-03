import hashlib
import hmac
import json
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import ApiPrincipal, ApiToken, Instance

CURRENT_API_PRINCIPAL = ContextVar('current_api_principal', default=None)

@dataclass(frozen=True)
class BearerPrincipal:
    token_id: str
    principal_id: int
    role: str
    instance_ids: set[int]
    all_instances: bool
    include_future_instances: bool
    expires_at: datetime
    user_id: int
    user: object

def hash_token(token_string: str) -> str:
    return hashlib.sha256(token_string.encode('utf-8')).hexdigest()

def _unauthorized():
    raise HTTPException(status_code=401, detail='Invalid or expired bearer token', headers={'WWW-Authenticate': 'Bearer'})

def _expiry(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

def _principal_snapshot(db, principal, token):
    ids = set(json.loads(principal.instance_ids_json or '[]'))
    if principal.all_instances and principal.include_future_instances:
        ids = set(db.scalars(select(Instance.id)))
    user = SimpleNamespace(role='admin' if principal.role == 'admin' else 'read_only', email=f'api:{principal.id}', id=-principal.id, is_active=True)
    return BearerPrincipal(token.id, principal.id, principal.role, ids, principal.all_instances, principal.include_future_instances, _expiry(token.expires_at), -principal.id, user)

def authenticate_token(db: Session, token_string: str) -> BearerPrincipal:
    if not isinstance(token_string, str):
        _unauthorized()
    pieces = token_string.split('.')
    if len(pieces) != 3 or pieces[0] != 'fleet' or not pieces[1] or len(pieces[2]) < 32:
        _unauthorized()
    _, token_id, _ = pieces
    token = db.get(ApiToken, token_id)
    if not token or not hmac.compare_digest(token.verifier, hash_token(token_string)):
        _unauthorized()
    principal = db.get(ApiPrincipal, token.principal_id)
    if not principal or not principal.enabled or token.revoked_at or _expiry(token.expires_at) <= datetime.now(timezone.utc):
        _unauthorized()
    return _principal_snapshot(db, principal, token)

def revalidate_principal(db: Session, snapshot: BearerPrincipal, instance_id=None, permission=None):
    token = db.get(ApiToken, snapshot.token_id, populate_existing=True)
    principal = db.get(ApiPrincipal, snapshot.principal_id, populate_existing=True)
    if (not token or not principal or token.principal_id != principal.id or
        not principal.enabled or token.revoked_at or _expiry(token.expires_at) <= datetime.now(timezone.utc)):
        _unauthorized()
    current = _principal_snapshot(db, principal, token)
    if instance_id is not None and instance_id not in current.instance_ids:
        raise HTTPException(status_code=403, detail='Instance access denied')
    if permission and permission != 'read' and principal.role != 'admin':
        raise HTTPException(status_code=403, detail='Insufficient permission')
    return current
