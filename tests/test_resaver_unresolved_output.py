"""An unresolved ReSaver output must not receive preservation credit for its readable input."""
import sys

import pytest

from conftest import REPO
from test_save_physical_paths import quoted

sys.path.insert(0, str(REPO / 'tools'))
from modding_harness import contracts
from skyguard import portable


def run(root, operation, command):
    action = contracts.Action('generic', 'shell', 'shell', shell='bash', cwd=str(root), command=command,
        session_id='owned-resaver-output', tool_call_id=operation)
    return portable.check(action, root)[0]


@pytest.mark.parametrize('operation', ['clean', 'reset-havok'])
@pytest.mark.parametrize('existing', [False, True])
def test_a_resolved_output_is_allowed_and_nothing_is_changed_or_backed_up(owned, operation, existing):
    """4.1: no rule decides on a save and no hook backs one up (the save vault keeps every version)."""
    root, _, saves, _, pair = owned
    source = pair(saves, 'Input', b'INPUT ORIGINAL')
    destination = tuple(saves / ('Output' + ext) for ext in ('.ess', '.skse'))
    if existing:
        destination = pair(saves, 'Output', b'OUTPUT ORIGINAL')
    monitored = source + (destination if existing else ())
    before = {p: p.read_bytes() for p in monitored}
    command = 'SAVE=' + quoted(source[0]) + '\nOUT=' + quoted(destination[0]) + '\n'
    command += 'bash tools/resaver-cli.sh ' + operation + ' "$SAVE" "$OUT" --apply'
    outcome = run(root, operation, command)
    assert outcome.result.decision == 'allow', outcome.result.as_dict()
    assert outcome.transactions == ()
    assert before == {p: p.read_bytes() for p in before}
    if not existing:
        assert not any(p.exists() for p in destination)

