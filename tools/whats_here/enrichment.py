"""House Carl discovery keeps uncertainty separate from absence."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def norm(path):
    return path.replace('\\', '/').casefold()


def texture_path(path, folder='textures'):
    r"""Data-relative `textures/...` (or `meshes/...`) form of a NIF or engine path.

    NIFs carry `textures\x.dds`, `x.dds`, `\textures\x.dds`, `Data\Textures\x.dds` or an absolute path that runs
    through `...\Data\textures\`. Prefixing `textures/` onto the last three gave a path no archive or folder holds,
    reported as missing."""
    parts = [part for part in path.replace('\\', '/').strip().split('/') if part]
    folded = [part.casefold() for part in parts]
    if folder in folded:
        return '/'.join(parts[folded.index(folder):])      # keeps the path's own spelling; lookups casefold
    if parts and parts[0].endswith(':'):
        return '/'.join(parts)        # absolute, outside any textures folder: left as given, never guessed
    if len(parts) > 1 and folded[0] == 'data':
        parts = parts[1:]
    return '/'.join([folder] + parts)


def run_hidden(argv, timeout, **kwargs):
    """subprocess.run with no console window that kills the child's whole tree on timeout.

    House Carl re-executes into its venv and skyrim-winner runs bash -> node: killing only the direct child on
    timeout left those running (and holding the pipes, so the call did not return either)."""
    import os
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    extra = {'creationflags': flags} if os.name == 'nt' else {'creationflags': flags, 'start_new_session': True}
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **extra, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name == 'nt':
                taskkill = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'taskkill.exe')
                subprocess.run([taskkill, '/F', '/T', '/PID', str(proc.pid)], capture_output=True, timeout=30,
                               creationflags=flags)
            elif proc.pid > 1:
                # Never 0 or 1: killpg(1) is kill(-1), every process this user may signal (a test stub's pid 1
                # took down the CI runner). A real child started with start_new_session leads its own group.
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def gateway(root, tool, arguments):
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as stream:
        json.dump(arguments, stream)
        name = stream.name
    try:
        process = run_hidden([sys.executable, str(Path(root) / 'tools/housecarl.py'), '--root', str(root), 'call', tool, '--arguments-file', name], 60)
    finally:
        Path(name).unlink()
    if process.returncode:
        raise RuntimeError('House Carl refused or failed: ' + (process.stderr or process.stdout)[-500:])
    envelope = json.loads(process.stdout)
    result = envelope.get('result', {})
    if result.get('isError') or result.get('is_error'):
        raise RuntimeError('House Carl returned an error')
    return envelope


def classify_assets(paths, envelope):
    body = json.loads(''.join(x.get('text', '') for x in envelope.get('result', {}).get('content', []) if x.get('type') == 'text'))
    rows = {norm(row['path']): row for row in body.get('results', [])}
    conclusive = envelope.get('absence_conclusive') is True and not body.get('read_incomplete') and not body.get('bsa_failures')
    assets = []
    for path in paths:
        row = rows.get(norm(path), {})
        exists = row.get('exists')
        status = 'present' if exists is True else ('missing' if conclusive else 'inconclusive') if exists is False else 'not checked'
        assets.append({'path': path, 'status': status, 'discovery': row, 'providerMeaning': 'candidate; runtime winner not established'})
    return {'assets': assets, 'absence_conclusive': conclusive, 'context': envelope.get('context', {}), 'raw': body}


def nif_textures(text, meshes):
    import re
    names = {norm(path): path for path in meshes}
    mesh, shape = None, None
    answered, uses, absent = set(), [], []
    for line in text.splitlines():
        if not line.startswith(' ') and norm(line.strip()).endswith('.nif'):
            mesh, shape = names.get(norm(line.strip())), None
        elif mesh and line.startswith('  version:'):
            answered.add(mesh)
        elif mesh and line.strip().startswith('ABSENT'):
            answered.add(mesh)
            absent.append(mesh)
        elif mesh:
            match = re.match(r"^  '(.*)':\s*$", line)
            if match: shape = match.group(1)
            match = re.match(r'^\s+tex\[\d+\](?: \([^)]*\))?: (\S.*?)\s*$', line)
            if match:
                path = texture_path(match.group(1))
                uses.append({'mesh': mesh, 'shape': shape, 'path': path, 'source': 'offline NIF; runtime binding not established'})
    return {'uses': uses, 'unanswered': [path for path in meshes if path not in answered], 'absent': absent}


def gateway_text(envelope):
    return ''.join(x.get('text', '') for x in envelope.get('result', {}).get('content', []) if x.get('type') == 'text')


def check_chain(root, form, resolved=None):
    import os
    import shutil
    if not resolved or resolved.get('status') != 'resolved':
        return {'status': 'not checked', 'reason': (resolved or {}).get('reason', 'observed runtime plugin map unavailable; reported plugin is not a defining-file guarantee')}
    plugin, local = resolved['plugin'], resolved['localId']
    bash = os.environ.get('TOOLKIT_BASH')
    if not bash:
        git_bash = Path('C:/Program Files/Git/bin/bash.exe')
        bash = str(git_bash) if git_bash.exists() else shutil.which('bash')
    if not bash: return {'status': 'not checked', 'reason': 'bash unavailable'}
    try:
        proc = run_hidden([bash, str(Path(root) / 'tools/skyrim-winner.sh'), 'chain', local, plugin], 60, cwd=root)
        result = {'status': 'checked' if proc.returncode == 0 else 'failed', 'plugin': plugin, 'localId': local,
                  'offlineOutput': proc.stdout, 'stderr': proc.stderr, 'comparison': 'not automatically reconciled; inspect both sources'}
        if proc.returncode == 0:
            # The game says `plugin` defines this form. An offline answer about a record another file defines (a
            # master record this plugin overrides) answers a different question: never "checked".
            defines = [line.split('.', 1)[1].split('[defines]')[0].strip() for line in proc.stdout.splitlines()
                       if '[defines]' in line and '.' in line]
            if not defines or defines[0].casefold() != plugin.casefold():
                result.update(status='failed', reason='offline chain names %s as the defining file, not %s: it traced a different record'
                              % (defines[0] if defines else 'no file', plugin))
        return result
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'status': 'failed', 'error': str(exc)}


def runtime_identity(form_id, form, mods):
    """Resolve a runtime address only through the answering game's index map.

    DevBench 1.35 inspect mods uses TESDataHandler full/light accessors and
    compile indices (including VR ESL redirection), not array order. GetFile(0)
    on a form may be an override; its reported plugin/localId are not this map.
    """
    import re
    unavailable = lambda reason: {'status': 'not checked', 'reason': reason, 'source': 'DevBench inspect mods'}
    try:
        if not re.fullmatch(r'(?:0[xX])?[0-9a-fA-F]{1,8}', str(form_id)):
            return unavailable('not a bounded runtime FormID')
        value = int(str(form_id), 16)
        prefix = value >> 24
        if prefix == 255: return unavailable('dynamic runtime form has no defining plugin index')
        spaces = []
        names = set()
        for array_key, count_key, ceiling in [('plugins', 'count', 255), ('lightPlugins', 'lightCount', 4096)]:
            rows, count = mods.get(array_key), mods.get(count_key)
            if not isinstance(rows, list) or type(count) is not int or not 0 <= count <= ceiling or len(rows) != count:
                return unavailable('runtime plugin map unavailable or incomplete')
            space = {}
            for row in rows:
                index, name = row.get('index'), row.get('name')
                if type(index) is not int or not 0 <= index < ceiling or index in space:
                    return unavailable('runtime plugin indices invalid or ambiguous')
                if not isinstance(name, str) or not name or name.strip() != name or any(c in name for c in '/\\:') or not name.lower().endswith(('.esm', '.esp', '.esl')) or name.casefold() in names:
                    return unavailable('runtime plugin names invalid or ambiguous')
                names.add(name.casefold())
                space[index] = name
            spaces.append(space)
        if type(mods.get('total')) is not int or mods['total'] != mods['count'] + mods['lightCount']:
            return unavailable('runtime plugin total is incomplete')
        full, light = spaces
        if prefix == 254 and 254 in full and light:
            return unavailable('FE full/light index space is ambiguous')
        kind = 'light' if prefix == 254 and 254 not in full else 'full'
        index = (value >> 12) & 0xFFF if kind == 'light' else prefix
        local = value & (0xFFF if kind == 'light' else 0xFFFFFF)
        plugin = (light if kind == 'light' else full).get(index)
        if plugin is None: return unavailable('runtime plugin index was not observed')
        try: reported_local = int(str(form.get('localId')), 16) if not isinstance(form.get('localId'), int) else form['localId']
        except (TypeError, ValueError): reported_local = None
        matches = isinstance(form.get('plugin'), str) and form['plugin'].casefold() == plugin.casefold() and reported_local == local
        return {'status': 'resolved', 'formId': format(value, '08X'), 'plugin': plugin,
                'localId': format(local, '06X'), 'kind': kind, 'index': index,
                'source': 'DevBench inspect mods runtime compile index', 'reportedIdentityMatches': matches}
    except (AttributeError, TypeError, ValueError):
        return unavailable('runtime plugin map has an unsupported shape')


def external_pick(root, now=None, limit=65536):
    """The newest `last-pick.json` under Data/SKSE/Plugins, as written by "What Am I Looking At" (Nexus 199906,
    SE/AE): the object the user clicked in the console. Its schema is not published, so the content is passed on
    as the file says, labelled unchecked, never merged into this tool's own fields."""
    import time
    plugins = Path(root) / 'Data/SKSE/Plugins'
    if not plugins.is_dir():
        return None
    try:
        found = []
        for depth in ('last-pick.json', '*/last-pick.json', '*/*/last-pick.json'):
            found += [path for path in plugins.glob(depth) if path.is_file()]
        if not found:
            return None
        path = max(found, key=lambda item: item.stat().st_mtime)
        stat = path.stat()
    except OSError as exc:            # locked, or replaced while being looked at: report it, never fail look
        return {'source': 'What Am I Looking At (external mod; schema not checked by this toolkit)',
                'status': 'not read', 'reason': type(exc).__name__}
    out = {'source': 'What Am I Looking At (external mod; schema not checked by this toolkit)',
           'path': path.relative_to(root).as_posix(), 'ageSeconds': round((now or time.time()) - stat.st_mtime, 1)}
    if stat.st_size > limit:
        return dict(out, status='not read', reason='larger than %d bytes' % limit)
    try:
        return dict(out, status='read', content=json.loads(path.read_text(encoding='utf-8-sig')))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return dict(out, status='not read', reason=type(exc).__name__)
