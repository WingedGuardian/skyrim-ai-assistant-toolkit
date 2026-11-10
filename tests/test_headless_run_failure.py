"""A failure while the managed game runs (Souls control05 shape, traced to the toolkit 2026-11-09).

In the Souls runner a running-phase refusal escaped to a cleanup that never sent the quit, and the supervisor exited
with the game still live: the only exit owner was gone. The toolkit's `run` cleans up through `stop`, which sends the
orderly quit itself. These tests pin that: whatever ends the wait, the live game gets exactly one quit before any
helper is stopped, the install is restored, and the original error is still reported.
"""
import pytest

from test_vr_headless import snapshot, tree, world  # noqa: F401  (fixtures)


def _fail_first_wait(mod, error, before_raise=lambda: None):
    real = mod.wait_until_exited
    calls = []

    def wait(games, port, deadline=None):
        calls.append(deadline)
        if len(calls) == 1:                      # run's own wait for the game; stop's waits are real
            before_raise()
            raise error
        return real(games, port, deadline=deadline)
    mod.wait_until_exited = wait


def _order(mod, world):
    order = []
    real_quit, real_terminate = mod.devbench_quit, mod.terminate_verified
    mod.devbench_quit = lambda port: (order.append("quit"), real_quit(port))[1]
    mod.terminate_verified = lambda pid, start: (order.append("game alive" if world.game() else "game gone"),
                                                 real_terminate(pid, start))[1]
    return order


@pytest.mark.parametrize("error", ["inventory", "ctrl-c"])
def test_a_failure_while_the_game_runs_still_quits_it_once_then_restores(tree, world, capsys, error):
    before = snapshot(tree)
    mod = tree["mod"]
    _fail_first_wait(mod, mod.ProcessReadError("fixture: inventory failed") if error == "inventory"
                     else KeyboardInterrupt())
    order = _order(mod, world)
    if error == "ctrl-c":
        with pytest.raises(KeyboardInterrupt):           # Ctrl-C is never swallowed by the cleanup
            mod.main(["run", "--desktop", "background"])
    else:
        assert mod.main(["run", "--desktop", "background"]) == 2
    assert order and order[0] == "quit" and order.count("quit") == 1, order
    assert all(step == "game gone" for step in order[1:]), "a helper was stopped under the live game"
    assert snapshot(tree) == before and mod.load_state() is None
    err = capsys.readouterr().err
    assert ("ProcessReadError: fixture: inventory failed" if error == "inventory" else "KeyboardInterrupt") in err


def test_the_original_error_survives_a_cleanup_that_also_fails(tree, world, capsys):
    """The game stops answering DevBench, so `stop` refuses (nothing sent, session kept): both errors are reported."""
    mod = tree["mod"]
    def devbench_goes_silent():
        world.health = None
    _fail_first_wait(mod, mod.ProcessReadError("fixture: inventory failed"), devbench_goes_silent)
    assert mod.main(["run", "--desktop", "background"]) == 2
    err = capsys.readouterr().err
    assert "ProcessReadError: fixture: inventory failed" in err
    assert "not ready for an orderly exit" in err
    assert world.quits == [] and mod.load_state() is not None      # kept for `stop` after the user closes it
