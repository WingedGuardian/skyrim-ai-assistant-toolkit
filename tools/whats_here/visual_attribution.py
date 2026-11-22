"""Post-capture timing join: which magic effect most likely placed each visual.

The native probe cannot see what attached an effect shader or art object, so it
reports attribution "unknown". This joins each visualAttached event to the
magicEffectApplied events just before it. Candidates are ranked by how often
that effect's applications were followed by this same visual form. A
frequently applied dummy that is only sometimes nearby ranks low. The result is
labelled "inferred:timing", never "direct": timing is evidence, not a link
read from the engine.
"""
from collections import defaultdict

WINDOW_BEFORE = 1.0  # seconds a cause may precede its visual
WINDOW_AFTER = 0.25  # tolerate small delivery reordering
STRONG_RATIO = 0.5   # share of a source's applications that must be followed by the visual


def _form(event):
    return ((event.get('visual') or {}).get('form') or {}).get('formId')


def attribute_visuals(events, before=WINDOW_BEFORE, after=WINDOW_AFTER):
    """Annotate visual events in place; return a summary list. Never raises on odd events."""
    applied = [e for e in events if e.get('kind') == 'magicEffectApplied'
               and e.get('sourceFormId') and isinstance(e.get('time'), (int, float))]
    attached = [e for e in events if e.get('kind') == 'visualAttached'
                and isinstance(e.get('time'), (int, float))]
    total = defaultdict(int)
    for e in applied:
        total[e['sourceFormId']] += 1

    def near(visual):
        t = visual['time']
        return [e for e in applied if t - before <= e['time'] <= t + after]

    # For each (source, visual form): how many of the source's applications had that visual follow.
    followed = defaultdict(set)
    for v in attached:
        form = _form(v)
        for e in near(v):
            followed[(e['sourceFormId'], form)].add(id(e))

    by_effect, summary = {}, []
    for v in attached:
        form = _form(v)
        seen, candidates = set(), []
        for e in near(v):
            sid = e['sourceFormId']
            if sid in seen:
                continue
            seen.add(sid)
            src = e.get('source') or {}
            hits = len(followed[(sid, form)])
            candidates.append({'sourceFormId': sid, 'editorId': src.get('editorId'),
                               'runtimeSourceFiles': src.get('runtimeSourceFiles'),
                               'leadSeconds': round(v['time'] - e['time'], 3),
                               'applications': total[sid], 'followedByThisVisual': hits,
                               'ratio': round(hits / total[sid], 3)})
        candidates.sort(key=lambda c: (-c['ratio'], -c['followedByThisVisual'], abs(c['leadSeconds'])))
        visual = v.setdefault('visual', {})
        visual['candidateSources'] = candidates[:5]
        best = candidates[0] if candidates else None
        tied = [c['sourceFormId'] for c in candidates[1:] if best and c['ratio'] == best['ratio']
                and c['followedByThisVisual'] == best['followedByThisVisual']]
        if tied:
            # Two sources the timing cannot tell apart: naming either would be a coin toss.
            visual['tiedSources'] = [best['sourceFormId']] + tied
        if best and best['ratio'] >= STRONG_RATIO and not tied:
            v['attribution'] = visual['attribution'] = 'inferred:timing'
            visual['inferredSource'] = best['sourceFormId']
        effect_id = visual.get('effectId')
        if effect_id is not None:
            by_effect[effect_id] = visual
        summary.append({'visualForm': form, 'effectId': effect_id,
                        'attribution': visual.get('attribution', 'unknown'),
                        'inferredSource': visual.get('inferredSource'),
                        'tiedSources': visual.get('tiedSources', [])})

    # A removal inherits the inference of the attachment with the same effectId.
    for e in events:
        if e.get('kind') != 'visualRemoved':
            continue
        visual = e.get('visual') or {}
        src = by_effect.get(visual.get('effectId'))
        if src and src.get('inferredSource'):
            visual['inferredSource'] = src['inferredSource']
            e['attribution'] = visual['attribution'] = 'inferred:timing'
    return summary
