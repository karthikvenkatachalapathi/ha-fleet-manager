"""Bearer request boundary: strict scheme, bounded limits, attribution and no cookie fallback."""
from collections import OrderedDict, deque
from datetime import timedelta
import json
import re
import threading
import time
import uuid
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from ..db import SessionLocal
from ..models import ApiToken, AuditEvent, now
from .api_auth import authenticate_token, CURRENT_API_PRINCIPAL
from .api_policy import authorize_route, CURRENT_API_ROUTE

class WindowLimiter:
    def __init__(self,limit,seconds=60,max_keys=4096):
        self.limit=limit;self.seconds=seconds;self.max_keys=max_keys
        self.rows=OrderedDict();self.lock=threading.Lock()
    def check(self,key):
        stamp=time.monotonic()
        with self.lock:
            rows=self.rows.pop(key,deque())
            while rows and rows[0]<=stamp-self.seconds:rows.popleft()
            self.rows[key]=rows
            while len(self.rows)>self.max_keys:self.rows.popitem(last=False)
            if len(rows)>=self.limit:raise HTTPException(429,'Rate limit exceeded',headers={'Retry-After':'60'})
            rows.append(stamp)

IP_LIMITER=WindowLimiter(240)
TOKEN_LIMITER=WindowLimiter(600)


def _authenticate(header):
    if len(header)>512 or not re.fullmatch(r'Bearer [A-Za-z0-9_.-]+',header):
        raise HTTPException(401,'Invalid bearer authentication',headers={'WWW-Authenticate':'Bearer'})
    with SessionLocal() as db:
        principal=authenticate_token(db,header[7:])
        row=db.get(ApiToken,principal.token_id)
        stamp=now()
        last=row.last_used_at
        if last is not None and last.tzinfo is None:last=last.replace(tzinfo=stamp.tzinfo)
        if last is None or stamp-last>timedelta(seconds=60):
            row.last_used_at=stamp;db.commit()
        return principal


def _audit(principal,method,status,request_id,route):
    # Deliberately excludes raw URL, query, headers and body, including malformed tokens.
    with SessionLocal() as db:
        db.add(AuditEvent(action='api_token_request',resource_type='api_request',resource_id=route[:120],
            actor_user_id=None,request_id=request_id,result='success' if status<400 else 'denied',
            metadata_json=json.dumps({'actor_type':'api','api_principal_id':principal.principal_id if principal else None,
                'api_token_id':principal.token_id if principal else None,'method':method,'status_code':status})))
        db.commit()


def register_bearer_boundary(app):
    @app.middleware('http')
    async def bearer_boundary(request,call_next):
        header=request.headers.get('authorization')
        if header is None:
            response=await call_next(request)
            if request.url.path.startswith('/api/access/'):
                response.headers['Cache-Control']='no-store'
            return response
        principal=None;context=None;route_context=None;response=None
        request_id=str(uuid.uuid4())
        try:
            if len(request.headers.getlist('authorization'))!=1:
                raise HTTPException(401,'Ambiguous authorization')
            IP_LIMITER.check(request.client.host if request.client else 'unknown')
            if request.url.scheme!='https':raise HTTPException(403,'HTTPS is required for API tokens')
            principal=await run_in_threadpool(_authenticate,header)
            TOKEN_LIMITER.check(principal.token_id)
            authorize_route(principal,request.method,request.url.path)
            request.state.api_principal=principal
            request.state.api_request_id=request_id
            context=CURRENT_API_PRINCIPAL.set(principal)
            route_context=CURRENT_API_ROUTE.set((request.method,request.url.path))
            response=await call_next(request)
        except HTTPException as exc:
            response=JSONResponse({'detail':exc.detail},status_code=exc.status_code,headers=exc.headers)
        except Exception:
            # Never reflect exception strings (possibly credentials or remote bodies).
            response=JSONResponse({'detail':'Request failed; check activity before retrying any mutation.'},status_code=500)
        finally:
            if context is not None:CURRENT_API_PRINCIPAL.reset(context)
            if route_context is not None:CURRENT_API_ROUTE.reset(route_context)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Request-ID']=request_id
        route=getattr(request.scope.get('route'),'path','unmapped')
        await run_in_threadpool(_audit,principal,request.method,response.status_code,request_id,route)
        return response
