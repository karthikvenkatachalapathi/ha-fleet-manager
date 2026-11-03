import json
from types import SimpleNamespace
from datetime import timedelta
import pytest
from fastapi import HTTPException
from fleet_manager.db import Base,SessionLocal,engine
from fleet_manager.models import ApiPrincipal,ApiToken,Instance,PolicySetting,now
from fleet_manager.services.api_auth import authenticate_token,hash_token,CURRENT_API_PRINCIPAL
from fleet_manager.services import api_policy


def test_reduced_global_scope_blocks_request_after_initial_authentication():
    Base.metadata.create_all(engine)
    import secrets
    with SessionLocal() as db:
        inst=Instance(friendly_name='global-scope-test',url='https://ha.invalid');db.add(inst);db.flush()
        p=ApiPrincipal(name='scope-test',owner='test',purpose='test',role='admin',all_instances=True,include_future_instances=True,enabled=True,instance_ids_json=json.dumps([inst.id]));db.add(p);db.flush()
        tid=secrets.token_hex(12);raw=f'fleet.{tid}.{secrets.token_urlsafe(32)}'
        db.add(ApiToken(id=tid,principal_id=p.id,verifier=hash_token(raw),expires_at=now()+timedelta(days=1)));db.commit()
        snapshot=authenticate_token(db,raw)
        p.all_instances=False;p.include_future_instances=False;db.commit()
        authctx=CURRENT_API_PRINCIPAL.set(snapshot)
        # The request was accepted while full-fleet, then an administrator reduced scope.
        routevar=getattr(api_policy,'CURRENT_API_ROUTE',None)
        routectx=routevar.set(('PUT','/api/policies/auto_update_policy')) if routevar else None
        try:
            with pytest.raises(HTTPException):api_policy.assert_live_access(write=True)
        finally:
            CURRENT_API_PRINCIPAL.reset(authctx)
            if routevar:routevar.reset(routectx)
