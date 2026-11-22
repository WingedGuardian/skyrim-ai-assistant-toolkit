import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from test_whats_here import endpoint


def setup_inventory(replies, inventory=None):
    inventory = inventory if inventory is not None else {'owner': {'formId': '00000014'}, 'count': 1, 'returned': 1, 'truncated': False, 'items': [{'formId': 'ABC', 'name': '', 'count': 1}]}
    replies['inspect'] = lambda body: inventory if body.get('kind') == 'inventory' else {'actor': {'formId': '00000014'}, 'activeEffects': []}


def test_effects_inventory_keeps_unnamed_raw_and_same_owner(endpoint):
    run, replies, requests, _ = endpoint
    setup_inventory(replies)
    result = run('effects', '--inventory')
    assert result.returncode == 0, result.stdout + result.stderr
    context = json.loads(result.stdout)['inventoryContext']
    assert context['complete'] is True
    snap = context['snapshots'][0]
    assert snap['phase'] == 'current' and snap['timestamp']
    assert snap['raw']['items'][0]['name'] == ''
    assert ('inspect', {'kind': 'inventory', 'formId': '0x14', 'limit': 500}) in requests


@pytest.mark.parametrize('flags', [('near', '--inventory'), ('effects', '--inventory-limit', '2'), ('effects', '--inventory', '--inventory-limit', '0'), ('watch', '--inventory', '--inventory-limit', '1001')])
def test_invalid_inventory_options_never_connect(endpoint, flags):
    run, _, requests, _ = endpoint
    result = run(*flags)
    assert result.returncode == 2
    assert requests == []


@pytest.mark.parametrize('changes', [ {'owner': {'formId': '00000015'}}, {'count': -1}, {'returned': True}, {'items': 'bad'}, {'truncated': 'false'}, {'count': 2}, {'items': [{'count': float('nan')}]}, {'owner': {}} ])
def test_unverified_inventory_is_gap_without_losing_effects(endpoint, changes):
    run, replies, _, _ = endpoint
    raw = {'owner': {'formId': '00000014'}, 'count': 1, 'returned': 1, 'truncated': False, 'items': [{'formId': 'ABC', 'count': 1}]}
    raw.update(changes)
    setup_inventory(replies, raw)
    result = run('effects', '--inventory')
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data['inventoryContext']['complete'] is False
    assert data['inventoryContext']['gaps']
    assert data['native']['activeEffects'] == []


def test_watch_inventory_only_before_and_after_cleanup(endpoint):
    run, replies, requests, _ = endpoint
    setup_inventory(replies)
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = {'schemaVersion': 1, 'sessionId': 's', 'generation': 1, 'data': {'captureId': 'own', 'active': False, 'events': [{'sequence': 1}]}}
    result = run('watch', '--inventory', '--seconds', '.01')
    assert result.returncode == 0, result.stdout
    data = json.loads(result.stdout)
    assert [s['phase'] for s in data['inventoryContext']['snapshots']] == ['before', 'after']
    inv = [i for i,(key,body) in enumerate(requests) if key == 'inspect' and body.get('kind') == 'inventory']
    capture = [i for i,(key,body) in enumerate(requests) if key == 'probe.watch']
    assert inv[0] < min(capture) < max(capture) < inv[1]
    assert data['events'] == [{'sequence': 1}]


@pytest.mark.parametrize('status', ['invalidated', 'superseded'])
def test_invalidated_watch_keeps_events_and_skips_after(endpoint, status):
    run, replies, requests, _ = endpoint
    setup_inventory(replies)
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = {'schemaVersion': 1, 'generation': 1, 'data': {'captureId': 'own', 'active': False, 'status': status, 'events': [{'sequence': 1}]}}
    data = json.loads(run('watch', '--inventory').stdout)
    assert data['events'] == [{'sequence': 1}]
    assert data['inventoryContext']['complete'] is False
    assert len([b for k,b in requests if k == 'inspect' and b.get('kind') == 'inventory']) == 1

@pytest.mark.parametrize('items,count,truncated', [([], 0, False), ([{'formId': 'A', 'count': 4}], 2, True)])
def test_empty_and_truncated_preserve_native_metadata(endpoint, items, count, truncated):
    run, replies, _, _ = endpoint
    raw = {'owner': {'formId': '14'}, 'items': items, 'count': count, 'returned': len(items), 'truncated': truncated}
    setup_inventory(replies, raw)
    data = json.loads(run('effects', '--inventory', '--inventory-limit', '1').stdout)
    context = data['inventoryContext']
    assert context['complete'] is (not truncated)
    assert context['snapshots'][0]['raw'] == raw


@pytest.mark.parametrize('actor,resolved,owner,complete', [('00001234', '00001234', '00001234', True), ('MyActor', '00001234', '00001234', True), ('MyActor', None, '14', False), ('14', '15', '14', False)])
def test_requested_actor_is_never_replaced_with_player(endpoint, actor, resolved, owner, complete):
    run, replies, requests, _ = endpoint
    replies['inspect'] = lambda body: ({'owner': {'formId': owner}, 'count': 0, 'returned': 0, 'truncated': False, 'items': []} if body['kind'] == 'inventory' else {'actor': {'formId': resolved}, 'activeEffects': []})
    data = json.loads(run('effects', '--inventory', '--actor', actor).stdout)
    assert data['inventoryContext']['complete'] is complete
    assert [b['formId'] for k,b in requests if k == 'inspect'] == [actor, actor]


@pytest.mark.parametrize('change', [{'pid': 99}, {'exe': 'Other.exe'}, {'sessionId': 'new'}, {'vr': False}, {'pid': None}])
def test_health_changed_during_inventory_prevents_verified_context(endpoint, change):
    run, replies, _, _ = endpoint
    health = dict(replies['/api/health'])
    def inspect(body):
        if body['kind'] == 'inventory':
            replies['/api/health'] = dict(health, **change)
            return {'owner': {'formId': '14'}, 'count': 0, 'returned': 0, 'truncated': False, 'items': []}
        return {'activeEffects': []}
    replies['inspect'] = inspect
    data = json.loads(run('effects', '--inventory').stdout)
    assert data['ok'] is True and data['inventoryContext']['complete'] is False
    assert 'healthAfter' in data['inventoryContext']['snapshots'][0]


def test_inventory_timeout_keeps_effects_success(endpoint):
    import time
    run, replies, _, _ = endpoint
    def inspect(body):
        if body['kind'] == 'inventory': time.sleep(.3)
        return {'activeEffects': []}
    replies['inspect'] = inspect
    result = run('effects', '--inventory', '--timeout', '.1')
    data = json.loads(result.stdout)
    assert result.returncode == 0 and data['ok'] is True
    assert data['inventoryContext']['complete'] is False
    assert 'error' in data['inventoryContext']['snapshots'][0]


def test_no_inventory_flag_keeps_requests_and_fields(endpoint):
    run, replies, requests, _ = endpoint
    setup_inventory(replies)
    data = json.loads(run('effects').stdout)
    assert 'inventoryContext' not in data
    assert [k for k,b in requests] == ['/api/health', '/api/tools', 'inspect']


def test_watch_error_preserves_events_and_skips_terminal_inventory(endpoint):
    run, replies, requests, _ = endpoint
    setup_inventory(replies)
    replies['/api/tools']['tools'].append('probe.watch')
    def capture(body):
        if body.get('stop'): return {'ok': True}
        if body.get('read'): return {'error': 'failed read'}
        return {'schemaVersion': 1, 'data': {'captureId': 'own', 'active': False, 'hasMore': True, 'events': [{'sequence': 1}], 'nextSequence': 1}}
    replies['probe.watch'] = capture
    result = run('watch', '--inventory')
    data = json.loads(result.stdout)
    assert result.returncode == 2
    assert data['events'] == [{'sequence': 1}]
    assert data['inventoryContext']['complete'] is False
    assert len([b for k,b in requests if k == 'inspect' and b.get('kind') == 'inventory']) == 1


def test_unsupported_watch_skips_terminal_inventory(endpoint):
    run, replies, requests, _ = endpoint
    setup_inventory(replies)
    data = json.loads(run('watch', '--inventory').stdout)
    assert data['ok'] is True and data['inventoryContext']['complete'] is False
    assert len([b for k,b in requests if k == 'inspect' and b.get('kind') == 'inventory']) == 1


def test_cleanup_invalidated_watch_skips_terminal_inventory(endpoint):
    run, replies, requests, _ = endpoint
    setup_inventory(replies)
    replies['/api/tools']['tools'].append('probe.watch')
    def capture(body):
        return {'schemaVersion': 1, 'sessionId': 's', 'generation': 2 if body.get('stop') else 1, 'data': {'captureId': 'own', 'active': False, 'events': [{'sequence': 1}]}}
    replies['probe.watch'] = capture
    data = json.loads(run('watch', '--inventory').stdout)
    assert data['events'] == [{'sequence': 1}]
    assert data['inventoryContext']['complete'] is False
    assert len([b for k,b in requests if k == 'inspect' and b.get('kind') == 'inventory']) == 1

@pytest.mark.parametrize('change', [{'sessionId': 'other'}, {'generation': 2}, {'data': {'captureId': 'foreign', 'active': False, 'events': []}}])
def test_watch_identity_change_preserves_earlier_events(endpoint, change):
    run, replies, requests, _ = endpoint
    setup_inventory(replies)
    replies['/api/tools']['tools'].append('probe.watch')
    def capture(body):
        if body.get('stop'): return {'ok': True}
        row = {'schemaVersion': 1, 'sessionId': 's', 'generation': 1, 'data': {'captureId': 'own', 'active': False, 'events': [{'sequence': 1}], 'hasMore': True, 'nextSequence': 1}}
        if body.get('read'): row.update(change)
        return row
    replies['probe.watch'] = capture
    data = json.loads(run('watch', '--inventory').stdout)
    assert data['events'] == [{'sequence': 1}]
    assert data['inventoryContext']['complete'] is False
    assert len([b for k,b in requests if k == 'inspect' and b.get('kind') == 'inventory']) == 1


def test_changed_health_before_after_phase_skips_inventory_query(endpoint):
    run, replies, requests, _ = endpoint
    setup_inventory(replies)
    replies['/api/tools']['tools'].append('probe.watch')
    def capture(body):
        if body.get('stop'): replies['/api/health']['pid'] = 999
        return {'schemaVersion': 1, 'sessionId': 's', 'generation': 0, 'data': {'captureId': 'own', 'active': False, 'events': []}}
    replies['probe.watch'] = capture
    data = json.loads(run('watch', '--inventory').stdout)
    assert data['inventoryContext']['complete'] is False
    assert 'raw' not in data['inventoryContext']['snapshots'][-1]
    assert len([b for k,b in requests if k == 'inspect' and b.get('kind') == 'inventory']) == 1

@pytest.mark.parametrize('flags', [('near', '--inventory'), ('effects', '--inventory-limit', '500'), ('watch', '--inventory', '--inventory-limit', '-1')])
def test_invalid_flags_rejected_before_client_construction(monkeypatch, capsys, flags):
    from whats_here import cli
    def forbidden(*args, **kwargs):
        pytest.fail('invalid options constructed Client')
    monkeypatch.setattr(cli, 'Client', forbidden)
    assert cli.main([*flags, '--json']) == 2
    assert json.loads(capsys.readouterr().out)['ok'] is False


def test_failed_watch_text_still_shows_inventory_context(monkeypatch, capsys):
    from whats_here import cli
    monkeypatch.setattr(cli, 'Client', lambda *args: object())
    monkeypatch.setattr(cli, 'observe', lambda *args: {'ok': False, 'error': 'capture failed', 'snapshots': [], 'inventoryContext': {'complete': False, 'snapshots': []}, 'gaps': ['inventory: after skipped']})
    assert cli.main(['watch', '--inventory']) == 2
    text = capsys.readouterr().out
    assert 'capture failed' in text and 'Inventory context:' in text and 'inventory: after skipped' in text

@pytest.mark.parametrize('command', ['effects', 'watch'])
def test_large_json_integer_preserves_primary_observation(endpoint, command):
    run, replies, _, _ = endpoint
    raw = {'owner': {'formId': '14'}, 'count': 1, 'returned': 1, 'truncated': False, 'items': [{'formId': 'A', 'count': 1, 'value': 10**400, 'weight': 10**400}]}
    setup_inventory(replies, raw)
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = {'schemaVersion': 1, 'sessionId': 's', 'generation': 1, 'data': {'captureId': 'own', 'active': False, 'events': [{'sequence': 1}]}}
    result = run(command, '--inventory')
    assert result.returncode == 0, result.stderr + result.stdout
    data = json.loads(result.stdout)
    assert data['ok'] is True and data['native']['activeEffects'] == []
    if command == 'watch': assert data['events'] == [{'sequence': 1}]
    assert data['inventoryContext']['snapshots'][0]['raw']['items'][0]['value'] == 10**400


@pytest.mark.parametrize('phase', ['start', 'read', 'cleanup'])
@pytest.mark.parametrize('identity', [{}, {'sessionId': None, 'generation': None}, {'sessionId': '', 'generation': 1}, {'sessionId': 2, 'generation': 1}, {'sessionId': 's', 'generation': True}, {'sessionId': 's', 'generation': -1}, {'sessionId': 's', 'generation': '1'}])
def test_missing_or_malformed_probe_identity_skips_after_but_keeps_events(endpoint, phase, identity):
    run, replies, requests, _ = endpoint
    setup_inventory(replies)
    replies['/api/tools']['tools'].append('probe.watch')
    def capture(body):
        current = 'cleanup' if body.get('stop') else 'read' if body.get('read') else 'start'
        data = {'captureId': 'own', 'active': False, 'events': [{'sequence': 1}]}
        if phase == 'read' and current == 'start': data.update(hasMore=True, nextSequence=1)
        row = {'schemaVersion': 1, 'data': data}
        row.update(identity if current == phase or phase == 'start' else {'sessionId': 's', 'generation': 1})
        return row
    replies['probe.watch'] = capture
    data = json.loads(run('watch', '--inventory').stdout)
    assert data['events'] == [{'sequence': 1}]
    assert data['inventoryContext']['complete'] is False
    assert len([b for k,b in requests if k == 'inspect' and b.get('kind') == 'inventory']) == 1
