"""Timing-join attribution of probe visual events (offline, no game)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from whats_here.visual_attribution import attribute_visuals  # noqa: E402


def mgef(t, sid, editor='E'):
    return {'kind': 'magicEffectApplied', 'time': t, 'sourceFormId': sid,
            'source': {'editorId': editor, 'runtimeSourceFiles': ['X.esp']}, 'attribution': 'direct'}


def vis(kind, t, eid, form='SHADER'):
    return {'kind': kind, 'time': t, 'attribution': 'unknown',
            'visual': {'effectId': eid, 'kind': 'effectShader', 'attribution': 'unknown', 'form': {'formId': form}}}


def noisy_dummy(start, stop, step=0.3):
    out, t = [], start
    while t < stop:
        out.append(mgef(t, 'DUMMY', 'madDetectionDummyEffect'))
        t += step
    return out


def test_proc_outranks_frequent_dummy_and_removal_inherits():
    # The proc leads by 0.9 s, so a closer dummy exists: ranking by timing alone would pick the dummy.
    events = noisy_dummy(0, 60) + [mgef(9.5, 'PROC', 'Unleash'), vis('visualAttached', 10.4, 1),
                                   vis('visualRemoved', 18.0, 1), mgef(36.0, 'PROC', 'Unleash'),
                                   vis('visualAttached', 36.1, 2), vis('visualRemoved', 44.0, 2)]
    events.sort(key=lambda e: e['time'])
    summary = attribute_visuals(events)
    assert [s['inferredSource'] for s in summary] == ['PROC', 'PROC']
    first = next(e for e in events if e['kind'] == 'visualAttached')
    assert first['attribution'] == 'inferred:timing'
    top = first['visual']['candidateSources'][0]
    assert top['sourceFormId'] == 'PROC' and top['ratio'] == 1.0 and top['applications'] == 2
    assert any(c['sourceFormId'] == 'DUMMY' and c['ratio'] < 0.1 for c in first['visual']['candidateSources'])
    removed = [e for e in events if e['kind'] == 'visualRemoved']
    assert all(e['visual']['inferredSource'] == 'PROC' for e in removed)
    assert all(e['attribution'] == 'inferred:timing' == e['visual']['attribution'] for e in removed)


def test_only_noise_nearby_stays_unknown():
    events = sorted(noisy_dummy(0, 60) + [vis('visualAttached', 30.05, 7)], key=lambda e: e['time'])
    summary = attribute_visuals(events)
    attached = next(e for e in events if e['kind'] == 'visualAttached')
    assert attached['attribution'] == 'unknown' and summary[0]['inferredSource'] is None
    assert attached['visual']['candidateSources']  # still listed, just not asserted


def test_cause_outside_window_is_not_used():
    events = [mgef(0.0, 'PROC'), vis('visualAttached', 5.0, 3)]
    attribute_visuals(events)
    assert events[1]['attribution'] == 'unknown' and events[1]['visual']['candidateSources'] == []


def test_malformed_events_do_not_raise():
    events = [{'kind': 'visualAttached'}, {'kind': 'magicEffectApplied', 'time': 'x'}, {'kind': 'hit'},
              vis('visualRemoved', 1.0, None)]
    assert attribute_visuals(events) == []


def test_two_sources_timing_cannot_separate_are_reported_tied_not_inferred():
    events = [mgef(9.5, 'A'), mgef(9.6, 'B'), vis('visualAttached', 10.0, 1),
              mgef(29.5, 'A'), mgef(29.6, 'B'), vis('visualAttached', 30.0, 2)]
    summary = attribute_visuals(events)
    assert all(s['inferredSource'] is None and s['attribution'] == 'unknown' for s in summary)
    assert all(sorted(s['tiedSources']) == ['A', 'B'] for s in summary)
