"""An unresolved ReSaver output must not receive preservation credit for its readable input."""
import json
import sys

import pytest

from conftest import REPO
from test_save_physical_paths import owned, quoted

sys.path.insert(0, str(REPO / 'tools'))
from modding_harness import contracts
from skyguard import portable, rules


def run(root, operation, command):
    action = contracts.Action('generic', 'shell', 'shell', shell='bash', cwd=str(root), command=command,
        session_id='owned-resaver-output', tool_call_id=operation)
    return portable.check(action, root)[0]


@pytest.mark.parametrize('operation', ['clean', 'reset-havok'])
@pytest.mark.parametrize('existing', [False, True])
def test_resolved_output_preserves_only_an_existing_output_pair(owned, operation, existing):
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
    assert len(outcome.transactions) == int(existing)
    assert before == {p: p.read_bytes() for p in before}
    if existing:
        store = root / '.modding-agent-harness/backups/saves'
        record = json.loads((store / 'transactions' / (outcome.transactions[0] + '.json')).read_bytes())
        members = [m for group in record['groups'] for m in group['members']]
        assert len(members) == 2 and {m['path'].replace('\\', '/').rsplit('/', 1)[1] for m in members} == {'Output.ess', 'Output.skse'}
        for member in members:
            assert (store / 'blobs' / member['sha256']).read_bytes() in [p.read_bytes() for p in destination]
    else:
        assert not any(p.exists() for p in destination)

