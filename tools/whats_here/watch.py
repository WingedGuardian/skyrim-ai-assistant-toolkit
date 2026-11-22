"""Capture ownership, cursors and bounded five-second reads."""
from collections import Counter
import time
import math

from .transport import ObservationError
from .visual_attribution import attribute_visuals


def watch(client, args, on_event=None, terminal_state=None):
    # Private enrichment signal; default public results remain unchanged.
    def unsafe(reason):
        if terminal_state is not None: terminal_state.update(safe=False, reason=reason)
    unsafe('watch unavailable or capture not completed')
    if 'probe.watch' not in client.tools():
        return {'ok': True, 'incomplete': True, 'events': [], 'summary': {}, 'gaps': ['watch unavailable; ToolkitProbe required']}
    out = {'ok': True, 'events': [], 'summary': {}, 'gaps': [], 'snapshots': [], 'captureCoverage': [], 'captureTruncated': False, 'dropped': 0, 'eventQueueDropped': 0, 'eventFailures': 0, 'visualEventsDropped': 0, 'visualStatuses': [], 'pendingEvents': 0, 'incomplete': False, 'activeDrainCyclesExhausted': 0, 'activeDrainCutoffs': [], 'terminalPageReads': 0, 'terminalPendingReads': 0, 'normalPolls': 0}
    owned = None
    seen = set()
    generation = None
    session_id = None
    deadline = None
    after = 0
    active_cycle_deadline = None
    active_cycle_responses = 0
    next_poll = None
    last_latest = None
    drain_deadline = None
    stop_sent = False
    response_intent = None

    def verify_lifecycle(response):
        if terminal_state is None: return True
        session = response.get('sessionId')
        current_generation = response.get('generation', response.get('sessionGeneration'))
        valid = (isinstance(session, str) and bool(session.strip()) and
                 type(current_generation) is int and current_generation >= 0)
        if not valid: unsafe('watch lifecycle identity unavailable or malformed')
        return valid

    def verify_cleanup(response):
        if terminal_state is None: return
        verify_lifecycle(response)
        data = response.get('data')
        if response.get('schemaVersion') != 1 or not isinstance(data, dict):
            unsafe('owned watch cleanup identity unavailable')
            return
        if (data.get('captureId') != owned or
                response.get('sessionId') != session_id or
                response.get('generation', response.get('sessionGeneration')) != generation or
                data.get('status') in {'invalidated', 'superseded'}):
            unsafe('owned watch cleanup identity changed or invalidated')

    def bounded_request(body, until, budget_label="final drain"):
        previous = getattr(client, 'timeout', 5)
        remaining = until - time.monotonic()
        if remaining <= 0: raise ObservationError('watch ' + budget_label + ' deadline reached; events remain')
        client.timeout = min(previous, remaining)
        try: return client.tool('probe.watch', body)
        finally: client.timeout = previous
    def normal_poll():
        nonlocal next_poll
        time.sleep(max(0, min(next_poll, deadline) - time.monotonic()))
        response = client.tool('probe.watch', {'read': True, 'captureId': owned, 'after': after})
        next_poll = time.monotonic() + 5
        out['normalPolls'] += 1
        return response

    def active_cutoff(reason, data, error=None):
        nonlocal active_cycle_deadline, active_cycle_responses
        row = {'reason': reason, 'pendingEvents': out['pendingEvents'], 'after': after,
               'latestSequence': data.get('latestSequence'), 'responses': active_cycle_responses}
        if error is not None: row['error'] = str(error)
        out['activeDrainCutoffs'].append(row)
        out['activeDrainCyclesExhausted'] += 1
        active_cycle_deadline, active_cycle_responses = None, 0

    try:
        response = client.tool('probe.watch', {'actor': args.actor, 'seconds': math.ceil(args.seconds)})
        boundary_initial = response.get('eventBoundaryFailures')
        out['eventBoundaryFailuresDelta'] = 0 if isinstance(boundary_initial, int) else None
        deadline = time.monotonic() + args.seconds
        next_poll = time.monotonic() + 5
        while True:
            if response.get('schemaVersion') != 1: raise ObservationError('Unsupported probe schema')
            lifecycle_verified = verify_lifecycle(response)
            data = response.get('data', {})
            capture = data.get('captureId')
            if owned is None:
                if terminal_state is not None and lifecycle_verified:
                    terminal_state.update(safe=True, reason='')
                owned = capture
                if not isinstance(owned, str) or not owned: raise ObservationError('Watch returned no capture identity; no stop sent')
                session_id = response.get('sessionId')
                generation = response.get('generation', response.get('sessionGeneration'))
            if response.get('sessionId') != session_id:
                unsafe('watch process session changed')
                out['gaps'].append('watch process session changed')
                owned = None  # The restarted process may reuse capture IDs; it owns no capture for us.
                break
            if capture != owned:
                unsafe('watch superseded; foreign capture ignored')
                out['gaps'].append('watch superseded; foreign capture ignored')
                break
            current = response.get('generation', response.get('sessionGeneration'))
            if current != generation:
                unsafe('watch generation changed; restart or load')
                out['gaps'].append('watch generation changed; restart or load')
                break
            if not data.get('active', False) or stop_sent or drain_deadline is not None:
                # Account completed responses, including the first natural/stop
                # reply. Later replies retain the intent that requested them:
                # incidental rows do not renew or transfer either allowance.
                if response_intent is None:
                    response_intent = ('pending' if data.get('pendingEvents', 0) and not data.get('events') and not data.get('hasMore') else 'published')
                out['terminalPendingReads' if response_intent == 'pending' else 'terminalPageReads'] += 1
            out['snapshots'].append(response)
            coverage = response.get('coverage')
            if coverage is not None and coverage not in out['captureCoverage']: out['captureCoverage'].append(coverage)
            out['captureTruncated'] = out['captureTruncated'] or bool(response.get('truncated'))
            out['dropped'] = max(out['dropped'], data.get('dropped', 0))
            previous_pending = out['pendingEvents'] if len(out['snapshots']) > 1 else None
            pending = data.get('pendingEvents', 0)
            if type(pending) is not int or pending < 0:
                raise ObservationError('pendingEvents must be a nonnegative integer')
            out['pendingEvents'] = pending
            pending_reduced = previous_pending is not None and pending < previous_pending
            previous_latest = last_latest
            latest = data.get('latestSequence')
            if 'latestSequence' in data:
                if type(latest) is not int or latest < after or latest < 0 or (last_latest is not None and latest < last_latest):
                    raise ObservationError('latestSequence must be a nonnegative monotonic integer at or beyond the consumed cursor')
                last_latest = latest
            publication_progress = latest is not None and previous_latest is not None and latest > previous_latest
            for key in ('maxDeliveryDelayMs', 'maxDrainMs', 'drainBudgetStops', 'queueHighWaterMark'):
                value = data.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    out[key] = max(out.get(key, 0), value)
            if 'visualSampling' in data: out['visualSampling'] = data['visualSampling']
            for key in ('eventFailures', 'visualEventsDropped'):
                out[key] = max(out[key], data.get(key, 0))
            visual_status = data.get('visualStatus')
            if visual_status is not None and visual_status not in out['visualStatuses']: out['visualStatuses'].append(visual_status)
            boundary_now = response.get('eventBoundaryFailures')
            if out['eventBoundaryFailuresDelta'] is not None and isinstance(boundary_now, int):
                out['eventBoundaryFailuresDelta'] = max(out['eventBoundaryFailuresDelta'], boundary_now - boundary_initial)
            out['eventQueueDropped'] = max(out['eventQueueDropped'], data.get('eventQueueDropped', 0))
            if data.get('visualSnapshotComplete') is False and 'visual snapshot incomplete' not in out['gaps']:
                out['gaps'].append('visual snapshot incomplete')
            if data.get('cursorLost'): out['gaps'].append('watch cursor lost events')
            page_start = after
            for event in data.get('events', []):
                sequence = event.get('sequence')
                if not isinstance(sequence, int):
                    out['gaps'].append('event missing sequence')
                    continue
                if sequence not in seen:
                    seen.add(sequence)
                    out['events'].append(event)
                    if on_event is not None: on_event(event)
                    after = max(after, sequence)
            if latest is not None and latest < after:
                raise ObservationError('latestSequence precedes delivered event rows')
            status = data.get('status')
            if status in {'invalidated', 'superseded'}:
                unsafe('watch ' + status)
                out['gaps'].append('watch ' + status)
            now = time.monotonic()
            if not data.get('active', False) and drain_deadline is None:
                drain_deadline = now + 5
            if data.get('active', False) and now >= deadline and not stop_sent:
                drain_deadline = now + 5
                stop_sent = True
                response = bounded_request({'stop': True, 'captureId': owned, 'after': after}, drain_deadline)
                out['stop'] = response
                verify_cleanup(response)
                # Older/fixture acknowledgements may not contain a capture page.
                if response.get('schemaVersion') != 1: break
                continue
            has_more = bool(data.get('hasMore'))
            pending_progress_page = False
            if has_more:
                cursor = data.get('nextSequence')
                if type(cursor) is not int or cursor < page_start or cursor < after:
                    out['gaps'].append('watch pagination made no progress')
                    break
                if cursor == page_start:
                    # Native reads page before draining. Accepted pending work can
                    # publish rows only after that page was made; fetch them next.
                    # A raised watermark is publication, never proof of delivery.
                    if not (pending_reduced or publication_progress):
                        out['gaps'].append('watch pagination made no progress')
                        break
                    pending_progress_page = True
                after = cursor
            backlog = has_more or pending > 0
            terminal = not data.get('active', False) or stop_sent or drain_deadline is not None
            if terminal:
                if not backlog: break
                if has_more:
                    if out['terminalPageReads'] >= 41:
                        out['gaps'].append('watch pagination page budget reached; events remain')
                        break
                else:
                    if out['terminalPendingReads'] >= 101:
                        out['gaps'].append('watch pending read budget reached; pending events remain')
                        break
                remaining = drain_deadline - time.monotonic()
                gap = ('watch final drain deadline reached; pending events remain' if not has_more
                       else 'watch final drain deadline reached; events remain')
                if remaining <= 0:
                    out['gaps'].append(gap)
                    break
                if not has_more or pending_progress_page:
                    time.sleep(min(.05, remaining))
                if time.monotonic() >= drain_deadline:
                    out['gaps'].append(gap)
                    break
                response_intent = 'published' if has_more else 'pending'
                response = bounded_request({'read': True, 'captureId': owned, 'after': after}, drain_deadline)
                continue
            if backlog:
                if active_cycle_deadline is None:
                    active_cycle_deadline = min(now + 5, deadline)
                    active_cycle_responses = 1
                remaining = active_cycle_deadline - time.monotonic()
                if active_cycle_responses >= 41 or remaining <= 0:
                    active_cutoff('page limit' if active_cycle_responses >= 41 else 'time limit', data)
                    response = normal_poll()
                    continue
                if not has_more or pending_progress_page:
                    time.sleep(min(.05, remaining))
                if time.monotonic() >= active_cycle_deadline:
                    active_cutoff('time limit', data)
                    response = normal_poll()
                    continue
                active_cycle_responses += 1
                try:
                    response = bounded_request({'read': True, 'captureId': owned, 'after': after}, active_cycle_deadline, 'active drain cycle')
                except ObservationError as exc:
                    if time.monotonic() < active_cycle_deadline: raise
                    active_cutoff('time limit', data, exc)
                    response = normal_poll()
                continue
            active_cycle_deadline, active_cycle_responses = None, 0
            response = normal_poll()
    except ObservationError as exc:
        unsafe('watch error: ' + str(exc))
        # The last clipped HTTP read can consume the drain budget before the
        # loop checks it. Preserve its error and the last observed pending count,
        # while explaining the same incomplete drain as the loop-boundary path.
        if drain_deadline is not None and time.monotonic() >= drain_deadline and out['pendingEvents'] > 0:
            gap = 'watch final drain deadline reached; pending events remain'
            if gap not in out['gaps']: out['gaps'].append(gap)
        out.update(ok=False, error=str(exc), incomplete=True)
        out['gaps'].append('watch incomplete: ' + str(exc))
    except KeyboardInterrupt:
        unsafe('watch interrupted')
        out['gaps'].append('watch interrupted')
    finally:
        if owned and not stop_sent:
            try:
                out['stop'] = bounded_request({'stop': True, 'captureId': owned}, time.monotonic() + 2)
                verify_cleanup(out['stop'])
            except ObservationError as exc:
                unsafe('owned watch stop failed: ' + str(exc))
                out['gaps'].append('owned watch stop failed: ' + str(exc))
    out['incomplete'] = out['incomplete'] or out['captureTruncated'] or any(out[key] for key in ('eventQueueDropped', 'eventFailures', 'visualEventsDropped')) or bool(out['gaps'])
    out['visualAttribution'] = attribute_visuals(out['events'])
    out['sources'] = {}
    for event in out['events']:
        key = event.get('sourceFormId') or 'unknown'
        detail = {'sourceKind': event.get('sourceKind', 'unknown'), 'source': event.get('source')}
        if detail not in out['sources'].setdefault(key, []): out['sources'][key].append(detail)
    out['summary'] = dict(Counter(event.get('sourceFormId') or 'unknown' for event in out['events']))
    return out
