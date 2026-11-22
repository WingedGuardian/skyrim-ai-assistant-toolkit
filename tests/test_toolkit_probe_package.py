"""Packaging must reject a DLL/receipt from a different source or changed bytes."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/package_toolkit_probe.py'


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def fixture(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    source = root / 'tools/toolkit-probe'
    source.mkdir(parents=True)
    (source / 'README.md').write_bytes(b'Test source only\n')     # LF bytes on every OS
    (root / 'LICENSE').write_bytes(b'Test license\n')
    git(root, 'add', '--', 'tools/toolkit-probe/README.md', 'LICENSE')
    git(root, 'commit', '-qm', 'fixture')
    sha = git(root, 'rev-parse', 'HEAD')
    dll = tmp_path / 'ToolkitProbe.dll'
    image = bytearray(256)
    image[:2] = b'MZ'
    struct.pack_into('<I', image, 0x3c, 128)
    image[128:132] = b'PE\0\0'
    struct.pack_into('<H', image, 132, 0x8664)
    struct.pack_into('<H', image, 150, 0x2000)
    dll.write_bytes(image)
    receipt = tmp_path / 'receipt.json'
    data = {'schemaVersion': 1, 'sourceCommit': sha,
            'dllSha256': hashlib.sha256(image).hexdigest(),
            'dependencies': {'test': 'fixture'}, 'toolchain': {'test': 'fixture'}}
    receipt.write_text(json.dumps(data))
    return root, sha, dll, receipt, data


def run(root, sha, dll, receipt, output):
    return subprocess.run([sys.executable, str(SCRIPT), '--repo', str(root), '--commit', sha,
                           '--dll', str(dll), '--receipt', str(receipt), '--output', str(output)],
                          text=True, capture_output=True)


def test_packages_exact_source_and_dll(tmp_path):
    root, sha, dll, receipt, _ = fixture(tmp_path)
    output = tmp_path / 'probe.zip'
    result = run(root, sha, dll, receipt, output)
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output) as z:
        assert z.read('Data/SKSE/Plugins/ToolkitProbe.dll') == dll.read_bytes()
        manifest = json.loads(z.read('build-receipt.json'))
        assert manifest['sourceCommit'] == sha
        assert set(z.namelist()) == {'Data/SKSE/Plugins/ToolkitProbe.dll',
                                     'README.txt', 'build-receipt.json', 'source.zip'}
        with zipfile.ZipFile(io.BytesIO(z.read('source.zip'))) as source:
            # Compare every nested blob with git, not the checkout's EOL/export rules.
            tracked = git(root, 'ls-tree', '-r', '--name-only', sha).splitlines()
            assert set(source.namelist()) == set(tracked)
            for name in tracked:
                committed = subprocess.check_output(['git', '-C', str(root), 'show', sha + ':' + name])
                assert source.read(name) == committed
            assert source.read('tools/toolkit-probe/README.md') == b'Test source only\n'
            assert 'LICENSE' in source.namelist()


def test_refuses_changed_dll(tmp_path):
    root, sha, dll, receipt, _ = fixture(tmp_path)
    dll.write_bytes(dll.read_bytes() + b'changed')
    output = tmp_path / 'probe.zip'
    result = run(root, sha, dll, receipt, output)
    assert result.returncode == 2
    assert 'hash' in result.stderr.lower()
    assert not output.exists()


def test_refuses_receipt_for_other_commit(tmp_path):
    root, sha, dll, receipt, data = fixture(tmp_path)
    data['sourceCommit'] = '0' * 40
    receipt.write_text(json.dumps(data))
    result = run(root, sha, dll, receipt, tmp_path / 'probe.zip')
    assert result.returncode == 2
    assert 'commit' in result.stderr.lower()


def test_refuses_uncommitted_source(tmp_path):
    root, sha, dll, receipt, _ = fixture(tmp_path)
    (root / 'tools/toolkit-probe/README.md').write_text('different input')
    result = run(root, sha, dll, receipt, tmp_path / 'probe.zip')
    assert result.returncode == 2
    assert 'source' in result.stderr.lower()


def test_refuses_non_x64_dll(tmp_path):
    root, sha, dll, receipt, data = fixture(tmp_path)
    image = bytearray(dll.read_bytes())
    struct.pack_into('<H', image, 132, 0x14c)
    dll.write_bytes(image)
    data['dllSha256'] = hashlib.sha256(image).hexdigest()
    receipt.write_text(json.dumps(data))
    result = run(root, sha, dll, receipt, tmp_path / 'probe.zip')
    assert result.returncode == 2
    assert 'x64' in result.stderr.lower()


def test_refuses_replacing_existing_artifact(tmp_path):
    root, sha, dll, receipt, _ = fixture(tmp_path)
    output = tmp_path / 'probe.zip'
    output.write_bytes(b'previous artifact')
    result = run(root, sha, dll, receipt, output)
    assert result.returncode == 2
    assert output.read_bytes() == b'previous artifact'


def test_untracked_source_is_not_silently_excluded(tmp_path):
    root, sha, dll, receipt, _ = fixture(tmp_path)
    (root / 'tools/toolkit-probe/new.cpp').write_text('uncommitted build input')
    result = run(root, sha, dll, receipt, tmp_path / 'probe.zip')
    assert result.returncode == 2
    assert 'uncommitted' in result.stderr


def test_real_installer_maps_only_dll_and_applies_to_scratch_data(tmp_path, monkeypatch):
    """Run real 7-Zip + both user entry points; never use the configured game."""
    import pytest
    monkeypatch.syspath_prepend(str(SCRIPT.parents[1] / 'tools'))
    import skyrim_paths
    sevenz, reason = skyrim_paths.find_7zip()
    if not sevenz:
        if os.environ.get('CI'):
            pytest.fail('7-Zip missing on CI, where this test must run: ' + reason)
        pytest.skip('real archive integration needs 7-Zip: ' + reason)
    root, sha, dll, receipt, _ = fixture(tmp_path)
    output = tmp_path / 'ToolkitProbe.zip'
    result = run(root, sha, dll, receipt, output)
    assert result.returncode == 0, result.stderr
    game = tmp_path / 'scratch-game'
    data = game / 'Data'
    data.mkdir(parents=True)
    (game / 'SkyrimSE.exe').touch()  # synthetic DLL need not have VR markers
    records = tmp_path / 'scratch-records'
    env = dict(os.environ, SEVENZIP=str(sevenz), SKYRIM_GAME_ROOT=str(game),
               SKYRIM_MANUAL_INSTALLS=str(records))
    env.pop('SKYRIM_MO2_MODS', None)
    tools = SCRIPT.parents[1] / 'tools'
    report = tmp_path / 'triage.json'
    def tool(name, *args):
        return subprocess.run([sys.executable, str(tools / name), str(output),
                               '--data', str(data), *args], env=env,
                              capture_output=True, text=True, timeout=120)
    triage = tool('archive-triage.py', '--json', str(report))
    assert triage.returncode == 0, triage.stdout + triage.stderr
    observed = json.loads(report.read_text())[0]
    assert observed['error'] is None
    assert set(observed['groups']) == {'(root)'}
    mapped = [row for rows in observed['groups']['(root)'].values() for row in rows]
    assert [(row['entry'].replace('\\', '/'), row['dest'], row['inside']) for row in mapped] == [
        ('Data/SKSE/Plugins/ToolkitProbe.dll', str(data / 'SKSE/Plugins/ToolkitProbe.dll'), True)]
    assert set(observed['unmapped']) == {'README.txt', 'source.zip', 'build-receipt.json'}
    dry = tool('mod-install.py')
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert 'DRY RUN -- nothing written' in dry.stdout
    assert not list(data.rglob('*')) and not records.exists()
    applied = tool('mod-install.py', '--apply')
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert {str(p.relative_to(data)).replace('\\', '/'): p.read_bytes()
            for p in data.rglob('*') if p.is_file()} == {'SKSE/Plugins/ToolkitProbe.dll': dll.read_bytes()}
    assert (records / 'ToolkitProbe.files.txt').is_file()
    manifest = (records / 'ToolkitProbe.files.txt').read_text()
    assert '# STATUS: complete, 1 file(s), 0 verify mismatch(es)' in manifest
    assert not any(p.is_file() for p in game.iterdir() if p.name != 'SkyrimSE.exe')
    # The same real triage now sees the sole installed file as SAME.
    again = tool('archive-triage.py', '--json', str(report))
    assert again.returncode == 0, again.stdout + again.stderr
    assert json.loads(report.read_text())[0]['tally'] == {'NEW': 0, 'SAME': 1, 'CHANGED': 0, 'NO CRC': 0}
