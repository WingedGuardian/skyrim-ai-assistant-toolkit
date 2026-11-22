"""Build provenance must be captured before compilation, and rejected on drift."""
import importlib.util
from pathlib import Path

from test_toolkit_probe_package import fixture, git


def module():
    path = Path(__file__).resolve().parents[1] / 'scripts/build_toolkit_probe.py'
    spec = importlib.util.spec_from_file_location('probe_build', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_source_snapshot_rejects_changed_file(tmp_path):
    m = module()
    root, sha, *_ = fixture(tmp_path)
    snapshot = m.source_snapshot(root)
    assert snapshot == sha
    (root / 'tools/toolkit-probe/README.md').write_text('edited after compiler started')
    import pytest
    with pytest.raises(ValueError, match='source'):
        m.verify_snapshot(root, snapshot)


def test_source_snapshot_rejects_new_commit(tmp_path):
    m = module()
    root, sha, *_ = fixture(tmp_path)
    (root / 'unrelated').write_text('new commit')
    git(root, 'add', '--', 'unrelated')
    git(root, 'commit', '-qm', 'concurrent commit')
    import pytest
    with pytest.raises(ValueError, match='commit'):
        m.verify_snapshot(root, sha)


def test_wrong_configured_source_is_refused(tmp_path):
    m = module()
    root, *_ = fixture(tmp_path)
    build = tmp_path / 'build'
    build.mkdir()
    (build / 'CMakeCache.txt').write_text('CMAKE_HOME_DIRECTORY:INTERNAL=C:/different/source\n')
    import pytest
    with pytest.raises(ValueError, match='configured'):
        m.verify_cache(root, build)


def test_install_fixture_excludes_native_build_products(tmp_path, monkeypatch):
    import conftest
    source = tmp_path / 'source'
    native = source / 'tools/toolkit-probe'
    (native / 'build').mkdir(parents=True)
    (native / 'build/rebuildable.bin').write_bytes(b'build cache')
    (native / 'README.md').write_text('shipping source')
    (source / 'examples/build').mkdir(parents=True)
    (source / 'examples/build/recipe.txt').write_text('keep unrelated source')
    monkeypatch.setattr(conftest, 'REPO', source)
    destination = tmp_path / 'copy'
    conftest.copy_repo(destination)
    assert not (destination / 'tools/toolkit-probe/build').exists()
    assert (destination / 'tools/toolkit-probe/README.md').exists()
    assert (destination / 'examples/build/recipe.txt').exists()


def _cache(tmp_path, root, **values):
    build = tmp_path / 'build'
    build.mkdir(exist_ok=True)
    base = {'CMAKE_HOME_DIRECTORY': (root / 'tools/toolkit-probe').as_posix(), 'COMMONLIB_PREBUILT': 'OFF',
            'ENABLE_SKYRIM_SE': 'ON', 'ENABLE_SKYRIM_AE': 'ON', 'ENABLE_SKYRIM_VR': 'ON'}
    base.update(values)
    (build / 'CMakeCache.txt').write_text(''.join('%s:STRING=%s\n' % kv for kv in base.items()))
    return build


def test_a_prebuilt_commonlib_build_is_refused(tmp_path):
    import pytest
    m = module()
    root, *_ = fixture(tmp_path)
    m.verify_cache(root, _cache(tmp_path, root))               # the control: a source build is accepted
    with pytest.raises(ValueError, match='PREBUILT'):
        m.verify_cache(root, _cache(tmp_path, root, COMMONLIB_PREBUILT='ON'))


def test_a_build_missing_a_runtime_is_refused(tmp_path):
    import pytest
    m = module()
    root, *_ = fixture(tmp_path)
    with pytest.raises(ValueError, match='SE, AE and VR'):
        m.verify_cache(root, _cache(tmp_path, root, ENABLE_SKYRIM_AE='OFF'))


def test_uncommitted_source_inputs_are_refused_before_building(tmp_path):
    import pytest
    m = module()
    root, *_ = fixture(tmp_path)
    (root / 'tools/toolkit-probe/README.md').write_text('edited, not committed')
    with pytest.raises(ValueError, match='uncommitted'):
        m.source_snapshot(root)


def test_the_receipt_names_the_vcpkg_checkout_actually_used(tmp_path):
    m = module()
    vcpkg = tmp_path / 'vcpkg'
    (vcpkg / 'scripts/buildsystems').mkdir(parents=True)
    (vcpkg / 'scripts/buildsystems/vcpkg.cmake').write_text('')
    git(vcpkg, 'init', '-q')
    git(vcpkg, 'add', '-A')
    git(vcpkg, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'vcpkg')
    toolchain = (vcpkg / 'scripts/buildsystems/vcpkg.cmake').as_posix()
    assert m.vcpkg_commit({'CMAKE_TOOLCHAIN_FILE': toolchain}) == git(vcpkg, 'rev-parse', 'HEAD').strip()
    assert m.vcpkg_commit({}).startswith('not recorded')
