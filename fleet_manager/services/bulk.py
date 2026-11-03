"""Durable bulk intents; the existing scheduler dispatches one item per home per cycle.

Queue entries are NOT active Operations: the per-instance exclusion constraint stays
intact. Claims and progress are committed before any network I/O. A lost worker is
reconciled, never blindly replayed. No live request/session or credential is stored.
"""
from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import update as sql_update

from ..models import JobRun, Instance, Operation, UpdateRecord, User, now
from .audit import audit
from .auth import require_permission
from .reliability import ACTIVE_STATUSES, aware_utc, safe_exception_message, durable_dispatch, current_terminal_hold

SCHEMA = 2
PENDING = {'queued', 'dispatching', 'running', 'accepted', 'verification_pending', 'reconnecting', 'restarting', 'waiting_verification'}
SUCCESS = {'succeeded', 'reviewed'}
LEASE_SECONDS = 300
QUEUE_LIFETIME = timedelta(hours=12)


def summarize(summary: dict) -> dict:
    rows = summary['results']
    summary['updated'] = sum(r['status'] == 'succeeded' for r in rows) if summary['action'] == 'update' else 0
    summary['skipped'] = sum(r['status'] == 'succeeded' for r in rows) if summary['action'] == 'skip' else 0
    summary['reviewed'] = sum(r['status'] == 'reviewed' for r in rows)
    summary['action_required'] = sum(r['status'] == 'action_required' for r in rows)
    summary['queued'] = sum(r['status'] in {'queued', 'waiting_verification'} for r in rows)
    summary['running'] = sum(r['status'] in PENDING - {'queued', 'waiting_verification'} for r in rows)
    summary['pending'] = summary['queued'] + summary['running']
    summary['blocked'] = [dict(r) for r in rows if r['status'] == 'blocked']
    summary['failed'] = [dict(r) for r in rows if r['status'] not in PENDING | SUCCESS | {'blocked', 'action_required'}]
    summary['completed'] = len(rows) - summary['pending']
    summary['status'] = ('running' if summary['pending'] else 'partial_failed' if summary['failed'] or summary['blocked'] else 'action_required' if summary['action_required'] else 'succeeded')
    return summary


class LostLease(RuntimeError):
    pass


class DispatchDenied(PermissionError):
    """Current authorization rejected before a downstream POST was attempted."""
    pass


def _save(db, job, summary, *, release=False):
    # Fence every progress write by the currently owned lease, not a stale ORM object.
    lease_id = summary['_lease']['id']
    summarize(summary)
    stored = {k: v for k, v in summary.items() if not (release and k == '_lease')}
    changed = db.execute(sql_update(JobRun).where(
        JobRun.id == job.id, JobRun.details_json.contains('"id": "' + lease_id + '"'))
        .values(status=summary['status'], ended_at=None if summary['pending'] else now(),
                details_json=json.dumps(stored, default=str))
        .execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        db.rollback()
        raise LostLease('bulk_worker_claim_lost')
    db.commit()
    if release:
        summary.pop('_lease', None)


def enqueue(db, *, update_ids, action, submitter):
    ids = list(dict.fromkeys(update_ids))
    found = {u.id: u for u in db.query(UpdateRecord).filter(UpdateRecord.id.in_(ids)).all()}
    batch_id = secrets.token_urlsafe(18)
    job = JobRun(kind='bulk_update', status='queued', started_at=now())
    db.add(job)
    db.flush()
    summary = {'schema_version': SCHEMA, 'action': action, 'batch_id': batch_id, 'job_id': job.id,
               'selected': len(ids), 'found': len(found), 'submitted_at': now().isoformat(),
               '_submitter': {'user_id': submitter.user_id}, 'results': []}
    if hasattr(submitter, 'principal_id'):
        summary['_submitter'] = {'principal_id': submitter.principal_id, 'api_token_id': submitter.token_id}
    for uid in ids:
        u = found.get(uid)
        summary['results'].append({'update': uid, 'instance': u.instance_id if u else None,
                                   'operation': None, 'status': 'queued' if u else 'failed',
                                   'reason': None if u else 'update_not_found',
                                   'target_version': u.available_version if u else None})
    summarize(summary)
    job.status = summary['status']
    if summary['pending']:
        summary['status'] = job.status = 'queued'
    else:
        job.ended_at = now()
    audit(db, action='bulk_update_queued', resource_type='job_run', actor_user_id=submitter.user_id,
          resource_id=job.id, metadata={'batch_id': batch_id, 'selected': len(ids), 'action': action})
    job.details_json = json.dumps(summary)
    db.commit()
    return public_summary(summary)


def public_summary(summary):
    return {k: v for k, v in summary.items() if not k.startswith('_')}


def read_summary(db, job):
    """Derive counters from current operations, including pre-fix historical batches."""
    summary = json.loads(job.details_json or '{}')
    if job.kind != 'bulk_update' or not isinstance(summary.get('results'), list):
        return summary
    for item in summary['results']:
        if item.get('operation'):
            op = db.get(Operation, item['operation'])
            if op is not None:
                item.update(status=op.status, reason=op.state)
    return public_summary(summarize(summary))


def is_managed_job(job):
    if job.kind != 'bulk_update':
        return False
    try:
        return json.loads(job.details_json or '{}').get('schema_version') == SCHEMA
    except (ValueError, TypeError, AttributeError):
        return False


def _authority(db, summary, iid):
    owner = summary['_submitter']
    if 'principal_id' in owner:
        from .api_auth import revalidate_principal
        from .api_policy import authorize_route
        ref = SimpleNamespace(token_id=owner['api_token_id'], principal_id=owner['principal_id'])
        principal = revalidate_principal(db, ref, instance_id=iid, permission='execute_updates')
        authorize_route(principal, 'POST', '/api/updates/bulk')
        return principal
    user = db.get(User, owner['user_id'], populate_existing=True)
    if not user or not user.is_active:
        raise PermissionError('submitter_disabled')
    principal = SimpleNamespace(user=user, user_id=user.id)
    require_permission(principal, 'execute_updates' if summary['action'] in {'update', 'skip'} else 'view_updates')
    return principal


@contextmanager
def authorized_dispatch(db, summary, iid):
    from .api_auth import CURRENT_API_PRINCIPAL
    from .api_policy import CURRENT_API_ROUTE, bind_scope
    principal = _authority(db, summary, iid)
    token = CURRENT_API_PRINCIPAL.set(principal if hasattr(principal, 'principal_id') else None)
    route = CURRENT_API_ROUTE.set(('POST', '/api/updates/bulk'))
    if hasattr(principal, 'principal_id'):
        bind_scope(db, principal)
    try:
        yield principal
    finally:
        db.info.pop('api_principal', None)
        CURRENT_API_PRINCIPAL.reset(token)
        CURRENT_API_ROUTE.reset(route)


def _claim(db, job):
    original = job.details_json
    summary = json.loads(original or '{}')
    if not is_managed_job(job) or not summary.get('pending'):
        return None
    lease = summary.get('_lease') or {}
    if lease.get('until') and aware_utc(__import__('datetime').datetime.fromisoformat(lease['until'])) > now():
        return None
    summary['_lease'] = {'id': secrets.token_hex(12), 'until': (now() + timedelta(seconds=LEASE_SECONDS)).isoformat()}
    changed = db.execute(sql_update(JobRun).where(JobRun.id == job.id, JobRun.details_json == original)
                         .values(status='running', details_json=json.dumps(summary)).execution_options(synchronize_session=False)).rowcount
    db.commit()
    if not changed:
        return None
    db.refresh(job)
    return summary


def run_cycle(session_factory, *, install=None, skip=None, review=None):
    """No background thread per request; safe across concurrent scheduler processes."""
    from .automation import install_update, skip_update, review_update_for_auto, load_auto_policy
    install = install or install_update
    skip = skip or skip_update
    review = review or review_update_for_auto
    dispatched_homes = set()
    with session_factory() as index_db:
        job_ids = [j.id for j in index_db.query(JobRun).filter(JobRun.kind == 'bulk_update', JobRun.status.in_(['queued', 'running'])).order_by(JobRun.id).all() if is_managed_job(j)]
    for job_id in job_ids:
        with session_factory() as db:
            job = db.get(JobRun, job_id)
            summary = _claim(db, job)
            if summary is None:
                continue
            try:
                for item in summary['results']:
                    if item['status'] not in PENDING:
                        continue
                    # Recover a worker lost after reservation without replaying its POST.
                    if item['status'] == 'dispatching' and not item['operation']:
                        op = db.query(Operation).filter(Operation.batch_id == summary['batch_id'], Operation.instance_id == item['instance']).all()
                        op = next((o for o in op if json.loads(o.details_json or '{}').get('update_id') == item['update']), None)
                        if op:
                            item['operation'] = op.id
                        else:
                            item.update(status='outcome_unknown', reason='worker_interrupted_before_confirmation')
                    if item['operation']:
                        op = db.get(Operation, item['operation'])
                        item.update(status=op.status if op else 'outcome_unknown', reason=op.state if op else 'operation_missing')
                    if item['status'] not in {'queued', 'waiting_verification'}:
                        continue
                    if now() - aware_utc(job.started_at) > QUEUE_LIFETIME:
                        item.update(status='blocked', reason='queue_authorization_expired')
                        continue
                    iid = item['instance']
                    # One dispatch per home per cycle gives other homes fair progress.
                    if iid in dispatched_homes:
                        continue
                    active = db.query(Operation).filter(Operation.instance_id == iid, Operation.status.in_(ACTIVE_STATUSES)).first()
                    if active:
                        item.update(status='waiting_verification', reason=f'waiting_for_operation:{active.id}')
                        continue
                    restart = next((op for op in db.query(Operation).filter(Operation.instance_id == iid, Operation.status == 'action_required').all() if current_terminal_hold(op)), None)
                    if restart:
                        item.update(status='waiting_verification', reason=f'waiting_for_restart:{restart.id}')
                        continue
                    # Preserve legacy eligibility: historical unknown rows predate
                    # durable dispatch and must not freeze unrelated future targets.
                    # Exact-target legacy replay is still rejected by install/skip;
                    # every new durable-protocol ambiguity holds its whole home.
                    uncertain = next((op for op in db.query(Operation).filter(
                        Operation.instance_id == iid,
                        Operation.status.in_(['outcome_unknown', 'manual_intervention_required'])).all()
                        if durable_dispatch(op)), None)
                    if uncertain:
                        item.update(status='blocked', reason=f'previous_outcome_unverified:{uncertain.id}')
                        continue
                    item.update(status='dispatching', reason='reserved_for_dispatch')
                    summary['_lease']['until'] = (now() + timedelta(seconds=LEASE_SECONDS)).isoformat()
                    _save(db, job, summary)
                    try:
                        with authorized_dispatch(db, summary, iid) as actor:
                            inst, upd = db.get(Instance, iid), db.get(UpdateRecord, item['update'])
                            if not inst or not upd or upd.instance_id != iid:
                                item.update(status='blocked', reason='resource_no_longer_authorized_or_available')
                            elif upd.available_version != item['target_version']:
                                item.update(status='blocked', reason='target_version_changed')
                            elif summary['action'] != 'review' and (upd.installation_state != 'available' or (summary['action'] == 'update' and upd.skip_state == 'skipped')):
                                item.update(status='blocked', reason='not_pending')
                            elif summary['action'] == 'review':
                                policy = load_auto_policy(db)
                                db.commit()
                                ok, reasons, _ = review(upd, policy)
                                item.update(status='reviewed' if ok else 'blocked', reason=None if ok else ','.join(reasons[:5]))
                                db.commit()
                            else:
                                # The adapter also revalidates bearer scope immediately before every POST.
                                def guard():
                                    summary['_lease']['until'] = (now() + timedelta(seconds=LEASE_SECONDS)).isoformat()
                                    _save(db, job, summary)
                                    try:
                                        with session_factory() as guard_db:
                                            _authority(guard_db, summary, iid)
                                    except Exception as exc:
                                        if isinstance(exc, PermissionError) or getattr(exc, 'status_code', None) in {401, 403, 404}:
                                            raise DispatchDenied('authorization_changed_before_dispatch') from None
                                        raise
                                fn = install if summary['action'] == 'update' else skip
                                op = fn(db, inst, upd, actor=f'user:{actor.user.email}', batch_id=summary['batch_id'], dispatch_guard=guard)
                                item.update(operation=op.id, status=op.status, reason=op.state)
                                dispatched_homes.add(iid)
                                db.commit()
                    except LostLease:
                        raise  # a stale worker may neither dispatch nor overwrite progress
                    except Exception as exc:
                        from sqlalchemy.exc import IntegrityError, OperationalError
                        db.rollback()
                        # If an intent was committed, preserve it even when final persistence
                        # failed after the POST. Never make that action eligible for replay.
                        owned = db.query(Operation).filter(Operation.batch_id == summary['batch_id'], Operation.instance_id == iid).all()
                        owned = next((o for o in owned if json.loads(o.details_json or '{}').get('update_id') == item['update']), None)
                        denied = isinstance(exc, PermissionError) or getattr(exc, 'status_code', None) in {401, 403, 404}
                        if owned:
                            if denied and (isinstance(exc, DispatchDenied) or not json.loads(owned.details_json or '{}').get('dispatch_reserved')):
                                owned.status = 'blocked'; owned.state = 'authorization_changed_before_dispatch'; owned.ended_at = now()
                                db.commit()
                            item.update(operation=owned.id, status=owned.status, reason=owned.state)
                        elif isinstance(exc, (IntegrityError, OperationalError)) or (isinstance(exc, ValueError) and str(exc).startswith('active operation')):
                            item.update(status='waiting_verification', reason='waiting_for_concurrent_operation')
                        else:
                            item.update(status='blocked' if denied else 'failed', reason=safe_exception_message(exc))
                    _save(db, job, summary)
                _save(db, job, summary, release=True)
            except Exception:
                db.rollback()
                # The durable lease expires. Next cycle reconciles any dispatching item,
                # never resubmits an unconfirmed action. Other batches can still progress.
                continue
