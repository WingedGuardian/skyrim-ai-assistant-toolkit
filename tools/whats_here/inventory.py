"""Optional, bounded native inventory observations; never part of capture polling."""
from datetime import datetime, timezone
import math
import re

from .transport import ObservationError


def form_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:0[xX])?[0-9a-fA-F]{1,8}', value):
        return None
    return format(int(value, 16), '08X')


def actor_id(actor, native):
    resolved = native.get('actor')
    resolved = form_id(resolved.get('formId')) if isinstance(resolved, dict) else None
    requested = form_id(actor)
    if requested and resolved and requested != resolved:
        return None
    return requested or resolved


def health_identity(value, port):
    if not isinstance(value, dict) or type(value.get('pid')) is not int or value['pid'] <= 0 or not isinstance(value.get('exe'), str) or not value['exe'] or type(value.get('vr')) is not bool:
        raise ObservationError('inventory process identity unavailable')
    result = {key: value[key] for key in ('pid', 'exe', 'vr')}
    result['port'] = value.get('port', port)
    for key in ('sessionId', 'generation', 'sessionGeneration'):
        if key in value:
            result[key] = value[key]
    return result


def finite(value):
    if isinstance(value, dict): return all(finite(v) for v in value.values())
    if isinstance(value, list): return all(finite(v) for v in value)
    return not isinstance(value, float) or math.isfinite(value)


def validate(raw, limit, actor):
    if not isinstance(raw, dict) or not finite(raw):
        raise ObservationError('malformed inventory response')
    items = raw.get('items')
    count, returned, truncated = raw.get('count'), raw.get('returned'), raw.get('truncated')
    if (not isinstance(items, list) or type(count) is not int or count < 0 or type(returned) is not int or returned < 0 or returned > limit or returned != len(items) or returned > count or type(truncated) is not bool or truncated != (count > returned)):
        raise ObservationError('malformed inventory counts/items/truncation')
    for item in items:
        if not isinstance(item, dict) or not form_id(item.get('formId')) or type(item.get('count')) is not int or item['count'] <= 0:
            raise ObservationError('malformed inventory item')
        for key in ('value', 'weight'):
            if key in item and (type(item[key]) not in (int, float) or (type(item[key]) is float and not math.isfinite(item[key]))):
                raise ObservationError('malformed inventory item ' + key)
        if 'equipped' in item and type(item['equipped']) is not bool:
            raise ObservationError('malformed inventory item equipped')
    owner = raw.get('owner')
    owner = form_id(owner.get('formId')) if isinstance(owner, dict) else None
    if not actor or not owner:
        raise ObservationError('inventory actor identity unverified')
    if owner != actor:
        raise ObservationError('inventory owner differs from effects actor')


def context(actor, limit):
    return {'complete': True, 'requestedActor': actor, 'limit': limit, 'snapshots': [], 'gaps': []}


def gap(out, reason):
    out['complete'] = False
    out['gaps'].append(str(reason))


def snapshot(client, out, phase, actor, baseline=None):
    row = {'phase': phase, 'timestamp': datetime.now(timezone.utc).isoformat(), 'source': 'DevBench inspect inventory', 'complete': False}
    out['snapshots'].append(row)
    try:
        port = getattr(client, 'port', None)
        expected = health_identity(client.identity, port)
        before = client.request('/api/health')
        row['healthBefore'] = before
        before_id = health_identity(before, port)
        if before_id != expected or (baseline is not None and before_id != baseline):
            raise ObservationError('inventory process/session identity changed before read')
        raw = client.tool('inspect', {'kind': 'inventory', 'formId': out['requestedActor'], 'limit': out['limit']})
        row['raw'] = raw
        after = client.request('/api/health')
        row['healthAfter'] = after
        if health_identity(after, port) != before_id:
            raise ObservationError('inventory process/session identity changed during read')
        validate(raw, out['limit'], actor)
        if raw['truncated']:
            row['error'] = 'inventory truncated at requested limit'
            gap(out, row['error'])
        else:
            row['complete'] = True
        return before_id
    except (ObservationError, OSError, ValueError, TypeError) as exc:
        row['error'] = str(exc)
        gap(out, exc)
        return None


def attach(result, out):
    result['inventoryContext'] = out
    result.setdefault('gaps', []).extend('inventory: ' + reason for reason in out['gaps'])
    result['gaps'].append('inventory snapshots are bounded carried-item context, not unique item-copy identity or causal attribution; cross-call observations are not atomic')
