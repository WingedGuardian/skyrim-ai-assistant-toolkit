"""Console suppression at the gateway's own subprocess boundaries, offline."""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / 'tools'))
from housecarl_integration import Refusal, gateway


def cli():
    spec = importlib.util.spec_from_file_location('housecarl_visibility', REPO / 'tools/housecarl.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize('command', ['call', 'serve', 'register', 'unregister'])
@pytest.mark.parametrize('implicit_argv', [False, True])
def test_venv_reexec_is_hidden_and_preserves_arguments_and_exit_code(tmp_path, monkeypatch, command, implicit_argv):
    mod = cli()
    py = tmp_path / '.skyguard/housecarl/venv/Scripts/python.exe'
    py.parent.mkdir(parents=True)
    py.touch()  # Deliberately not executable: every child call is intercepted.
    argv = ['--root', str(tmp_path), command] + (['housecarl_asset_status', '--arguments', '{"paths":[]}'] if command == 'call' else [])
    monkeypatch.setattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
    calls = []

    def call(args, **kwargs):
        calls.append(args)
        assert kwargs == {'creationflags': 0x08000000,
                          'stdin': sys.stdin, 'stdout': sys.stdout, 'stderr': sys.stderr}
        assert args == [str(py), str(REPO / 'tools/housecarl.py'), *argv]
        return 7

    monkeypatch.setattr(subprocess, 'call', call)
    monkeypatch.setattr(sys, 'argv', ['housecarl.py', *argv])
    assert mod.main(None if implicit_argv else argv) == 7
    assert len(calls) == 1


def test_missing_venv_still_refuses_without_starting_a_helper(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, 'call', lambda *a, **k: pytest.fail('missing venv must refuse'))
    assert cli().main(['--root', str(tmp_path), 'serve']) == 2
    assert 'Optional gateway environment missing' in capsys.readouterr().out


def test_node_validator_is_hidden_and_preserves_bounded_call(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)
    args, env = ['node', 'validator.js', 'candidate.esp'], {'PATH': 'fixture'}
    result = subprocess.CompletedProcess(args, 9, 'output', 'failure')

    def run(actual, **kwargs):
        assert actual is args
        assert kwargs == {'env': env, 'capture_output': True, 'text': True,
                          'encoding': 'utf-8', 'errors': 'replace', 'timeout': 120,
                          'creationflags': 0x08000000}
        return result

    monkeypatch.setattr(subprocess, 'run', run)
    assert gateway._node(args, env, tmp_path, 'validator') is result


def test_node_timeout_keeps_the_original_refusal(tmp_path, monkeypatch):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(Refusal, match='validator did not finish within 120 s; nothing published; retained'):
        gateway._node(['node', 'validator.js'], {}, tmp_path, 'validator')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows console contract')
def test_validated_reexec_kwargs_produce_a_console_free_real_child(tmp_path, monkeypatch, capfd):
    mod = cli()
    py = tmp_path / '.skyguard/housecarl/venv/Scripts/python.exe'
    py.parent.mkdir(parents=True)
    py.touch()
    real_call = subprocess.call
    captured = []

    def call(args, **kwargs):
        assert kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW
        captured.append(kwargs)
        return 0

    monkeypatch.setattr(subprocess, 'call', call)
    # pytest replaces stdin with a pseudofile; use a real OS-backed input handle.
    with open(os.devnull) as stdin:
        monkeypatch.setattr(sys, 'stdin', stdin)
        assert mod.main(['--root', str(tmp_path), 'serve']) == 0
        output = tmp_path / 'console.json'
        code = 'import ctypes,json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps({"console":ctypes.windll.kernel32.GetConsoleWindow()}))'
        assert real_call([sys.executable, '-c', code, str(output)], **captured[0]) == 0
        assert json.loads(output.read_text()) == {'console': 0}


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows console contract')
def test_validated_node_kwargs_produce_a_console_free_real_child(tmp_path, monkeypatch):
    real_run = subprocess.run
    captured = []

    def run(args, **kwargs):
        assert kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW
        captured.append(kwargs)
        return subprocess.CompletedProcess(args, 0, '', '')

    monkeypatch.setattr(subprocess, 'run', run)
    gateway._node(['node'], os.environ.copy(), tmp_path, 'validator')
    code = 'import ctypes,json; print(json.dumps({"console":ctypes.windll.kernel32.GetConsoleWindow()}))'
    result = real_run([sys.executable, '-c', code], **captured[0])
    assert result.returncode == 0 and json.loads(result.stdout) == {'console': 0}


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows no-console pipe inheritance')
def test_nested_hidden_reexec_preserves_live_stdio_and_exit_status(tmp_path):
    py = tmp_path / '.skyguard/housecarl/venv/Scripts/python.exe'
    py.parent.mkdir(parents=True)
    py.touch()  # Only satisfies production discovery; never executed.
    inner = "import ctypes,json,sys; value=sys.stdin.readline().strip() if sys.stdin else None; print(json.dumps({'input':value,'console':ctypes.windll.kernel32.GetConsoleWindow()}),flush=True); print('inner-stderr',file=sys.stderr,flush=True); sys.exit(7)"
    outer = r"""
import ctypes, importlib.util, subprocess, sys
assert ctypes.windll.kernel32.GetConsoleWindow() == 0
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location('production_cli', sys.argv[1] + '/housecarl.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real_call = subprocess.call
def harmless_child(args, **kwargs):
    # Validate production flags BEFORE starting even the harmless child.
    assert kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW
    return real_call([sys.executable, '-c', sys.argv[3]], **kwargs)
subprocess.call = harmless_child
sys.exit(mod.main(['--root', sys.argv[2], 'serve']))
"""
    flags = subprocess.CREATE_NO_WINDOW
    assert flags == 0x08000000
    result = subprocess.run([sys.executable, '-c', outer, str(REPO / 'tools'), str(tmp_path), inner],
                            input='live-request\n', capture_output=True, text=True,
                            timeout=20, creationflags=flags)
    assert result.returncode == 7, result.stderr
    assert result.stdout.strip(), 'hidden venv reexec lost stdout'
    assert json.loads(result.stdout) == {'input': 'live-request', 'console': 0}
    assert result.stderr.strip() == 'inner-stderr'
