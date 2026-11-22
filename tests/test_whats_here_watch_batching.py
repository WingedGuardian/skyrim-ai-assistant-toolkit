"""Behavioral CLI contracts for bounded event/read-assisted watch draining."""
import json
import os
import subprocess
import sys
import time
from argparse import Namespace

import pytest

from test_whats_here import CLI, endpoint


def capture(data, **envelope):
    return {'schemaVersion': 1, 'ok': True, 'sessionId': 'same', 'generation': 1,
            'data': {'captureId': 'own', 'events': [], 'nextSequence': 0, 'pendingEvents': 0, **data}, **envelope}


def test_active_pending_is_drained_before_normal_poll_and_empty_page_can_publish_later(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    reads = []
    def watch(body):
        if body.get('stop'): return capture({'active': False})
        if not body.get('read'): return capture({'active': True, 'pendingEvents': 2})
        reads.append(time.monotonic())
        if len(reads) == 1: return capture({'active': True, 'pendingEvents': 1})
        if len(reads) == 2: return capture({'active': True, 'pendingEvents': 0, 'hasMore': True, 'latestSequence': 1})
        return capture({'active': True, 'events': [{'sequence': 1, 'sourceFormId': 'READY'}], 'nextSequence': 1})
    replies['probe.watch'] = watch
    started = time.monotonic()
    result = run('watch', '--seconds', '.6')
    data = json.loads(result.stdout)
    assert data['events'] == [{'sequence': 1, 'sourceFormId': 'READY'}]
    assert len(reads) >= 3 and reads[2] - started < .5
    assert 'watch pagination made no progress' not in data['gaps']


def test_unchanged_active_pending_is_paced_and_cycle_cutoff_preserves_capture_duration(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        return capture({'active': False}) if body.get('stop') else capture({'active': True, 'pendingEvents': 2000})
    replies['probe.watch'] = watch
    started = time.monotonic()
    result = run('watch', '--seconds', '2.5')
    elapsed = time.monotonic() - started
    data = json.loads(result.stdout)
    reads = [body for key, body in requests if key == 'probe.watch' and body.get('read')]
    assert 1 <= data['activeDrainCyclesExhausted'] <= 2
    assert data['activeDrainCutoffs'][0]['pendingEvents'] == 2000
    assert 1 <= len(reads) <= 42
    assert 2.4 <= elapsed < 4
    assert len([body for key, body in requests if key == 'probe.watch' and body.get('stop')]) == 1
    assert data['pendingEvents'] == 0


def test_pending_decrease_never_permits_a_backward_published_cursor(endpoint):
    run, replies, _, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return capture({'active': False})
        if body.get('read'): return capture({'active': True, 'pendingEvents': 1, 'hasMore': True, 'nextSequence': 0, 'latestSequence': 2})
        return capture({'active': True, 'pendingEvents': 2, 'hasMore': True, 'nextSequence': 1, 'events': [{'sequence': 1}], 'latestSequence': 1})
    replies['probe.watch'] = watch
    data = json.loads(run('watch', '--seconds', '.4').stdout)
    assert 'watch pagination made no progress' in data['gaps']
    assert data['events'] == [{'sequence': 1}]


@pytest.mark.parametrize('bad_pending', [True, -1, 1.5, '2'])
def test_pending_control_value_must_be_nonnegative_integer(endpoint, bad_pending):
    run, replies, _, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = capture({'active': True, 'pendingEvents': bad_pending})
    result = run('watch', '--seconds', '.01')
    data = json.loads(result.stdout)
    assert result.returncode == 2
    assert 'pendingEvents' in data['error']


def test_active_cycle_deadline_does_not_reset_on_each_read(monkeypatch):
    from whats_here import watch as module
    clock, sleeps = [0.0], []
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(module.time, 'sleep', sleep)
    class Client:
        timeout = 60
        reads = []
        stop_times = []
        def tools(self): return {'probe.watch'}
        def tool(self, name, body):
            if body.get('stop'):
                self.stop_times.append(clock[0])
                return capture({'active': False})
            if body.get('read'):
                self.reads.append(self.timeout)
                clock[0] += 2
            return capture({'active': True, 'pendingEvents': 100})
    client = Client()
    data = module.watch(client, Namespace(seconds=12, actor='0x14'))
    assert data['activeDrainCyclesExhausted'] >= 1
    assert data['activeDrainCutoffs'][0]['reason'] == 'time limit'
    assert data['normalPolls'] >= 1  # Return to the normal poll anchor after a cutoff.
    assert client.stop_times[0] >= 12
    assert client.reads[1] < client.reads[0]
    assert client.timeout == 60


def test_batch_telemetry_is_aggregated_and_global_queue_scope_is_visible_in_text(endpoint):
    run, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return {'ok': True}
        read = body.get('read')
        return capture({'active': False, 'hasMore': not read, 'nextSequence': 2 if read else 1,
                        'events': [{'sequence': 2 if read else 1}],
                        'maxDeliveryDelayMs': 25 if read else 10, 'maxDrainMs': 3 if read else 4,
                        'drainBudgetStops': 5 if read else 4, 'queueHighWaterMark': 200 if read else 100,
                        'visualSampling': 'per-actor-capture-drain'})
    replies['probe.watch'] = watch
    data = json.loads(run('watch', '--seconds', '.01').stdout)
    assert (data['maxDeliveryDelayMs'], data['maxDrainMs'], data['drainBudgetStops'], data['queueHighWaterMark']) == (25, 4, 5, 200)
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    result = subprocess.run([sys.executable, str(CLI), 'watch', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert 'Global pending queue high-water mark experienced: 200' in result.stdout
    assert 'Maximum event delivery delay (ms): 25' in result.stdout
    assert 'Maximum native drain (ms): 4' in result.stdout
    assert 'Native drain budget stops: 5' in result.stdout
    assert 'per-actor-capture-drain' in result.stdout and 'miss' in result.stdout


def test_telemetry_survives_partial_error_in_json_and_text(endpoint):
    run, replies, _, root = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return {'ok': True}
        if body.get('read'): return {'error': 'read failed'}
        return capture({'active': False, 'pendingEvents': 1, 'maxDeliveryDelayMs': 9,
                        'maxDrainMs': 4, 'drainBudgetStops': 2, 'queueHighWaterMark': 700,
                        'visualSampling': 'per-actor-capture-drain'})
    replies['probe.watch'] = watch
    result = run('watch', '--seconds', '.01')
    data = json.loads(result.stdout)
    assert result.returncode == 2 and data['queueHighWaterMark'] == 700
    assert data['maxDeliveryDelayMs'] == 9 and data['incomplete'] is True
    env = dict(os.environ)
    env.pop('DEVBENCH_PORT', None)
    text = subprocess.run([sys.executable, str(CLI), 'watch', '--game-dir', str(root)], capture_output=True, text=True, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert text.returncode == 2
    assert 'read failed' in text.stdout and 'Global pending queue high-water mark experienced: 700' in text.stdout
    assert 'per-actor-capture-drain' in text.stdout


def test_active_budget_timeout_resumes_at_poll_anchor_without_losing_ownership(monkeypatch):
    from whats_here import watch as module
    from whats_here.transport import ObservationError
    clock, calls = [0.0], []
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    class Client:
        timeout = 10
        def tools(self): return {'probe.watch'}
        def tool(self, name, body):
            calls.append((clock[0], body.copy()))
            if body.get('stop'): return capture({'active': False})
            if body.get('read') and len(calls) == 2:
                clock[0] = 5.01
                raise ObservationError('clipped active request timed out')
            return capture({'active': True, 'pendingEvents': 1})
    data = module.watch(Client(), Namespace(seconds=6, actor='0x14'))
    assert data['ok'] is True
    assert data['activeDrainCyclesExhausted'] >= 1
    assert data['activeDrainCutoffs'][0]['error'] == 'clipped active request timed out'
    assert len([body for _, body in calls if body.get('stop')]) == 1
    assert next(t for t, body in calls if body.get('stop')) >= 6


def test_pending_only_terminal_reads_keep_full_window_and_explicit_counters(endpoint):
    run, replies, _, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = capture({'active': False, 'pendingEvents': 1})
    started = time.monotonic()
    data = json.loads(run('watch', '--seconds', '.01', timeout=8).stdout)
    assert 4.8 <= time.monotonic() - started < 7
    assert data['terminalPageReads'] == 0
    assert 1 <= data['terminalPendingReads'] <= 101
    assert data['pendingEvents'] == 1 and data['incomplete'] is True


def test_newly_admitted_event_can_publish_without_observed_pending_decrease(endpoint):
    run, replies, _, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    phase = [0]
    def watch(body):
        if body.get('stop'): return capture({'active': False})
        if not body.get('read'):
            # Existing pending work first provokes an assisted read. Then one
            # newly admitted callback is completely drained between snapshots.
            return capture({'active': True, 'pendingEvents': 1, 'latestSequence': 0})
        phase[0] += 1
        if phase[0] == 1: return capture({'active': True, 'pendingEvents': 0, 'latestSequence': 0})
        if phase[0] == 2: return capture({'active': False, 'pendingEvents': 0, 'latestSequence': 1, 'hasMore': True})
        return capture({'active': False, 'pendingEvents': 0, 'latestSequence': 1, 'nextSequence': 1, 'events': [{'sequence': 1}]})
    replies['probe.watch'] = watch
    data = json.loads(run('watch', '--seconds', '.2').stdout)
    assert data['events'] == [{'sequence': 1}]
    assert 'watch pagination made no progress' not in data['gaps']


@pytest.mark.parametrize('latest', [True, -1, .5, '1'])
def test_publication_watermark_must_be_nonnegative_integer(endpoint, latest):
    run, replies, _, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = capture({'active': False, 'latestSequence': latest})
    result = run('watch', '--seconds', '.01')
    assert result.returncode == 2
    assert 'latestSequence' in json.loads(result.stdout)['error']


def test_publication_watermark_cannot_go_backwards_even_when_pending_decreases(endpoint):
    run, replies, _, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return {'ok': True}
        if body.get('read'): return capture({'active': False, 'pendingEvents': 0, 'latestSequence': 0})
        return capture({'active': False, 'pendingEvents': 1, 'latestSequence': 1})
    replies['probe.watch'] = watch
    result = run('watch', '--seconds', '.01')
    assert result.returncode == 2
    assert 'latestSequence' in json.loads(result.stdout)['error']


def test_publication_watermark_cannot_precede_delivered_rows(endpoint):
    run, replies, _, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    replies['probe.watch'] = capture({'active': False, 'latestSequence': 1, 'nextSequence': 2, 'events': [{'sequence': 2}]})
    result = run('watch', '--seconds', '.01')
    assert result.returncode == 2
    assert 'latestSequence' in json.loads(result.stdout)['error']


def test_terminal_initial_page_counts_toward_exact_41_response_boundary(endpoint):
    run, replies, requests, _ = endpoint
    replies['/api/tools']['tools'].append('probe.watch')
    def watch(body):
        if body.get('stop'): return {'ok': True}
        n = body.get('after', 0) + 1
        return capture({'active': False, 'hasMore': True, 'pendingEvents': 9,
                        'latestSequence': 1000, 'nextSequence': n, 'events': [{'sequence': n}]})
    replies['probe.watch'] = watch
    data = json.loads(run('watch', '--seconds', '.01').stdout)
    assert data['terminalPageReads'] == 41
    assert len(data['events']) == 41
    assert len([body for key, body in requests if key == 'probe.watch' and body.get('read')]) == 40
    assert data['pendingEvents'] == 9 and data['incomplete'] is True
    assert 'watch pagination page budget reached; events remain' in data['gaps']


def test_explicit_stop_reply_is_the_initial_terminal_page(monkeypatch):
    from whats_here import watch as module
    clock, stopped, sequence = [0.0], [False], [0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    class Client:
        timeout = 5
        def tools(self): return {'probe.watch'}
        def tool(self, name, body):
            if body.get('stop'): stopped[0] = True
            if not stopped[0]: return capture({'active': True})
            sequence[0] += 1
            return capture({'active': False, 'hasMore': True, 'nextSequence': sequence[0],
                            'events': [{'sequence': sequence[0]}]})
    data = module.watch(Client(), Namespace(seconds=.1, actor='0x14'))
    assert data['terminalPageReads'] == len(data['events']) == 41
    assert data['terminalPendingReads'] == 0


def test_mixed_terminal_intent_budgets_are_disjoint_never_reset_and_total_142(monkeypatch):
    from whats_here import watch as module
    # Freeze wall time independently to reach both category ceilings. Real HTTP
    # tests separately verify that the same fixed five-second wall also applies.
    monkeypatch.setattr(module.time, 'monotonic', lambda: 0)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    phase = [0]
    class Client:
        timeout = 5
        def tools(self): return {'probe.watch'}
        def tool(self, name, body):
            if body.get('stop'): return {'ok': True}
            phase[0] += 1
            # First40 replies are pages. Reply41 completes a page-intent read
            # with pending-only data; subsequent101 replies are pending assists.
            if phase[0] <= 40:
                return capture({'active': False, 'hasMore': True, 'pendingEvents': 1,
                                'events': [{'sequence': phase[0]}], 'nextSequence': phase[0]})
            return capture({'active': False, 'hasMore': False, 'pendingEvents': 1})
    data = module.watch(Client(), Namespace(seconds=1, actor='0x14'))
    assert data['terminalPageReads'] == 41
    assert data['terminalPendingReads'] == 101
    assert phase[0] == 142
    assert len(data['events']) == 40 and data['pendingEvents'] == 1
    assert data['incomplete'] is True
    assert 'watch pending read budget reached; pending events remain' in data['gaps']


def test_pending_assist_returning_rows_does_not_open_a_new_terminal_budget(monkeypatch):
    from whats_here import watch as module
    monkeypatch.setattr(module.time, 'monotonic', lambda: 0)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    phase = [0]
    class Client:
        timeout = 5
        def tools(self): return {'probe.watch'}
        def tool(self, name, body):
            if body.get('stop'): return {'ok': True}
            phase[0] += 1
            if phase[0] == 1: return capture({'active': False, 'pendingEvents': 1})
            return capture({'active': False, 'pendingEvents': 1, 'hasMore': phase[0] < 43,
                            'events': [{'sequence': phase[0] - 1}], 'nextSequence': phase[0] - 1})
    data = module.watch(Client(), Namespace(seconds=1, actor='0x14'))
    assert (data['terminalPageReads'], data['terminalPendingReads']) == (41, 101)
    assert phase[0] == 142 and len(data['events']) == 141
    assert data['incomplete'] is True and data['pendingEvents'] == 1
