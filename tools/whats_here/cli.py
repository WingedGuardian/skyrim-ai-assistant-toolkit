"""Read-only command orchestration."""
import argparse
import json
import math
import subprocess
from pathlib import Path
import sys

from .transport import Client, ObservationError

MAX_CHECKS = 12      # --check: at most this many forms, each one full-order xEdit load


def identity(row):
    value = row.get('formId', '')
    try: return format(int(str(value), 16), '08X')
    except ValueError: return str(value)


def observe(client, args):
    if args.command == "watch":
        from .watch import watch
        native = client.tool('inspect', {'kind': 'effects', 'formId': args.actor})
        callback = None if args.json else lambda event: print('Event: ' + json.dumps(event), flush=True)
        inventory = None
        terminal = {'safe': False, 'reason': 'capture not completed'}
        if getattr(args, 'inventory', False):
            from .inventory import context, actor_id, snapshot
            inventory = context(args.actor, args.inventory_limit)
            resolved_actor = actor_id(args.actor, native)
            baseline = snapshot(client, inventory, 'before', resolved_actor)
            result = watch(client, args, callback, terminal_state=terminal)
        else:
            result = watch(client, args, callback)
        result.update(schemaVersion=1, instance=client.identity, native=native)
        result['gaps'].append('native effects snapshot and captured events are not atomic')
        if inventory is not None:
            from .inventory import attach, gap
            if terminal['safe'] and baseline is not None:
                snapshot(client, inventory, 'after', resolved_actor, baseline)
            else:
                gap(inventory, 'after snapshot skipped: ' + (terminal['reason'] if not terminal['safe'] else 'before snapshot unverified'))
            attach(result, inventory)
        return result
    tools = client.tools()
    payload = {'kind': 'effects', 'formId': args.actor} if args.command == 'effects' else {'kind': 'refs', 'limit': args.limit, 'radius': args.radius}
    if args.command == 'ref': payload = {'kind': 'refs', 'formId': args.form_id}
    if args.types and args.command in {'near', 'look'} and ',' not in args.types: payload['formType'] = args.types
    native = client.tool('inspect', payload)
    out = {'schemaVersion': 1, 'ok': True, 'command': args.command, 'instance': client.identity,
           'native': native, 'probe': {'status': 'unavailable'}, 'gaps': ['cross-call observations are not atomic']}
    tool = 'probe.' + args.command
    if tool in tools:
        params = {'look': {'from': args.origin or ('hmd' if client.identity['vr'] else 'crosshair'), 'max': args.max, 'limit': args.limit},
                  'near': {'radius': args.radius, 'at': args.at, 'limit': args.limit},
                  'ref': {'formId': args.form_id}, 'effects': {'actor': args.actor}}[args.command]
        if args.command == 'near' and args.types.strip(): params['types'] = args.types
        probe = client.tool(tool, params)
        if probe.get('schemaVersion') != 1:
            raise ObservationError('Unsupported probe schema')
        out['probe'] = probe
    else:
        out['gaps'].append({'look': 'gaze unavailable', 'near': 'bounds unavailable; native distances use origins',
                            'ref': 'loaded shape textures unavailable', 'effects': 'visual attribution unavailable'}[args.command])
    if args.command == 'near' and args.at != 'player' and tool not in tools:
        out['gaps'].append('native refs are centered on player; requested xyz needs probe')
    if ',' in args.types and tool not in tools:
        out['gaps'].append('multiple form types not filtered by native fallback')
    natives = {identity(row): row for row in native.get('refs', [])}
    probes = out['probe'].get('data', {}).get('refs', [])
    if args.command == 'ref':
        ref = out['probe'].get('data', {}).get('ref')
        probes = [ref] if ref else []
    order = list(dict.fromkeys([identity(r) for r in probes] + list(natives)))
    supplemental = {identity(r): r for r in probes}
    for candidate in probes[:args.limit]:
        if identity(candidate) not in natives:
            detail = client.tool('inspect', {'kind': 'refs', 'formId': candidate['formId']})
            natives.update({identity(row): row for row in detail.get('refs', [])})
    out['refs'] = [{'formId': key, 'native': natives.get(key), 'probe': supplemental.get(key)} for key in order]
    out['forms'] = {}
    ids = ([args.form_id] if args.command == 'ref' else [])
    if args.textures or args.check:
        ids += [row['formId'] for row in out['refs']]
        for effect in native.get('activeEffects', []):
            ids += [form['formId'] for key in ('spell', 'effect') if isinstance((form := effect.get(key)), dict) and form.get('formId')]
    base_ids = set()
    if args.check:
        for row in out['refs']:
            for record in (row.get('native') or {}, row.get('probe') or {}):
                base = record.get('base') or {}
                base_id = base.get('formId') or record.get('baseFormId')
                if base_id:
                    canonical = identity({'formId': base_id})
                    base_ids.add(canonical)
                    ids.append(canonical)
    unique_ids = {}
    for form_id in ids: unique_ids.setdefault(identity({'formId': form_id}), form_id)
    for form_id in unique_ids.values():
        out['forms'][form_id] = client.tool('forms', {'action': 'describe', 'formId': form_id})
    if args.check:
        from .enrichment import check_chain, runtime_identity
        try: out['runtimePluginMap'] = client.tool('inspect', {'kind': 'mods'})
        except ObservationError as exc: out['runtimePluginMap'] = {'error': str(exc)}
        reference_ids = {row['formId'] for row in out['refs']}
        if args.command == 'ref': reference_ids.add(identity({'formId': args.form_id}))
        out['checks'] = []
        for number, (key, form) in enumerate(out['forms'].items()):
            if number >= MAX_CHECKS:
                # Each check loads the whole load order in xEdit (seconds and ~1 GB each) while the game runs.
                out['checks'].append({'formId': key, 'status': 'not checked',
                                      'reason': 'over the %d-form --check limit; ask for this form with `ref`' % MAX_CHECKS})
                continue
            canonical = identity({'formId': key})
            resolved = runtime_identity(key, form, out['runtimePluginMap'])
            roles = (['reference'] if canonical in reference_ids else []) + (['base'] if canonical in base_ids else [])
            check = dict(formId=key, roles=roles or ['effect'], runtimeIdentity=resolved,
                         offlineAuthority='skyrim-winner configured active file load order; may differ from runtime',
                         **check_chain(client.root, form, resolved))
            out['checks'].append(check)
        out['validationIncomplete'] = not out['checks'] or any(check['status'] != 'checked' for check in out['checks'])
        if out['validationIncomplete']: out['gaps'].append('offline validation incomplete; inspect failed or unavailable checks')
    if args.textures:
        from .enrichment import gateway, classify_assets, nif_textures, gateway_text, texture_path
        paths = []
        def collect(value, key=''):
            if isinstance(value, dict):
                for k, v in value.items(): collect(v, k)
            elif isinstance(value, list):
                for v in value: collect(v, key)
            elif isinstance(value, str) and value.lower().endswith(('.nif', '.dds')):
                paths.append(texture_path(value, 'meshes' if value.lower().endswith('.nif') else 'textures'))
        collect(native)
        collect([row.get('native') for row in out['refs']])
        collect(out['forms'])
        collect(out['probe'].get('data', {}))
        paths = list({p.replace('\\', '/').casefold(): p for p in paths}.values())
        meshes = [p for p in paths if p.lower().endswith('.nif')]
        if meshes:
            try:
                text = gateway_text(gateway(client.root, 'housecarl_nif_inspect', {'mesh_paths': meshes[:50], 'sections': 'paths', 'max_chars': 1000000}))
                out['offlineTextures'] = nif_textures(text, meshes)
                paths += [use['path'] for use in out['offlineTextures']['uses']]
                paths = list({p.replace('\\', '/').casefold(): p for p in paths}.values())
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                out['offlineTextures'] = {'status': 'not checked', 'error': str(exc), 'unanswered': meshes}
        if paths:
            try:
                envelope = gateway(client.root, 'housecarl_asset_status', {'asset_paths': paths[:500], 'format': 'json', 'max_chars': 1000000})
                out['assets'] = classify_assets(paths, envelope)
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                out['assets'] = {'assets': [{'path': p, 'status': 'not checked'} for p in paths], 'error': str(exc)}
        else: out['assets'] = {'assets': [], 'status': 'not checked; no paths observed'}
        out['gaps'].append('offline texture paths do not establish runtime bindings; provider candidates do not establish runtime winners')
    if getattr(args, 'inventory', False):
        from .inventory import context, actor_id, snapshot, attach
        inventory = context(args.actor, args.inventory_limit)
        snapshot(client, inventory, 'current', actor_id(args.actor, native))
        attach(out, inventory)
    if args.command == 'look':
        from .enrichment import external_pick
        pick = external_pick(client.root)
        if pick:
            out['externalPick'] = pick
            out['gaps'].append('external pick (What Am I Looking At) is a separate observation: not cross-checked against this look')
    return out


def render_reference_details(native, probe, args):
    """Keep observed reference/base provenance and shape uses visible in text."""
    if args.command == 'ref': print('    Native reference detail: ' + json.dumps(native))
    if not probe: return
    files = probe.get('runtimeSourceFiles')
    print('    Reference runtime files: ' + (' -> '.join(files) if files else 'not recorded' if files == [] else 'not checked'))
    base = probe.get('base', {})
    base_id = base.get('formId', probe.get('baseFormId'))
    if base_id:
        files = base.get('runtimeSourceFiles')
        print('    Base ' + str(base_id) + ' runtime files: ' + (' -> '.join(files) if files else 'not recorded' if files == [] else 'not checked'))
    if probe.get('mesh'): print('    Mesh: ' + probe['mesh'] + ' (probe)')
    elif native.get('model'): print('    Mesh: ' + native['model'] + ' (native)')
    for key in ('formType', 'editorId', 'loaded3D', 'disabled', 'cell', 'position', 'bounds', 'distance', 'boundMethod'):
        if key in probe: print('    ' + key + ' (probe): ' + json.dumps(probe[key]))
    if args.command == 'ref' or args.textures:
        for shape in probe.get('shapes', []):
            print('    Shape ' + str(shape.get('shape', 'unknown')) + ' (probe)')
            for key in ('shaderType', 'shaderFeature', 'bounds'):
                if key in shape: print('      ' + key + ': ' + json.dumps(shape[key]))
            for texture in shape.get('textures', []):
                print('      slot ' + str(texture.get('slot', 'unknown')) + ': ' + str(texture.get('path', 'not checked')) + ' [' + str(texture.get('source', 'source not checked')) + ']')


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=['look', 'near', 'ref', 'effects', 'watch'])
    ap.add_argument('form_id', nargs='?')
    ap.add_argument('--game-dir', default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--timeout', type=float, default=5)
    ap.add_argument('--from', dest='origin', choices=['hmd', 'right', 'left', 'crosshair'])
    ap.add_argument('--max', type=float, default=2000)
    ap.add_argument('--radius', type=float, default=500)
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--at', default='player')
    ap.add_argument('--types', default='')
    ap.add_argument('--actor', default='0x14')
    ap.add_argument('--inventory', action='store_true')
    ap.add_argument('--inventory-limit', type=int, default=None)
    ap.add_argument('--seconds', type=float, default=180)
    ap.add_argument('--textures', action='store_true')
    ap.add_argument('--check', action='store_true')
    return ap


def main(argv=None):
    ap = parser()
    args = ap.parse_args(argv)
    try:
        if args.inventory and args.command not in {'effects', 'watch'}:
            raise ObservationError('--inventory requires effects or watch')
        if args.inventory_limit is not None and not args.inventory:
            raise ObservationError('--inventory-limit requires --inventory')
        if args.inventory_limit is not None and not 1 <= args.inventory_limit <= 1000:
            raise ObservationError('--inventory-limit must be 1..1000')
        if args.inventory: args.inventory_limit = args.inventory_limit if args.inventory_limit is not None else 500
        if not 0 < args.timeout <= 60 or not 1 <= args.limit <= 100 or not 0 < args.radius <= 10000 or not 0 < args.max <= 10000:
            raise ObservationError('Invalid timeout, result limit or distance')
        if args.command == 'ref' and not args.form_id: raise ObservationError('ref requires a form ID')
        if not 0 < args.seconds <= 600: raise ObservationError('Invalid watch duration')
        if args.at != 'player':
            xyz = [float(value) for value in args.at.split(',')]
            if len(xyz) != 3 or not all(math.isfinite(v) for v in xyz): raise ObservationError('at requires player or finite x,y,z')
            args.at = xyz
        client = Client(args.game_dir, args.timeout)
        result = observe(client, args)
        # 0 complete, 3 observed but incomplete (a partial, superseded or interrupted watch; a --check that did not
        # finish), 2 failed. A script reading only the exit code must not take a partial capture as a full one.
        code = (3 if result.get('incomplete') or result.get('validationIncomplete') else 0) if result.get('ok') else 2
    except (ObservationError, OSError, ValueError) as exc:
        result, code = {'ok': False, 'error': str(exc)}, 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if not result.get('ok'): print('Error: ' + result['error'])
        if result.get('ok') or result.get('snapshots') or 'inventoryContext' in result:
            print(args.command + ': observed via DevBench')
            for row in result.get('refs', []):
                native = row.get('native') or {}
                probe = row.get('probe') or {}
                context = ' [native origin candidate; gaze not established]' if args.command == 'look' and not row.get('probe') else (' [native origin candidate]' if args.command == 'near' and not row.get('probe') else '')
                print('  ' + row['formId'] + ' ' + native.get('name', native.get('editorId', '')) + (' [shape: ' + str(probe['shape']) + ']' if probe.get('shape') else '') + context)
                render_reference_details(native, probe, args)
            for form_id, form in result.get('forms', {}).items():
                print('  ' + form_id + ' native form: ' + json.dumps(form))
            for effect in result.get('native', {}).get('activeEffects', []): print('  Effect: ' + json.dumps(effect))
            for visual in result.get('probe', {}).get('data', {}).get('visuals', []): print('  Attached visual (probe): ' + json.dumps(visual))
            for effect in result.get('probe', {}).get('data', {}).get('effects', []): print('  Supplemental effect (probe): ' + json.dumps(effect))
            for key in ('effectsCoverage', 'visualsCoverage'):
                if key in result.get('probe', {}).get('data', {}): print('Probe ' + key + ': ' + json.dumps(result['probe']['data'][key]))
            for key, label in [('maxDeliveryDelayMs', 'Maximum event delivery delay (ms)'), ('maxDrainMs', 'Maximum native drain (ms)'), ('drainBudgetStops', 'Native drain budget stops'), ('queueHighWaterMark', 'Global pending queue high-water mark experienced')]:
                if key in result: print(label + ': ' + str(result[key]))
            if result.get('visualSampling'): print('Visual sampling: ' + str(result['visualSampling']) + '; coalesced observations may miss transitions between drains, and callback timestamps can precede delivery')
            if result.get('activeDrainCyclesExhausted'): print('Active drain cycle cutoffs: ' + str(result['activeDrainCyclesExhausted']) + '; backlog resumes at normal polls: ' + json.dumps(result['activeDrainCutoffs']))
            if 'terminalPageReads' in result: print('Terminal drain responses by request intent: ' + str(result['terminalPageReads']) + ' published-pagination, ' + str(result['terminalPendingReads']) + ' pending-assist (including initial; bounded, remaining work may be unobserved)')
            for visual_status in result.get('visualStatuses', []): print('Visual status: ' + visual_status)
            if result.get('eventFailures'): print('Gap: capture event failures: ' + str(result['eventFailures']))
            if result.get('visualEventsDropped'): print('Gap: visual events dropped: ' + str(result['visualEventsDropped']))
            if result.get('eventBoundaryFailuresDelta') is not None: print('Global event boundary failures since start (all captures): ' + str(result['eventBoundaryFailuresDelta']))
            if result.get('pendingEvents'): print('Gap: pending events remain: ' + str(result['pendingEvents']))
            for coverage in result.get('captureCoverage', []): print('Capture coverage: ' + json.dumps(coverage))
            if result.get('captureTruncated'): print('Gap: capture truncated')
            if result.get('eventQueueDropped'): print('Gap: native event queue dropped: ' + str(result['eventQueueDropped']))
            if result.get('dropped'): print('Ring retention evictions: ' + str(result['dropped']) + ' (event loss requires cursor/queue/failure evidence)')
            if args.command != 'watch':
                for event in result.get('events', []): print('  Event: ' + json.dumps(event))
            if 'summary' in result: print('Sources: ' + json.dumps(result['summary']))
            for form_id, details in result.get('sources', {}).items(): print('Source details ' + form_id + ': ' + json.dumps(details))
            for row in result.get('visualAttribution', []):
                said = ('inferred from timing: ' + str(row['inferredSource'])) if row.get('inferredSource') else (
                    'tied, timing cannot choose: ' + ', '.join(row['tiedSources']) if row.get('tiedSources') else 'unknown')
                print('Visual ' + str(row.get('visualForm')) + ' (effect ' + str(row.get('effectId')) + '): ' + said)
            for asset in result.get('assets', {}).get('assets', []): print('  Asset ' + asset['status'] + ': ' + asset['path'])
            offline = result.get('offlineTextures', {})
            if offline.get('status'): print('Offline NIF status: ' + offline['status'])
            if offline.get('error'): print('Gap: offline NIF inspection: ' + offline['error'])
            for use in result.get('offlineTextures', {}).get('uses', []): print('  Offline NIF use: ' + use['mesh'] + ' [' + str(use['shape']) + '] -> ' + use['path'] + ' (' + use['source'] + ')')
            for mesh in result.get('offlineTextures', {}).get('unanswered', []): print('Gap: offline NIF not checked: ' + mesh)
            if result.get('validationIncomplete'): print('Validation incomplete: requested offline checks did not all complete')
            for check in result.get('checks', []): print('  Offline check: ' + json.dumps(check))
            if result.get('externalPick'):
                pick = result['externalPick']
                print('External pick (' + pick['path'] + ', ' + str(pick['ageSeconds']) + ' s old, ' + pick['status'] + '): '
                      + json.dumps(pick.get('content', pick.get('reason'))))
            if 'inventoryContext' in result:
                print('Inventory context: ' + json.dumps(result['inventoryContext']))
            for gap in result.get('gaps', []): print('Gap: ' + gap)
            if result.get('probe', {}).get('coverage'): print('Probe coverage: ' + json.dumps(result['probe']['coverage']))
            if result.get('probe', {}).get('truncated'): print('Gap: probe results truncated')
            if 'error' in result.get('assets', {}): print('Gap: ' + result['assets']['error'])
    return code
