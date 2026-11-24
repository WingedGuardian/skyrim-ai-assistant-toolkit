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


# Skyrim 4.3: the session-start background helpers (housekeep sweep, save vault watcher, update check) start through
# skyguard.hidden, which never asks for DETACHED_PROCESS (Windows ignores CREATE_NO_WINDOW beside it, so a `py` launcher
# or venv redirector's real python.exe child opened a visible console).
SESSION_HELPERS = ("tools/skyguard/run.py", "tools/skyguard/vault_watch.py", "tools/skyguard/update_check.py")


def test_background_helpers_get_a_hidden_console_and_are_never_detached():
    if os.name != "nt":
        pytest.skip("creation flags are a Windows mechanism")
    from skyguard import hidden
    assert hidden.FLAGS & 0x08000000                      # CREATE_NO_WINDOW
    assert hidden.FLAGS & 0x00000200                      # CREATE_NEW_PROCESS_GROUP
    assert not hidden.FLAGS & 0x00000008                  # never DETACHED_PROCESS


def test_spawn_background_passes_the_hidden_flags_and_no_handles(monkeypatch):
    from skyguard import hidden
    seen = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw)))
    # the Windows value on every OS: FLAGS is 0 off Windows, where a call passing 0 would look the same
    sentinel = hidden.CREATE_NEW_PROCESS_GROUP | hidden.CREATE_NO_WINDOW
    monkeypatch.setattr(hidden, "FLAGS", sentinel)
    hidden.spawn_background(["x", "y"], REPO)
    (argv, kw), = seen
    assert argv == ["x", "y"] and kw["creationflags"] == sentinel and kw["close_fds"] is True
    assert kw["stdin"] is kw["stdout"] is kw["stderr"] is subprocess.DEVNULL


def test_every_session_start_helper_starts_through_hidden():
    import ast
    for rel in SESSION_HELPERS:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        assert not [n.lineno for n in calls if n.func.attr == "Popen"], rel + " starts a process itself"
        assert [n for n in calls if n.func.attr == "spawn_background"], rel + " no longer starts its helper hidden"


def test_no_shipped_tool_asks_for_detached_process():
    """A code token, not a mention: comments and docstrings that explain the rule are fine."""
    import io
    import tokenize
    bad = []
    for path in sorted((REPO / "tools").rglob("*.py")):
        if {".venv", "site-packages", "__pycache__"} & set(path.parts):
            continue
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline))
        except (UnicodeDecodeError, tokenize.TokenError, SyntaxError):
            continue
        for tok in tokens:
            if (tok.type == tokenize.NAME and tok.string == "DETACHED_PROCESS") or                     (tok.type == tokenize.NUMBER and tok.string.lower() == "0x00000008"):
                bad.append("{}:{}".format(path.relative_to(REPO).as_posix(), tok.start[0]))
    assert bad == []


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows console contract')
def test_a_background_helpers_own_console_child_has_no_window_either():
    """The failure DETACHED_PROCESS caused: the helper's CHILD (the real python.exe a `py` launcher or venv redirector
    starts, with no flags of its own) got a new, visible console. Under skyguard.hidden's flags it inherits the
    hidden one. Positive case only: never launch the unhidden contrast (it would open a window)."""
    from skyguard import hidden
    grandchild = 'import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())'
    helper = ('import subprocess, sys; r = subprocess.run([sys.executable, "-c", {!r}], capture_output=True, text=True); '
              'print(r.stdout.strip())').format(grandchild)
    probe = 'import ctypes, subprocess, sys; print(ctypes.windll.kernel32.GetConsoleWindow()); ' + helper
    child = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, timeout=60,
                           creationflags=hidden.FLAGS)
    assert child.returncode == 0, child.stderr
    assert child.stdout.split() == ['0', '0']             # neither the helper nor its own child has a console window
