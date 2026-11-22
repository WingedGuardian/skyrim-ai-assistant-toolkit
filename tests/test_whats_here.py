"""Subprocess tests against a real loopback HTTP boundary; never a game."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

CLI = Path(__file__).resolve().parents[1] / 'tools/whats-here.py'
sys.path.insert(0, str(CLI.parent))

@pytest.fixture
def endpoint(tmp_path):
    requests = []
    replies = {'/api/health': {'pid': 4242, 'exe': 'SkyrimVR.exe', 'vr': True},
               '/api/tools': {'tools': ['inspect', 'forms']},
               'inspectmods': {'count': 1, 'lightCount': 0, 'total': 1, 'plugins': [{'index': 0, 'name': 'Origin.esm'}], 'lightPlugins': []},
               'inspect': {'refs': [{'formId': '00001234', 'name': 'Native rock'}]}}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def send(self, key, body=None):
            requests.append((key, body))
            value = replies.get('inspectmods' if key == 'inspect' and body and body.get('kind') == 'mods' else key, {'error': 'not found'})
            if callable(value): value = value(body)
            code = 404 if isinstance(value, dict) and value.get('error') == 'not found' else 200
            raw = json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try: self.wfile.write(raw)
            except OSError: pass
        def do_GET(self): self.send(self.path)
        def do_POST(self): self.send(self.path.rsplit('/', 1)[-1], json.loads(self.rfile.read(int(self.headers['Content-Length']))))
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = tmp_path / 'Data/SKSE/Plugins/devbench/runtime.json'
    runtime.parent.mkdir(parents=True)
    runtime.write_text(json.dumps({'port': server.server_port, 'pid': 4242, 'exe': 'SkyrimVR.exe'}))
    (tmp_path / 'SkyrimVR.exe').touch()
    def run(*args, timeout=8):
        env = dict(os.environ)
        env.pop('DEVBENCH_PORT', None)
        return subprocess.run([sys.executable, str(CLI), *args, '--game-dir', str(tmp_path), '--json'], capture_output=True, text=True, env=env, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    yield run, replies, requests, tmp_path
    server.shutdown()
    server.server_close()


def test_native_fallback_keeps_refs_without_claiming_gaze(endpoint):
    run, _, requests, _ = endpoint
    result = run('look')
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['native']['refs'][0]['name'] == 'Native rock'
    assert 'gaze unavailable' in data['gaps']
    assert data['probe']['status'] == 'unavailable'


def test_wrong_instance_is_rejected_before_inspection(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/health']['pid'] = 999
    result = run('near')
    assert result.returncode != 0
    assert 'instance' in json.loads(result.stdout)['error'].lower()
    assert not any(key == 'inspect' for key, _ in requests)


def test_http_timeout_is_an_error_not_empty_success(endpoint):
    run, replies, _, _ = endpoint
    def slow(body):
        time.sleep(.4)
        return {'refs': []}
    replies['inspect'] = slow
    result = run('near', '--timeout', '.1')
    assert result.returncode != 0
    assert 'error' in json.loads(result.stdout)


def test_probe_is_supplemental_and_keeps_native_name(endpoint):
    run, replies, _, _ = endpoint
    replies['/api/tools']['tools'].append('probe.near')
    replies['probe.near'] = {'schemaVersion': 1, 'ok': True, 'generation': 2, 'coverage': {'complete': False}, 'truncated': True, 'data': {'refs': [{'formId': '00001234', 'name': 'Probe name', 'shape': 'RockShape'}]}}
    result = run('near')
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['refs'][0]['native']['name'] == 'Native rock'
    assert data['refs'][0]['probe']['shape'] == 'RockShape'
    assert data['probe']['truncated'] is True


def test_ref_uses_native_forms_describe_for_provenance(endpoint):
    run, replies, requests, _ = endpoint
    replies['forms'] = {'formId': '00001234', 'plugin': 'Rock.esm', 'localId': '001234'}
    result = run('ref', '00001234')
    assert result.returncode == 0
    assert json.loads(result.stdout)['forms']['00001234']['plugin'] == 'Rock.esm'
    assert ('forms', {'action': 'describe', 'formId': '00001234'}) in requests


def test_asset_absence_requires_complete_coverage():
    sys.path.insert(0, str(CLI.parent))
    from whats_here.enrichment import classify_assets
    result = classify_assets(['textures/a.dds', 'textures/b.dds', 'textures/c.dds'],
        {'absence_conclusive': False, 'context': {'archive_coverage': {'complete': False}},
         'result': {'content': [{'type': 'text', 'text': json.dumps({'results': [
             {'path': 'textures/a.dds', 'exists': False}, {'path': 'textures/b.dds', 'exists': True, 'providers': ['A.bsa']}]})}]}})
    assert [row['status'] for row in result['assets']] == ['inconclusive', 'present', 'not checked']
    assert result['assets'][1]['providerMeaning'] == 'candidate; runtime winner not established'


def test_watch_expiry_deduplicates_events_and_stops_owned_capture(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'own', 'status': 'stopped'}}
        return {'schemaVersion': 1, 'ok': True, 'generation': 1, 'data': {'captureId': 'own', 'active': False, 'status': 'expired', 'events': [{'sequence': 1, 'sourceFormId': 'ABC'}, {'sequence': 1, 'sourceFormId': 'ABC'}], 'nextSequence': 2, 'cursorLost': True, 'dropped': 4}}
    replies['probe.watch'] = watch
    result = run('watch', '--seconds', '.01')
    assert result.returncode == 3, result.stdout     # cursorLost: observed, but incomplete
    data = json.loads(result.stdout)
    assert len(data['events']) == 1
    assert data['summary'] == {'ABC': 1}
    assert 'watch cursor lost events' in data['gaps']
    calls = [body for key, body in requests if key == 'probe.watch']
    assert len([b for b in calls if not b.get('read') and not b.get('stop')]) == 1
    assert calls[-1] == {'stop': True, 'captureId': 'own'}


def test_watch_start_timeout_never_retries_or_stops_unknown_capture(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def slow(body):
        time.sleep(.4)
        return {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'unknown'}}
    replies['probe.watch'] = slow
    result = run('watch', '--timeout', '.1')
    assert result.returncode != 0
    assert len([b for key, b in requests if key == 'probe.watch']) == 1


def test_watch_invalidation_stops_polling_and_reports_gap(endpoint):
    run, replies, _, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = {'schemaVersion': 1, 'ok': True, 'generation': 9, 'data': {'captureId': 'own', 'active': False, 'status': 'invalidated', 'events': []}}
    result = run('watch', '--seconds', '.01')
    assert 'watch invalidated' in json.loads(result.stdout)['gaps']


def test_check_maps_plugin_local_identity_and_preserves_runtime_chain(endpoint):
    run, replies, _, root = endpoint
    replies['forms'] = {'formId': '00001234', 'plugin': 'Origin.esm', 'localId': '001234', 'sourceFiles': ['Origin.esm', 'Override.esp']}
    script = root / 'tools/skyrim-winner.sh'
    script.parent.mkdir()
    script.write_text('#!/bin/sh\nprintf "   0. Origin.esm  [defines]\\n   1. Other.esp  [overrides]   <-- WINS\\n"\nprintf "%s|%s|%s" "$1" "$2" "$3" > "' + (root / 'called.txt').as_posix() + '"\n')
    result = run('ref', '00001234', '--check')
    assert result.returncode == 0, result.stdout
    data = json.loads(result.stdout)
    assert data['forms']['00001234']['sourceFiles'] == ['Origin.esm', 'Override.esp']
    assert 'Other.esp' in data['checks'][0]['offlineOutput']
    assert data['checks'][0]['comparison'] == 'not automatically reconciled; inspect both sources'
    assert (root / 'called.txt').read_text() == 'chain|001234|Origin.esm'


def test_check_never_guesses_runtime_plugin_identity(endpoint):
    run, replies, _, _ = endpoint
    replies['forms'] = {'formId': 'FF001234', 'name': 'Dynamic'}
    result = run('ref', 'FF001234', '--check')
    assert result.returncode == 3      # the requested check could not be done: observed, but incomplete
    assert json.loads(result.stdout)['checks'][0]['status'] == 'not checked'


def test_text_output_labels_native_records_and_gaps(endpoint):
    _, replies, _, root = endpoint
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'look', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert 'Native rock' in result.stdout
    assert 'Gap: gaze unavailable' in result.stdout
    assert not result.stdout.lstrip().startswith('{')


def test_offline_nif_extraction_preserves_shape_and_unanswered_meshes():
    sys.path.insert(0, str(CLI.parent))
    from whats_here.enrichment import nif_textures
    found = nif_textures("meshes/a.nif\n  version: 20\n  'Shape':\n    tex[0] (diffuse): actors/a.dds\n", ['meshes/a.nif', 'meshes/b.nif'])
    assert found['uses'] == [{'mesh': 'meshes/a.nif', 'shape': 'Shape', 'path': 'textures/actors/a.dds', 'source': 'offline NIF; runtime binding not established'}]
    assert found['unanswered'] == ['meshes/b.nif']


def test_absent_asset_complete_vs_archive_read_failure():
    sys.path.insert(0, str(CLI.parent))
    from whats_here.enrichment import classify_assets
    for incomplete, expected in [(False, 'missing'), (True, 'inconclusive')]:
        envelope = {'absence_conclusive': True, 'result': {'content': [{'type': 'text', 'text': json.dumps({'read_incomplete': incomplete, 'results': [{'path': 'textures/a.dds', 'exists': False}]})}]}}
        assert classify_assets(['textures/a.dds'], envelope)['assets'][0]['status'] == expected


def test_probe_only_candidate_gets_native_reference_details(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.near')
    replies['probe.near'] = {'schemaVersion': 1, 'ok': True, 'data': {'refs': [{'formId': '00009999', 'shape': 'Far origin'}]}}
    def refs(body):
        if body.get('formId') == '00009999': return {'refs': [{'formId': '00009999', 'name': 'Distant native mesh'}]}
        return {'refs': []}
    replies['inspect'] = refs
    result = run('near')
    assert json.loads(result.stdout)['refs'][0]['native']['name'] == 'Distant native mesh'


def test_watch_generation_change_is_not_joined_across_loads(endpoint):
    run, replies, _, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return {'ok': True}
        if body.get('read'): return {'schemaVersion': 1, 'ok': True, 'generation': 2, 'data': {'captureId': 'own', 'events': [{'sequence': 1, 'sourceFormId': 'STALE'}]}}
        return {'schemaVersion': 1, 'ok': True, 'generation': 1, 'data': {'captureId': 'own', 'active': True, 'events': []}}
    replies['probe.watch'] = watch
    result = run('watch', '--seconds', '.01')
    data = json.loads(result.stdout)
    assert data['events'] == []
    assert 'watch generation changed; restart or load' in data['gaps']


def test_watch_keyboard_interrupt_stops_only_capture_it_started():
    from argparse import Namespace
    sys.path.insert(0, str(CLI.parent))
    from whats_here.watch import watch
    class Client:
        calls = []
        def tools(self): return {'probe.watch'}
        def tool(self, name, body):
            self.calls.append(body)
            if body.get('stop'): return {'ok': True}
            if body.get('read'): raise KeyboardInterrupt()
            return {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'mine', 'active': True}}
    client = Client()
    result = watch(client, Namespace(seconds=.001, actor='0x14'))
    assert result['gaps'] == ['watch interrupted']
    assert client.calls[-1] == {'stop': True, 'captureId': 'mine'}


def test_invalid_caps_fail_before_connecting(endpoint):
    run, _, requests, _ = endpoint
    result = run('near', '--limit', '101')
    assert result.returncode == 2
    assert requests == []


def test_asset_checks_are_deduplicated_and_use_only_gateway(endpoint):
    run, replies, _, root = endpoint
    # External boundary fixture implements the public gateway CLI, no backend bypass.
    gateway = root / 'tools/housecarl.py'
    gateway.parent.mkdir()
    gateway.write_text('import json,sys\nfrom pathlib import Path\na=json.loads(Path(sys.argv[-1]).read_text())\nPath(__file__).with_name("asked.json").write_text(json.dumps(a))\nprint(json.dumps({"absence_conclusive":False,"result":{"content":[{"type":"text","text":json.dumps({"results":[{"path":"textures/a.dds","exists":False}]})}]}}))\n')
    replies['inspect'] = {'refs': [{'formId': '00001234', 'texture': 'textures/a.dds', 'other': 'TEXTURES/A.DDS'}]}
    replies['forms'] = {'formId': '00001234'}
    result = run('near', '--textures')
    data = json.loads(result.stdout)
    assert len(data['assets']['assets']) == 1
    assert data['assets']['assets'][0]['status'] == 'inconclusive'
    assert json.loads((root / 'tools/asked.json').read_text())['asset_paths'] == ['TEXTURES/A.DDS']


def test_watch_without_probe_still_reports_native_effects(endpoint):
    run, replies, requests, _ = endpoint
    replies['inspect'] = {'activeEffects': [{'spell': {'formId': '0000ABCD', 'name': 'Blood'}}]}
    result = run('watch', '--seconds', '.01')
    assert json.loads(result.stdout)['native']['activeEffects'][0]['spell']['name'] == 'Blood'
    assert ('inspect', {'kind': 'effects', 'formId': '0x14'}) in requests


def test_near_types_passes_native_form_type_and_xyz_is_explicit(endpoint):
    run, _, requests, _ = endpoint
    result = run('near', '--types', 'Static', '--at', '1,2,3')
    assert result.returncode == 0
    assert any(body.get('formType') == 'Static' for key, body in requests if key == 'inspect')
    assert 'native refs are centered on player; requested xyz needs probe' in json.loads(result.stdout)['gaps']


def test_effect_checks_describe_spell_identity(endpoint):
    run, replies, _, _ = endpoint
    replies['inspect'] = {'activeEffects': [{'spell': {'formId': '0000ABCD'}, 'effect': {'formId': '0000ABCE'}}]}
    replies['forms'] = {'plugin': 'Effects.esm', 'localId': '0x00ABCD'}
    result = run('effects', '--check')
    assert set(json.loads(result.stdout)['forms']) == {'0000ABCD', '0000ABCE'}


def test_invalid_xyz_fails_before_connecting(endpoint):
    run, _, requests, _ = endpoint
    result = run('near', '--at', '1,2,NaN')
    assert result.returncode == 2
    assert requests == []


def test_native_watch_start_uses_integer_seconds(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'own', 'active': False, 'status': 'expired'}}
    run('watch', '--seconds', '.01')
    start = next(body for key, body in requests if key == 'probe.watch')
    assert start['seconds'] == 1
    assert isinstance(start['seconds'], int)


def test_native_relative_model_gets_meshes_prefix_before_gateway(endpoint):
    run, replies, _, root = endpoint
    gateway = root / 'tools/housecarl.py'
    gateway.parent.mkdir()
    gateway.write_text('import json,sys\nfrom pathlib import Path\na=json.loads(Path(sys.argv[-1]).read_text())\nwith Path(__file__).with_name("asked.jsonl").open("a") as f:f.write(json.dumps(a)+"\\n")\nprint(json.dumps({"absence_conclusive":False,"result":{"content":[{"type":"text","text":"meshes/rocks/a.nif\\n  version: 20\\n" if "mesh_paths" in a else json.dumps({"results":[]})}]}}))\n')
    replies['inspect'] = {'refs': [{'formId': '00001234', 'model': 'rocks\\a.nif'}]}
    replies['forms'] = {'formId': '00001234'}
    result = run('near', '--textures')
    assert result.returncode == 0
    asked = [json.loads(line) for line in (root / 'tools/asked.jsonl').read_text().splitlines()]
    assert asked[0]['mesh_paths'] == ['meshes/rocks/a.nif']


def test_text_output_reports_partial_probe_coverage(endpoint):
    _, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.look')
    replies['probe.look'] = {'schemaVersion': 1, 'ok': True, 'coverage': {'gaze': 'unavailable', 'bounds': 'not checked'}, 'data': {'refs': []}}
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'look', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert 'gaze' in result.stdout
    assert 'unavailable' in result.stdout
    assert 'not checked' in result.stdout


def test_watch_text_streams_events_before_capture_ends(endpoint):
    _, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return {'ok': True}
        return {'schemaVersion': 1, 'ok': True, 'generation': 1, 'data': {'captureId': 'own', 'active': True, 'events': [{'sequence': 1, 'sourceFormId': 'VISIBLE'}]}}
    replies['probe.watch'] = watch
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    process = subprocess.Popen([sys.executable, str(CLI), 'watch', '--seconds', '1', '--game-dir', str(root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    line = process.stdout.readline()
    assert 'VISIBLE' in line
    assert process.poll() is None
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert 'Sources:' in stdout


def test_slow_drip_body_exceeds_total_request_deadline():
    import socket
    sys.path.insert(0, str(CLI.parent))
    from whats_here.transport import Client, ObservationError
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    def serve():
        peer, _ = listener.accept()
        with peer:
            peer.recv(4096)
            peer.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\n')
            for byte in b'{"a":1}':
                try: peer.sendall(bytes([byte]))
                except OSError: break
                time.sleep(.03)
    worker = threading.Thread(target=serve)
    worker.start()
    client = object.__new__(Client)
    client.port, client.timeout = listener.getsockname()[1], .05
    import urllib.request
    client.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # Constructor bypass isolates this real transport request from unrelated identity checks.
    started = time.monotonic()
    try:
        with pytest.raises(ObservationError): client.request('/api/health')
        assert time.monotonic() - started < .15
    finally:
        worker.join(timeout=1)
        listener.close()
    assert not worker.is_alive()


def test_distant_candidate_and_forms_models_reach_gateway(endpoint):
    run, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.near')
    replies['probe.near'] = {'schemaVersion': 1, 'ok': True, 'data': {'refs': [{'formId': '00009999'}]}}
    replies['inspect'] = lambda body: {'refs': [{'formId': '00009999', 'model': 'rocks/distant.nif'}]} if body.get('formId') else {'refs': []}
    replies['forms'] = {'formId': '00009999', 'maleModel': 'rocks/form.nif'}
    gateway = root / 'tools/housecarl.py'
    gateway.parent.mkdir()
    gateway.write_text('import json,sys\nfrom pathlib import Path\na=json.loads(Path(sys.argv[-1]).read_text())\nwith Path(__file__).with_name("asked.jsonl").open("a") as f:f.write(json.dumps(a)+"\\n")\nprint(json.dumps({"absence_conclusive":False,"result":{"content":[{"type":"text","text":"" if "mesh_paths" in a else json.dumps({"results":[]})}]}}))\n')
    result = run('near', '--textures')
    assert result.returncode == 0, result.stdout
    asked = [json.loads(line) for line in (root / 'tools/asked.jsonl').read_text().splitlines()]
    assert set(asked[0]['mesh_paths']) == {'meshes/rocks/distant.nif', 'meshes/rocks/form.nif'}


def test_watch_text_reports_capture_coverage_truncation_and_drops(endpoint):
    _, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = {'schemaVersion': 1, 'ok': True, 'coverage': {'hits': 'unavailable'}, 'truncated': True, 'data': {'captureId': 'own', 'active': False, 'dropped': 7}}
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'watch', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert 'hits' in result.stdout and 'unavailable' in result.stdout
    assert 'truncated' in result.stdout
    assert 'Ring retention evictions: 7' in result.stdout


def test_effects_text_reports_probe_attached_visual_and_attribution(endpoint):
    _, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.effects')
    replies['probe.effects'] = {'schemaVersion': 1, 'ok': True, 'data': {'visuals': [{'kind': 'effectShader', 'form': {'formId': '0000FACE'}, 'attribution': 'unknown'}]}}
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'effects', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert 'effectShader' in result.stdout and '0000FACE' in result.stdout
    assert 'unknown' in result.stdout


def test_fallback_without_runtime_rejects_other_game_root(endpoint, monkeypatch):
    _, replies, requests, root = endpoint
    runtime = root / 'Data/SKSE/Plugins/devbench/runtime.json'
    port = json.loads(runtime.read_text())['port']
    runtime.unlink()
    replies['/api/health']['exe'] = str(root.parent / 'OtherGame/SkyrimVR.exe')
    env = dict(os.environ, DEVBENCH_PORT=str(port))
    result = subprocess.run([sys.executable, str(CLI), 'near', '--game-dir', str(root), '--json'], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 2
    assert 'instance' in json.loads(result.stdout)['error'].lower()
    assert not any(key == 'inspect' for key, _ in requests)


def test_text_labels_native_only_origin_candidates(endpoint):
    _, _, _, root = endpoint
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'look', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert '[native origin candidate; gaze not established]' in result.stdout


def test_slow_drip_headers_exceed_total_request_deadline():
    import socket
    sys.path.insert(0, str(CLI.parent))
    from whats_here.transport import Client, ObservationError
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    def serve():
        peer, _ = listener.accept()
        with peer:
            peer.recv(4096)
            for byte in b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}':
                try: peer.sendall(bytes([byte]))
                except OSError: break
                time.sleep(.01)
    worker = threading.Thread(target=serve)
    worker.start()
    client = object.__new__(Client)
    client.port, client.timeout = listener.getsockname()[1], .05
    started = time.monotonic()
    try:
        with pytest.raises(ObservationError): client.request('/api/health')
        assert time.monotonic() - started < .15
    finally:
        worker.join(timeout=1)
        listener.close()
    assert not worker.is_alive()


def test_os_executable_query_returns_this_process_image():
    sys.path.insert(0, str(CLI.parent))
    from whats_here.transport import process_executable
    assert process_executable(os.getpid()) == Path(sys.executable).resolve()


def test_fallback_basename_queries_pid_image_and_rejects_unverifiable(endpoint, monkeypatch):
    _, replies, _, root = endpoint
    from whats_here import transport
    runtime = root / 'Data/SKSE/Plugins/devbench/runtime.json'
    port = json.loads(runtime.read_text())['port']
    runtime.unlink()
    monkeypatch.setenv('DEVBENCH_PORT', str(port))
    observed = []
    def image(pid):
        observed.append(pid)
        raise OSError('inaccessible process')
    monkeypatch.setattr(transport, 'process_executable', image)
    with pytest.raises(transport.ObservationError, match='instance executable could not be verified'):
        transport.Client(root, 1)
    assert observed == [4242]


def test_fallback_pid_image_must_match_selected_root(endpoint, monkeypatch):
    _, replies, requests, root = endpoint
    from whats_here import transport
    runtime = root / 'Data/SKSE/Plugins/devbench/runtime.json'
    port = json.loads(runtime.read_text())['port']
    runtime.unlink()
    monkeypatch.setenv('DEVBENCH_PORT', str(port))
    monkeypatch.setattr(transport, 'process_executable', lambda pid: root.parent / 'foreign/SkyrimVR.exe')
    with pytest.raises(transport.ObservationError, match='another game root'):
        transport.Client(root, 1)
    assert not any(key == 'inspect' for key, _ in requests)
    monkeypatch.setattr(transport, 'process_executable', lambda pid: root / 'SkyrimVR.exe')
    client = transport.Client(root, 1)
    assert client.identity['pid'] == 4242


def test_watch_restart_session_id_prevents_joining_same_generation(endpoint):
    run, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return {'ok': True}
        return {'schemaVersion': 1, 'ok': True, 'generation': 1, 'sessionId': 'new' if body.get('read') else 'old', 'data': {'captureId': 'own', 'active': True, 'events': [{'sequence': 1, 'sourceFormId': 'STALE'}] if body.get('read') else []}}
    replies['probe.watch'] = watch
    result = run('watch', '--seconds', '.01')
    data = json.loads(result.stdout)
    assert data['events'] == []
    assert 'watch process session changed' in data['gaps']


def test_watch_text_reports_native_event_queue_drops(endpoint):
    _, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'own', 'active': False, 'eventQueueDropped': 5, 'visualSnapshotComplete': False}}
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'watch', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert 'native event queue dropped: 5' in result.stdout
    assert 'visual snapshot incomplete' in result.stdout


def test_default_near_omits_empty_types_for_native_filter_contract(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.near')
    # Reproduce native presence-based type filtering: an empty filter has no
    # matching exact base type; omitting it includes the reference.
    def native_near(body):
        rows = [] if 'types' in body and body['types'] != 'Static' else [{'formId': '00001234', 'shape': 'Rock'}]
        return {'schemaVersion': 1, 'ok': True, 'data': {'refs': rows}}
    replies['probe.near'] = native_near
    data = json.loads(run('near').stdout)
    assert data['refs'][0]['probe']['shape'] == 'Rock'
    assert 'types' not in next(body for key, body in requests if key == 'probe.near')
    data = json.loads(run('near', '--types', 'Static').stdout)
    assert data['refs'][0]['probe']['shape'] == 'Rock'


def test_ref_text_exposes_runtime_provenance_and_shape_texture_uses(endpoint):
    _, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.ref')
    replies['forms'] = {'formId': '00001234', 'plugin': 'Origin.esm', 'localId': '0x001234'}
    replies['probe.ref'] = {'schemaVersion': 1, 'ok': True, 'data': {'ref': {'formId': '00001234', 'baseFormId': '00001235', 'runtimeSourceFiles': ['Origin.esm', 'Override.esp'], 'base': {'formId': '00001235', 'runtimeSourceFiles': ['Base.esm']}, 'mesh': 'meshes/rock.nif', 'loaded3D': True, 'disabled': False, 'shapes': [{'shape': 'RockFace', 'shaderType': 'Lighting', 'textures': [{'slot': 0, 'path': 'textures/rock.dds', 'source': 'runtime lighting texture set'}]}]}}}
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'ref', '00001234', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0
    assert 'Reference runtime files: Origin.esm -> Override.esp' in result.stdout
    assert 'Base 00001235 runtime files: Base.esm' in result.stdout
    assert 'Shape RockFace' in result.stdout
    assert 'slot 0: textures/rock.dds' in result.stdout
    assert 'runtime lighting texture set' in result.stdout
    assert 'Mesh: meshes/rock.nif' in result.stdout


def test_near_textures_text_preserves_shape_to_asset_use(endpoint):
    _, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.near')
    replies['probe.near'] = {'schemaVersion': 1, 'ok': True, 'data': {'refs': [{'formId': '00001234', 'shapes': [{'shape': 'WallPanel', 'textures': [{'slot': 1, 'path': 'textures/wall_n.dds', 'source': 'loaded source texture'}]}]}]}}
    replies['forms'] = {'formId': '00001234'}
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'near', '--textures', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0
    assert 'Shape WallPanel' in result.stdout
    assert 'slot 1: textures/wall_n.dds' in result.stdout


def test_watch_restart_with_different_capture_never_stops_old_id_in_new_session(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return {'ok': True}
        return {'schemaVersion': 1, 'ok': True, 'generation': 1, 'sessionId': 'new' if body.get('read') else 'old', 'data': {'captureId': 'foreign' if body.get('read') else 'own', 'active': True, 'events': []}}
    replies['probe.watch'] = watch
    result = run('watch', '--seconds', '.01')
    assert 'watch process session changed' in json.loads(result.stdout)['gaps']
    assert not any(body.get('stop') for key, body in requests if key == 'probe.watch')


def test_watch_drains_terminal_capture_pages_immediately(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return {'ok': True}
        after = body.get('after', 0)
        stop = 50 if after == 0 else 75
        events = [{'sequence': n, 'sourceFormId': 'SOURCE'} for n in range(after + 1, stop + 1)]
        return {'schemaVersion': 1, 'ok': True, 'sessionId': 'same', 'generation': 1,
                'data': {'captureId': 'own', 'active': False, 'status': 'expired', 'events': events,
                         'nextSequence': stop, 'latestSequence': 75, 'hasMore': stop < 75}}
    replies['probe.watch'] = watch
    started = time.monotonic()
    result = run('watch', '--seconds', '.01')
    data = json.loads(result.stdout)
    assert len(data['events']) == 75
    assert data['summary'] == {'SOURCE': 75}
    assert time.monotonic() - started < 2
    reads = [body for key, body in requests if key == 'probe.watch' and body.get('read')]
    assert reads == [{'read': True, 'captureId': 'own', 'after': 50}]


def test_watch_paging_without_cursor_progress_stops_instead_of_spinning(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'own', 'active': False, 'status': 'expired', 'events': [], 'nextSequence': 0, 'latestSequence': 10, 'hasMore': True}}
    result = run('watch', '--seconds', '.01')
    assert 'watch pagination made no progress' in json.loads(result.stdout)['gaps']
    assert len([body for key, body in requests if key == 'probe.watch' and body.get('read')]) == 0


def test_watch_stops_at_deadline_then_keeps_terminal_stop_events(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        after = body.get('after', 0)
        if body.get('read'): time.sleep(.02)
        return {'schemaVersion': 1, 'ok': True, 'sessionId': 'same', 'generation': 1,
                'data': {'captureId': 'own', 'active': not body.get('stop'), 'status': 'stopped' if body.get('stop') else 'active',
                         'events': [{'sequence': after + 1, 'sourceFormId': 'FINAL' if body.get('stop') else 'LIVE'}],
                         'nextSequence': after + 1, 'latestSequence': after + 1, 'hasMore': not body.get('stop')}}
    replies['probe.watch'] = watch
    # 1.5 s, not 0.01: under load the start reply alone can outlast a short watch, which then stops before its
    # first read and sends no cursor (valid: the plugin reads from 0), so this test would see no `after`. 0.3 s still
    # failed on a GitHub Windows runner (CI 45045244154).
    data = json.loads(run('watch', '--seconds', '1.5').stdout)
    stops = [body for key, body in requests if key == 'probe.watch' and body.get('stop')]
    assert len(stops) == 1
    assert 'after' in stops[0]
    assert data['events'][-1]['sourceFormId'] == 'FINAL'


def test_watch_terminal_paging_has_small_explicit_page_bound():
    from argparse import Namespace
    from whats_here.watch import watch
    class Client:
        timeout = 5
        calls = []
        def tools(self): return {'probe.watch'}
        def tool(self, name, body):
            self.calls.append(body)
            if body.get('stop'): return {'ok': True}
            n = body.get('after', 0) + 1
            return {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'own', 'active': False, 'events': [{'sequence': n}], 'nextSequence': n, 'hasMore': True}}
    client = Client()
    data = watch(client, Namespace(seconds=1, actor='0x14'))
    assert len(client.calls) <= 43
    assert 'watch pagination page budget reached; events remain' in data['gaps']


def test_watch_terminal_drain_has_wall_budget_and_clips_each_http_timeout(monkeypatch):
    from argparse import Namespace
    from whats_here import watch as module
    clock = [0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    class Client:
        timeout = 60
        read_timeouts = []
        def tools(self): return {'probe.watch'}
        def tool(self, name, body):
            if body.get('stop'): return {'ok': True}
            if body.get('read'):
                self.read_timeouts.append(self.timeout)
                clock[0] += 2
            n = body.get('after', 0) + 1
            return {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'own', 'active': False, 'events': [{'sequence': n}], 'nextSequence': n, 'hasMore': True}}
    client = Client()
    data = module.watch(client, Namespace(seconds=1, actor='0x14'))
    assert len(client.read_timeouts) <= 3
    assert client.read_timeouts == [5, 3, 1]
    assert client.timeout == 60
    assert 'watch final drain deadline reached; events remain' in data['gaps']


def test_watch_summary_keeps_copied_source_identity_without_extra_requests(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    source = {'formId': '0000ABCD', 'name': 'Blood', 'runtimeSourceFiles': ['Effects.esp']}
    replies['probe.watch'] = {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'own', 'active': False, 'events': [{'sequence': 1, 'sourceFormId': '0000ABCD', 'sourceKind': 'magicEffect', 'source': source}]}}
    data = json.loads(run('watch', '--seconds', '.01').stdout)
    assert data['sources']['0000ABCD'] == [{'sourceKind': 'magicEffect', 'source': source}]
    assert not any(key == 'forms' for key, _ in requests)


def test_watch_drain_transport_failure_preserves_partial_events_with_error():
    from argparse import Namespace
    from whats_here.watch import watch
    from whats_here.transport import ObservationError
    class Client:
        timeout = 5
        def tools(self): return {'probe.watch'}
        def tool(self, name, body):
            if body.get('stop'): return {'ok': True}
            if body.get('read'): raise ObservationError('total deadline exceeded')
            return {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'own', 'active': False, 'events': [{'sequence': 1}], 'nextSequence': 1, 'hasMore': True}}
    data = watch(Client(), Namespace(seconds=1, actor='0x14'))
    assert data['ok'] is False
    assert data['events'] == [{'sequence': 1}]
    assert data['error'] == 'total deadline exceeded'


def test_watch_terminal_pending_event_is_collected_after_it_settles(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return {'ok': True}
        read = body.get('read', False)
        return {'schemaVersion': 1, 'ok': True, 'sessionId': 'same', 'generation': 1,
                'data': {'captureId': 'own', 'active': False, 'status': 'expired', 'hasMore': False,
                         'pendingEvents': 0 if read else 1, 'nextSequence': 1 if read else 0,
                         'events': [{'sequence': 1, 'sourceFormId': 'LATE'}] if read else []}}
    replies['probe.watch'] = watch
    result = run('watch', '--seconds', '.01')
    data = json.loads(result.stdout)
    assert data['events'] == [{'sequence': 1, 'sourceFormId': 'LATE'}]
    assert data['pendingEvents'] == 0
    assert len([body for key, body in requests if key == 'probe.watch' and body.get('read')]) == 1


def test_watch_pending_never_settles_is_paced_bounded_and_explicit(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'own', 'active': False, 'hasMore': False, 'pendingEvents': 1, 'nextSequence': 0, 'events': []}}
    started = time.monotonic()
    result = run('watch', '--seconds', '.01', timeout=8)
    elapsed = time.monotonic() - started
    data = json.loads(result.stdout)
    assert data['incomplete'] is True
    assert data['pendingEvents'] == 1
    assert 'watch final drain deadline reached; pending events remain' in data['gaps']
    reads = [body for key, body in requests if key == 'probe.watch' and body.get('read')]
    assert 1 <= len(reads) <= 101
    assert 4.8 <= elapsed < 7


def test_watch_text_exposes_capture_failures_and_global_boundary_delta(endpoint):
    _, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return {'ok': True}
        read = body.get('read', False)
        return {'schemaVersion': 1, 'ok': True, 'eventBoundaryFailures': 104 if read else 100,
                'data': {'captureId': 'own', 'active': False, 'hasMore': not read, 'nextSequence': 1 if not read else 2,
                         'events': [{'sequence': 2 if read else 1}], 'eventFailures': 3, 'visualEventsDropped': 7, 'visualStatus': 'unavailable'}}
    replies['probe.watch'] = watch
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'watch', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert 'capture event failures: 3' in result.stdout
    assert 'visual events dropped: 7' in result.stdout
    assert 'Visual status: unavailable' in result.stdout
    assert 'Global event boundary failures since start (all captures): 4' in result.stdout
    assert 'since start (all captures): 104' not in result.stdout


def test_effects_text_exposes_supplemental_collection_coverage(endpoint):
    _, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.effects')
    replies['probe.effects'] = {'schemaVersion': 1, 'ok': True, 'data': {'effects': [], 'visuals': [], 'effectsCoverage': {'status': 'unavailable', 'reason': 'no MagicTarget'}, 'visualsCoverage': {'status': 'incomplete', 'reason': 'budget'}}}
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'effects', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert 'effectsCoverage' in result.stdout and 'no MagicTarget' in result.stdout
    assert 'visualsCoverage' in result.stdout and 'incomplete' in result.stdout


@pytest.mark.parametrize('loaded', [[], [
    {'slot': 'source', 'path': 'textures/fallback.dds', 'source': 'loaded source texture'},
    {'slot': 'greyscale', 'path': 'textures/loaded-grey.dds', 'source': 'loaded source texture'}]])
def test_effect_assigned_paths_are_checked_with_null_or_different_loaded_names(endpoint, loaded):
    run, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.ref')
    assigned = [
        {'slot': 'source', 'path': 'textures/requested.dds', 'source': 'runtime effect material path'},
        {'slot': 'greyscale', 'path': 'textures/requested-grey.dds', 'source': 'runtime effect material path'}]
    replies['probe.ref'] = {'schemaVersion': 1, 'ok': True, 'data': {'ref': {
        'formId': '00001234', 'shapes': [{'shape': 'EffectShape', 'textures': assigned + loaded}]}}}
    replies['forms'] = {'formId': '00001234'}
    gateway = root / 'tools/housecarl.py'
    gateway.parent.mkdir()
    gateway.write_text("""import json,sys
from pathlib import Path
a=json.loads(Path(sys.argv[-1]).read_text())
Path(__file__).with_name('asked.json').write_text(json.dumps(a))
rows=[{'path':p,'exists':'requested' not in p} for p in a['asset_paths']]
print(json.dumps({'absence_conclusive':True,'result':{'content':[{'type':'text','text':json.dumps({'results':rows})}]}}))
""")
    result = run('ref', '00001234', '--textures')
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['refs'][0]['probe']['shapes'][0]['textures'] == assigned + loaded
    expected = [row['path'] for row in assigned + loaded]
    assert json.loads((root / 'tools/asked.json').read_text())['asset_paths'] == expected
    assert {row['path']: row['status'] for row in data['assets']['assets']} == {
        row['path']: 'missing' if row in assigned else 'present' for row in assigned + loaded}


@pytest.mark.parametrize('failure,detail', [('refused', 'fixture NIF refusal'),
                                           ('malformed', 'Expecting value')])
def test_text_reports_offline_nif_failure_after_successful_asset_check(endpoint, failure, detail):
    _, replies, _, root = endpoint
    replies['inspect'] = {'refs': [{'formId': '00001234', 'model': 'meshes/effect.nif'}]}
    replies['forms'] = {'formId': '00001234'}
    gateway = root / 'tools/housecarl.py'
    gateway.parent.mkdir()
    gateway.write_text("""import json,sys
from pathlib import Path
a=json.loads(Path(sys.argv[-1]).read_text())
with Path(__file__).with_name('asked.jsonl').open('a') as f:f.write(json.dumps(a)+'\\n')
if 'mesh_paths' in a:
    if FAILURE == 'refused':
        print('fixture NIF refusal',file=sys.stderr)
        sys.exit(2)
    print('malformed response')
else:
    rows=[{'path':p,'exists':True} for p in a['asset_paths']]
    print(json.dumps({'absence_conclusive':True,'result':{'content':[{'type':'text','text':json.dumps({'results':rows})}]}}))
""".replace('FAILURE', repr(failure)))
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'near', '--textures',
                             '--game-dir', str(root)], capture_output=True, text=True, env=env, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    asked = [json.loads(line) for line in (root / 'tools/asked.jsonl').read_text().splitlines()]
    assert len(asked) == 2 and asked[0]['mesh_paths'] == ['meshes/effect.nif']
    assert asked[1]['asset_paths'] == ['meshes/effect.nif']
    assert 'Asset present: meshes/effect.nif' in result.stdout
    assert 'Offline NIF status: not checked' in result.stdout
    assert 'Gap: offline NIF inspection: ' in result.stdout and detail in result.stdout
    assert 'Gap: offline NIF not checked: meshes/effect.nif' in result.stdout


@pytest.mark.parametrize('asset_exists,expected_status', [(True, 'present'), (False, 'inconclusive')])
def test_text_does_not_promote_incomplete_nif_absence_to_confirmed_missing(endpoint, asset_exists, expected_status):
    _, replies, _, root = endpoint
    replies['inspect'] = {'refs': [{'formId': '00001234', 'model': 'meshes/effect.nif'}]}
    replies['forms'] = {'formId': '00001234'}
    gateway = root / 'tools/housecarl.py'
    gateway.parent.mkdir()
    gateway.write_text("""import json,sys
from pathlib import Path
a=json.loads(Path(sys.argv[-1]).read_text())
if 'mesh_paths' in a:
    text='meshes/effect.nif\\n  ABSENT from checked sources\\n'
else:
    text=json.dumps({'read_incomplete':True,'results':[{'path':p,'exists':ASSET_EXISTS} for p in a['asset_paths']]})
print(json.dumps({'absence_conclusive':False,'context':{'archive_coverage':'incomplete'},'result':{'content':[{'type':'text','text':text}]}}))
""".replace('ASSET_EXISTS', repr(asset_exists)))
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'near', '--textures',
                             '--game-dir', str(root)], capture_output=True, text=True, env=env, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    assert 'Asset ' + expected_status + ': meshes/effect.nif' in result.stdout
    assert 'Offline NIF absent:' not in result.stdout
    assert 'Asset missing:' not in result.stdout


@pytest.mark.parametrize('failure_time,deadline_expected', [(5.1, True), (.1, False)])
def test_pending_drain_transport_error_distinguishes_deadline_from_early_failure(monkeypatch, failure_time, deadline_expected):
    from argparse import Namespace
    from whats_here import watch as module
    from whats_here.transport import ObservationError
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    class Client:
        timeout = 5
        def tools(self): return {'probe.watch'}
        def tool(self, name, body):
            if body.get('stop'): return {'ok': True}
            if body.get('read'):
                clock[0] = failure_time
                raise ObservationError('transport timeout')
            return {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'own', 'active': False, 'hasMore': False, 'pendingEvents': 1, 'events': [{'sequence': 1}], 'nextSequence': 1}}
    data = module.watch(Client(), Namespace(seconds=1, actor='0x14'))
    assert data['ok'] is False
    assert data['error'] == 'transport timeout'
    assert data['events'] == [{'sequence': 1}]
    assert data['pendingEvents'] == 1 and data['incomplete'] is True
    assert ('watch final drain deadline reached; pending events remain' in data['gaps']) is deadline_expected


def write_winner_recorder(root, exit_code=0):
    script = root / 'tools/skyrim-winner.sh'
    script.parent.mkdir(exist_ok=True)
    script.write_text('#!/bin/sh\nprintf "%s|%s|%s\\n" "$1" "$2" "$3" >> "' + (root / 'winner-calls.txt').as_posix() + '"\nprintf "   0. %s  [defines]   <-- WINS\\n" "$3"\nexit ' + str(exit_code) + '\n')


@pytest.mark.parametrize('form_id,expected_local,expected_plugin', [('0004D753', '04D753', 'Skyrim.esm'), ('01001218', '001218', 'Update.esm'), ('FE001ABC', '000ABC', 'Light.esl')])
def test_check_decodes_observed_full_and_light_map_instead_of_first_override(endpoint, form_id, expected_local, expected_plugin):
    run, replies, requests, root = endpoint
    replies['forms'] = {'formId': form_id, 'plugin': 'Override.esp', 'localId': '0x1001218'}
    replies['inspect'] = {'refs': [{'formId': form_id, 'name': 'Observed target'}]}
    replies['inspectmods'] = {'count': 2, 'lightCount': 1, 'total': 3, 'plugins': [{'index': 0, 'name': 'Skyrim.esm'}, {'index': 1, 'name': 'Update.esm'}], 'lightPlugins': [{'index': 1, 'name': 'Light.esl'}]}
    write_winner_recorder(root)
    result = run('ref', form_id, '--check')
    assert result.returncode == 0, result.stdout
    data = json.loads(result.stdout)
    assert data['forms'][form_id]['plugin'] == 'Override.esp'
    assert data['checks'][0]['runtimeIdentity']['plugin'] == expected_plugin
    assert data['checks'][0]['runtimeIdentity']['reportedIdentityMatches'] is False
    assert (root / 'winner-calls.txt').read_text().strip() == 'chain|' + expected_local + '|' + expected_plugin
    assert data['validationIncomplete'] is False
    assert len([body for key, body in requests if key == 'inspect' and body.get('kind') == 'mods']) == 1


@pytest.mark.parametrize('mapping', [
    {'count': 0, 'lightCount': 0, 'total': 0, 'plugins': [], 'lightPlugins': []},
    {'count': 2, 'lightCount': 0, 'total': 2, 'plugins': [{'index': 0, 'name': 'A.esm'}, {'index': 0, 'name': 'B.esm'}], 'lightPlugins': []},
    {'count': 2, 'lightCount': 0, 'total': 2, 'plugins': [{'index': 0, 'name': 'A.esm'}], 'lightPlugins': []},
    {'error': 'not found'},
])
def test_check_missing_ambiguous_or_unsupported_runtime_map_never_uses_reported_plugin(endpoint, mapping):
    run, replies, _, root = endpoint
    replies['forms'] = {'formId': '00001234', 'plugin': 'Plausible.esm', 'localId': '001234'}
    replies['inspectmods'] = mapping
    write_winner_recorder(root)
    data = json.loads(run('ref', '00001234', '--check').stdout)
    assert data['checks'][0]['status'] == 'not checked'
    assert data['validationIncomplete'] is True
    assert not (root / 'winner-calls.txt').exists()


def test_check_includes_probe_base_and_ref_separately(endpoint):
    run, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.ref')
    replies['probe.ref'] = {'schemaVersion': 1, 'ok': True, 'data': {'ref': {'formId': '00001234', 'baseFormId': '01005678', 'base': {'formId': '01005678'}}}}
    replies['forms'] = lambda body: {'formId': body['formId'], 'plugin': 'Override.esp', 'localId': '0x1BAD'}
    replies['inspectmods'] = {'count': 2, 'lightCount': 0, 'total': 2, 'plugins': [{'index': 0, 'name': 'Skyrim.esm'}, {'index': 1, 'name': 'Base.esm'}], 'lightPlugins': []}
    write_winner_recorder(root)
    data = json.loads(run('ref', '00001234', '--check').stdout)
    assert set(data['forms']) == {'00001234', '01005678'}
    assert {check['formId']: check['roles'] for check in data['checks']} == {'00001234': ['reference'], '01005678': ['base']}
    assert set((root / 'winner-calls.txt').read_text().splitlines()) == {'chain|001234|Skyrim.esm', 'chain|005678|Base.esm'}


def test_failed_chain_marks_validation_incomplete_and_is_visible_in_text(endpoint):
    _, replies, _, root = endpoint
    replies['forms'] = {'formId': '00001234', 'plugin': 'Origin.esm', 'localId': '001234'}
    write_winner_recorder(root, 2)
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'ref', '00001234', '--check', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert 'Validation incomplete' in result.stdout
    assert 'failed' in result.stdout


def test_check_off_does_not_request_unused_runtime_plugin_map(endpoint):
    run, _, requests, _ = endpoint
    run('near')
    assert not any(key == 'inspect' and body.get('kind') == 'mods' for key, body in requests)


@pytest.mark.parametrize('cursor_lost,want_incomplete', [(False, False), (True, True)])
def test_retention_evictions_after_delivery_do_not_claim_missing_events(endpoint, cursor_lost, want_incomplete):
    run, replies, _, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'own', 'active': False, 'events': [{'sequence': n} for n in range(1, 2432)], 'dropped': 431, 'cursorLost': cursor_lost}}
    data = json.loads(run('watch', '--seconds', '.01').stdout)
    assert len(data['events']) == 2431
    assert data['dropped'] == 431
    assert data['incomplete'] is want_incomplete


def test_retention_evictions_text_is_not_reported_as_event_loss(endpoint):
    _, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = {'schemaVersion': 1, 'ok': True, 'data': {'captureId': 'own', 'active': False, 'events': [], 'dropped': 431, 'cursorLost': False}}
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'watch', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert 'Ring retention evictions: 431' in result.stdout
    assert 'capture events dropped' not in result.stdout


def test_check_fails_when_offline_chain_traced_another_files_record(endpoint):
    # skyrim-winner once matched a master record the plugin overrides (Skyrim.esm 00001234) instead of the
    # plugin's own xx001234: an answer about a different record must never read as "checked".
    run, replies, _, root = endpoint
    replies['forms'] = {'formId': '00001234', 'plugin': 'Origin.esm', 'localId': '001234', 'sourceFiles': ['Origin.esm']}
    script = root / 'tools/skyrim-winner.sh'
    script.parent.mkdir()
    script.write_text('#!/bin/sh\nprintf "   0. Skyrim.esm  [defines]\\n   1. Origin.esm  [overrides]   <-- WINS\\n"\n')
    result = run('ref', '00001234', '--check')
    assert result.returncode == 3, result.stdout
    check = json.loads(result.stdout)['checks'][0]
    assert check['status'] == 'failed' and 'Skyrim.esm' in check['reason'] and 'Origin.esm' in check['reason']


def test_check_is_capped(endpoint, monkeypatch):
    run, replies, _, root = endpoint
    replies['inspect'] = {'refs': [{'formId': '%08X' % (0x1000 + i), 'name': 'r'} for i in range(15)]}
    replies['forms'] = {'formId': '00001000', 'plugin': 'Origin.esm', 'localId': '001000'}
    write_winner_recorder(root)
    result = run('near', '--check')
    data = json.loads(result.stdout)
    capped = [c for c in data['checks'] if 'limit' in c.get('reason', '')]
    assert len(data['checks']) - len(capped) <= 12 and capped
    assert len((root / 'winner-calls.txt').read_text().splitlines()) <= 12


@pytest.mark.parametrize('given,expected', [
    ('textures\\a\\b.dds', 'textures/a/b.dds'), ('a/b.dds', 'textures/a/b.dds'), ('\\textures\\a.dds', 'textures/a.dds'),
    ('Data\\Textures\\a.dds', 'textures/a.dds'), ('C:\\Games\\Skyrim\\Data\\textures\\x\\y.dds', 'textures/x/y.dds'),
    ('Data\\a.dds', 'textures/a.dds'), ('C:\\x\\y.dds', 'C:/x/y.dds')])
def test_texture_paths_are_normalised_not_prefixed(given, expected):
    sys.path.insert(0, str(CLI.parent))
    from whats_here.enrichment import texture_path
    assert texture_path(given).casefold() == expected.casefold()
    assert texture_path(given.replace('textures', 'meshes').replace('Textures', 'Meshes').replace('.dds', '.nif'),
                        'meshes').casefold() == expected.replace('textures', 'meshes').replace('.dds', '.nif').casefold()


def test_look_attaches_an_external_pick_unparsed_and_labelled(endpoint):
    run, _, _, root = endpoint
    folder = root / 'Data/SKSE/Plugins/WhatAmILookingAt'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'last-pick.json').write_text('{"anything": "the mod wrote", "formId": "00012345"}', encoding='utf-8')
    data = json.loads(run('look').stdout)
    pick = data['externalPick']
    assert pick['status'] == 'read' and pick['content'] == {'anything': 'the mod wrote', 'formId': '00012345'}
    assert pick['path'] == 'Data/SKSE/Plugins/WhatAmILookingAt/last-pick.json' and 'not checked' in pick['source']
    assert any('not cross-checked' in gap for gap in data['gaps'])


def test_look_without_an_external_pick_adds_nothing(endpoint):
    run, *_ = endpoint
    assert 'externalPick' not in json.loads(run('look').stdout)


def test_an_external_pick_that_vanishes_mid_read_never_fails_look(tmp_path, monkeypatch):
    sys.path.insert(0, str(CLI.parent))
    from whats_here import enrichment
    folder = tmp_path / 'Data/SKSE/Plugins'
    folder.mkdir(parents=True)
    (folder / 'last-pick.json').write_text('{}', encoding='utf-8')
    # Listed by the glob, then replaced or deleted before its time is read (the mod rewrites it on every click).
    def vanished(*args, **kwargs):
        raise FileNotFoundError(2, 'gone', 'last-pick.json')
    monkeypatch.setattr(enrichment, 'max', vanished, raising=False)
    pick = enrichment.external_pick(tmp_path)
    assert pick['status'] == 'not read' and pick['reason'] == 'FileNotFoundError'
