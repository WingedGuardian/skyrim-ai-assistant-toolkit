"""Exercise production helper kwargs without ever spawning an unhidden contrast."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import REPO
from test_vr_headless import load

sys.path.insert(0, str(REPO / 'tools'))
from whats_here import enrichment


WINNER_OUT = '   0. Skyrim.esm  [defines]   <-- WINS\nRESULT: OK\n'
CASES = ['tasklist', 'processes', 'parent', 'tree', 'gateway', 'winner']


def popen_through(run):
    """enrichment.run_hidden uses Popen (to kill a timed-out child's whole tree); feed it through the same `run`
    stub, with the kwargs it would amount to, so every contract below still applies to it."""
    class FakePopen:
        pid = 987654          # never 0 or 1: see test_a_timed_out_helper_on_posix_*

        def __init__(self, args, **kwargs):
            assert kwargs.pop('stdout') is subprocess.PIPE and kwargs.pop('stderr') is subprocess.PIPE
            kwargs.pop('start_new_session', None)
            self.args, self.kwargs = args, kwargs

        def communicate(self, timeout=None):
            result = run(self.args, capture_output=True, timeout=timeout, **self.kwargs)
            self.returncode = result.returncode
            return result.stdout, result.stderr

        def kill(self):
            pass
    return FakePopen


def invoke(case, monkeypatch, tmp_path, run):
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(subprocess, 'Popen', popen_through(run))
    monkeypatch.setattr(os, 'killpg', lambda pid, sig: None, raising=False)   # a stub child has no real group
    if case in CASES[:4]:
        mod = load()
        mod.IS_WINDOWS = True
        mod.os = SimpleNamespace(name='nt')
        return {'tasklist': mod.steamvr_running, 'processes': mod.read_processes,
                'parent': lambda: mod.parent_pid(123), 'tree': mod.read_process_tree}[case]()
    if case == 'gateway':
        return enrichment.gateway(tmp_path, 'housecarl_asset_status', {'paths': ['textures/a.dds']})
    monkeypatch.setenv('TOOLKIT_BASH', 'fixture-bash')
    return enrichment.check_chain(tmp_path, {}, {'status': 'resolved', 'plugin': 'Skyrim.esm', 'localId': '000001'})


@pytest.mark.parametrize('case', CASES)
def test_each_production_helper_requests_no_console(case, monkeypatch, tmp_path):
    calls = []
    # Nonzero sentinel proves the expression is applied even on non-Windows CI.
    monkeypatch.setattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)

    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs['creationflags'] == 0x08000000
        assert kwargs['capture_output'] is True and kwargs['text'] is True
        assert kwargs['timeout'] == (30 if case == 'tasklist' else 60 if case in CASES[4:] else 8)
        if case in {'processes', 'tree'}:
            assert kwargs['check'] is True
        stdout = {'tasklist': '"explorer.exe","1"', 'parent': '42',
                  'gateway': '{"result":{}}', 'winner': WINNER_OUT}.get(case, '[]')
        return subprocess.CompletedProcess(args, 0, stdout, '')

    result = invoke(case, monkeypatch, tmp_path, run)
    assert len(calls) == 1
    assert result == {'tasklist': False, 'processes': [], 'parent': 42, 'tree': [],
                      'gateway': {'result': {}}, 'winner': {'status': 'checked', 'plugin': 'Skyrim.esm',
                      'localId': '000001', 'offlineOutput': WINNER_OUT, 'stderr': '',
                      'comparison': 'not automatically reconciled; inspect both sources'}}[case]


@pytest.mark.parametrize('case', CASES)
def test_timeout_keeps_existing_read_failure_semantics(case, monkeypatch, tmp_path):
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])

    if case in {'processes', 'tree'}:
        with pytest.raises(Exception, match='process') as error:
            invoke(case, monkeypatch, tmp_path, run)
        assert type(error.value).__name__ == 'ProcessReadError'
        assert len(calls) == 3
    elif case in {'parent', 'gateway'}:
        with pytest.raises(subprocess.TimeoutExpired):
            invoke(case, monkeypatch, tmp_path, run)
    else:
        result = invoke(case, monkeypatch, tmp_path, run)
        assert result is True if case == 'tasklist' else result['status'] == 'failed'


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows console contract')
@pytest.mark.parametrize('case', CASES)
def test_real_console_child_with_exact_production_kwargs_has_no_console(case, monkeypatch, tmp_path):
    real_run = subprocess.run
    captured = []

    def capture(args, **kwargs):
        # Validate before launching any child; no unhidden negative control.
        assert kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW
        captured.append(kwargs)
        return subprocess.CompletedProcess(args, 0, {'tasklist': '"explorer.exe","1"',
            'parent': '42', 'gateway': '{"result":{}}', 'winner': 'chain'}.get(case, '[]'), '')

    real_popen = subprocess.Popen
    invoke(case, monkeypatch, tmp_path, capture)
    monkeypatch.setattr(subprocess, 'Popen', real_popen)      # the real child below needs the real Popen
    kwargs = captured[0]
    if 'cwd' in kwargs:
        assert kwargs['cwd'] == tmp_path
    probe = 'import ctypes,json; print(json.dumps({"console":ctypes.windll.kernel32.GetConsoleWindow()}))'
    child = real_run([sys.executable, '-c', probe], **kwargs)
    assert child.returncode == 0 and json.loads(child.stdout) == {'console': 0}


@pytest.mark.skipif(sys.platform != 'win32', reason='taskkill tree kill is the Windows path; POSIX kills the process group')
def test_a_timed_out_helper_has_its_whole_tree_killed(monkeypatch):
    # House Carl re-executes into its venv and skyrim-winner runs bash -> node: killing only the direct child
    # left the grandchildren running and holding the pipes.
    calls = []

    class Stuck:
        pid = 4242

        def __init__(self, args, **kwargs):
            pass

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired('helper', timeout)

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, 'Popen', Stuck)
    monkeypatch.setattr(subprocess, 'run', lambda args, **kwargs: calls.append((args, kwargs)))
    with pytest.raises(subprocess.TimeoutExpired):
        enrichment.run_hidden(['helper'], 1)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0].lower().endswith('taskkill.exe') and args[1:] == ['/F', '/T', '/PID', '4242']
    assert kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW


@pytest.mark.parametrize('pid, killed', [(4242, [4242]), (1, []), (0, [])])
def test_a_timed_out_helper_on_posix_has_its_process_group_killed_but_never_group_0_or_1(monkeypatch, pid, killed):
    # killpg(1) is kill(-1): every process the user may signal. A stub with pid 1 sent that on CI and killed the
    # runner (run 45052617918, all four Linux legs). Runs on every platform: os.name and killpg are patched.
    import signal
    sent = []

    class Stuck:
        def __init__(self, args, **kwargs):
            self.pid = pid

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired('helper', timeout)

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, 'Popen', Stuck)
    monkeypatch.setattr(os, 'killpg', lambda p, sig: sent.append((p, sig)), raising=False)
    monkeypatch.setattr(signal, 'SIGKILL', 9, raising=False)
    monkeypatch.setattr(os, 'name', 'posix')
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            enrichment.run_hidden(['helper'], 1)
    finally:
        monkeypatch.undo()
    assert sent == [(p, 9) for p in killed]
