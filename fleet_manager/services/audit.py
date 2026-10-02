from __future__ import annotations
import json
from ..models import AuditEvent

def audit(db, *, action: str, resource_type: str, actor_user_id=None, resource_id=None, instance_id=None, result='success', before=None, after=None, metadata=None, request_id=None):
    ev = AuditEvent(action=action, resource_type=resource_type, actor_user_id=actor_user_id, resource_id=str(resource_id) if resource_id is not None else None, instance_id=instance_id, request_id=request_id, result=result, before_summary=json.dumps(before, sort_keys=True)[:4000] if before is not None else None, after_summary=json.dumps(after, sort_keys=True)[:4000] if after is not None else None, metadata_json=json.dumps(metadata or {}, sort_keys=True)[:4000])
    db.add(ev)
    return ev
