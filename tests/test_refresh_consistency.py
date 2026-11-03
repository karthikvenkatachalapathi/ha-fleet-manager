from types import SimpleNamespace

import pytest

from fleet_manager.services.ha_adapter import HomeAssistantAdapter


@pytest.mark.parametrize('envelope', [{}, {'states': []}, None, 'not states', [None], [{}]])
def test_discovery_rejects_malformed_success_envelopes(envelope):
    adapter = object.__new__(HomeAssistantAdapter)
    adapter.get = lambda path: envelope
    with pytest.raises(ValueError):
        adapter.discover_updates()


def test_discovery_accepts_legitimate_empty_states():
    adapter = object.__new__(HomeAssistantAdapter)
    adapter.get = lambda path: []
    assert adapter.discover_updates() == []


@pytest.mark.parametrize('state', ['unknown', 'unavailable'])
def test_discovery_keeps_unknown_states_explicit(state):
    adapter = object.__new__(HomeAssistantAdapter)
    adapter.get = lambda path: [{
        'entity_id': 'update.test', 'state': state,
        'attributes': {'installed_version': '1', 'latest_version': '2'},
    }]
    rows = adapter.discover_updates()
    assert len(rows) == 1
    assert rows[0]['state'] == state


def test_discovery_rejects_malformed_update_attributes():
    adapter = object.__new__(HomeAssistantAdapter)
    adapter.get = lambda path: [{
        'entity_id': 'update.test', 'state': 'on', 'attributes': []
    }]
    with pytest.raises(ValueError):
        adapter.discover_updates()
