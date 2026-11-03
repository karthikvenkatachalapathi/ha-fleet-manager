"""Offline native SQLite/HA-GET refresh regression; never production state."""
import json, threading
from datetime import timedelta
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from fleet_manager.db import Base
from fleet_manager.models import Instance, UpdateRecord, InstanceCredential, Approval, Operation, JobRun, AuditEvent, Notification, now
from fleet_manager.services import ha_adapter as H
from fleet_manager.services.credentials import CredentialService

@pytest.fixture
def fleet(tmp_path,monkeypatch):
    e=create_engine('sqlite:///'+str(tmp_path/'fixture.db'),connect_args={'check_same_thread':False,'timeout':2})
    @event.listens_for(e,'connect')
    def wal(c,_):c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA busy_timeout=2000')
    Base.metadata.create_all(e); S=sessionmaker(bind=e,autoflush=False)
    with S() as d:
        i=Instance(friendly_name='Synthetic',url='http://fixture.invalid',last_update_scan=now()-timedelta(hours=6),maintenance_hold=True)
        d.add(i);d.flush();iid=i.id
        CredentialService().set_instance_token(d,iid,'synthetic-token')
        u=UpdateRecord(instance_id=iid,entity_id='update.home_assistant_core_update',component='Home Assistant Core',installed_version='1',available_version='2',installation_state='available',approval_state='required',raw_json=json.dumps({'state':'on','attributes':{'in_progress':True}}),last_discovered=now()-timedelta(hours=6))
        d.add(u);d.flush();uid=u.id
        d.add(Operation(instance_id=iid,kind='update_install',status='action_required',state='InstalledRestartRequired',details_json=json.dumps({'update_id':uid,'target_version':'2'})))
        d.commit()
    states=[{'entity_id':'update.home_assistant_core_update','state':'off','attributes':{'installed_version':'2','latest_version':'2','in_progress':False}}]
    hook=[None]; calls=[]
    def initialize(self,instance,token):self.instance=instance;self.token=token
    def get(self,path):
        calls.append(path)
        assert path in {'/api/states','/api/config'}
        if hook[0]:hook[0](path)
        return {'version':'synthetic','location_name':'Synthetic'} if path=='/api/config' else states
    def forbidden(*a,**k):raise AssertionError('HA mutation forbidden')
    monkeypatch.setattr(H.HomeAssistantAdapter,'__init__',initialize);monkeypatch.setattr(H.HomeAssistantAdapter,'get',get)
    monkeypatch.setattr(H.HomeAssistantAdapter,'post',forbidden);monkeypatch.setattr(H.HomeAssistantAdapter,'supervisor_post',forbidden)
    yield S,iid,uid,states,hook,calls
    e.dispose()

def run(S,iid):
    with S() as d:
        n=H.sync_updates(d,d.get(Instance,iid),'synthetic-token');d.commit();return n

@pytest.mark.parametrize('change',['url','credential','approval','scan','new_update'])
def test_concurrent_change_rejects_entire_observation(fleet,change):
    S,iid,uid,states,hook,calls=fleet
    with S() as d:original=d.get(Instance,iid).last_update_scan
    def edit(path):
        if path!='/api/states':return
        hook[0]=None
        with S() as d:
            if change=='url':d.get(Instance,iid).url='http://other.invalid'
            elif change=='credential':CredentialService().set_instance_token(d,iid,'replacement-synthetic')
            elif change=='approval':d.get(UpdateRecord,uid).approval_state='approved'
            elif change=='scan':d.get(Instance,iid).last_update_scan=now()
            else:d.add(UpdateRecord(instance_id=iid,entity_id='update.new',component='new'))
            d.commit()
    hook[0]=edit
    with pytest.raises(RuntimeError,match='changed'):run(S,iid)
    with S() as d:
        u=d.get(UpdateRecord,uid);i=d.get(Instance,iid)
        assert u.installed_version=='1' and json.loads(u.raw_json)['attributes']['in_progress'] is True
        if change!='scan':assert i.last_update_scan==original
        if change=='approval':assert u.approval_state=='approved'
        assert i.maintenance_hold is True
        assert d.query(Operation).one().status=='action_required'

@pytest.mark.parametrize('decision',['approved','rejected'])
@pytest.mark.parametrize('absence',['unavailable','unknown','missing'])
def test_exact_target_decision_survives_absence_and_new_target_requires_new_decision(fleet,decision,absence):
    S,iid,uid,states,hook,calls=fleet
    with S() as d:
        d.get(UpdateRecord,uid).approval_state=decision
        d.add(Approval(instance_id=iid,update_record_id=uid,target_version='2',status=decision));d.commit()
    original=dict(states[0]);states.clear()
    if absence!='missing':states.append({'entity_id':original['entity_id'],'state':absence,'attributes':{}})
    assert run(S,iid)==0
    with S() as d:assert d.get(UpdateRecord,uid).installation_state=='unavailable';assert d.get(UpdateRecord,uid).approval_state==decision
    states[:]=[{'entity_id':original['entity_id'],'state':'on','attributes':{'installed_version':'1','latest_version':'2','in_progress':False}}]
    assert run(S,iid)==1
    with S() as d:assert d.get(UpdateRecord,uid).approval_state==decision;assert d.get(Instance,iid).pending_approvals==0
    states[0]['attributes']['latest_version']='3';run(S,iid)
    with S() as d:
        assert d.get(UpdateRecord,uid).approval_state=='required'
        assert d.get(Instance,iid).pending_approvals==1
        assert d.query(Approval).one().status==decision
        assert d.query(Operation).one().status=='action_required'
        assert d.get(Instance,iid).maintenance_hold is True
        assert d.query(Notification).count()==0

@pytest.mark.parametrize('payload',[{},None,[{}],[{'entity_id':'update.test','state':'off','attributes':[]}],[{'entity_id':'update.test','state':'off','attributes':{'in_progress':'false'}}]])
def test_malformed_response_cannot_modify_cache_or_freshness(fleet,monkeypatch,payload):
    S,iid,uid,states,hook,calls=fleet
    with S() as d:before=tuple(d.query(Instance.last_update_scan).one());raw=d.get(UpdateRecord,uid).raw_json
    monkeypatch.setattr(H.HomeAssistantAdapter,'get',lambda self,path:payload)
    with pytest.raises(ValueError):run(S,iid)
    with S() as d:assert tuple(d.query(Instance.last_update_scan).one())==before;assert d.get(UpdateRecord,uid).raw_json==raw

def test_apply_writer_is_atomic_against_late_approval(fleet,monkeypatch):
    S,iid,uid,states,hook,calls=fleet;entered=threading.Event();done=threading.Event();errors=[];threads=[];original=H._apply_update_snapshot
    def writer():
        try:
            with S() as d:
                entered.set();d.execute(text("UPDATE update_records SET approval_state='approved' WHERE id=:id"),{'id':uid});d.commit()
        except Exception as e:errors.append(type(e).__name__)
        finally:done.set()
    def checked_apply(db,inst,updates):
        t=threading.Thread(target=writer);threads.append(t);t.start();assert entered.wait(1)
        assert not done.wait(.15),'concurrent approval writer was not serialized'
        return original(db,inst,updates)
    monkeypatch.setattr(H,'_apply_update_snapshot',checked_apply)
    assert run(S,iid)==0
    for t in threads:t.join(3)
    assert done.is_set() and errors==[]
    with S() as d:assert d.get(UpdateRecord,uid).approval_state=='approved';assert d.get(UpdateRecord,uid).installed_version=='2'

def test_explicit_refresh_releases_writer_during_each_ha_get(fleet):
    from fleet_manager import app as A
    S,iid,uid,states,hook,calls=fleet;probes=[]
    def writer_probe(path):
        with S() as other:
            other.add(AuditEvent(action='synthetic-writer-probe',resource_type='fixture'));other.commit();probes.append(path)
    hook[0]=writer_probe
    with S() as d:summary=A.run_update_discovery(d,actor='fixture');d.commit()
    assert summary['sync_ok']==1 and summary['sync_failed']==0
    assert len(probes)==3 and calls==['/api/config','/api/states','/api/states']
    with S() as d:
        assert d.query(JobRun).one().status=='succeeded'
        assert d.get(UpdateRecord,uid).installed_version=='2'
        assert d.get(Instance,iid).maintenance_hold is True
        assert d.query(Operation).one().status=='action_required'
        assert d.query(Notification).count()==0
