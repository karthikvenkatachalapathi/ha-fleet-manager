from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fleet_manager.services import ha_adapter, api_policy


def test_revoked_api_authority_blocks_post_before_http_client(monkeypatch):
    attempted=[]
    class FakeClient:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):
            attempted.append('post')
            return SimpleNamespace(status_code=200,text='',raise_for_status=lambda:None)
    monkeypatch.setattr(ha_adapter.httpx,'Client',lambda **kwargs:FakeClient())
    def denied(instance_id,*,write=False):
        assert instance_id==42 and write is True
        raise HTTPException(401,'Revoked')
    monkeypatch.setattr(api_policy,'assert_live_access',denied)
    adapter=object.__new__(ha_adapter.HomeAssistantAdapter)
    adapter.instance=SimpleNamespace(id=42);adapter.base='https://ha.invalid';adapter.token='synthetic'
    with pytest.raises(HTTPException):adapter._post_allowed('/api/services/update/install',{})
    assert attempted==[]
