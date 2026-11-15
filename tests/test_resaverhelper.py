"""resaverhelper: reads run, game-changing commands need --apply, saving and Transplant are refused."""
import sys

import pytest

from conftest import REPO
sys.path.insert(0, str(REPO / "tools"))
import resaverhelper as rh  # noqa: E402


class Fake:
    def __init__(self, reply="1\tOK\tReSaverHelper\t18\t1.4.15\tVR\t1"):
        self.reply, self.sent = reply, []

    def __call__(self, line):
        self.sent.append(line)
        return self.reply + "\n"


def test_reads_are_sent_as_one_tab_separated_line():
    fake = Fake("1\tOK\tMQ101\t0\t20\t1\t0\t")
    assert rh.main(["quest", "0x0002610C"], transport=fake) == 0
    assert fake.sent == ["1\tQUEST\t0x0002610C\n"]


def test_writes_are_a_dry_run_without_apply_and_send_nothing(capsys):
    fake = Fake()
    assert rh.main(["setstage", "0x0002610C", "20"], transport=fake) == 0
    assert fake.sent == [] and "DRY RUN" in capsys.readouterr().out


def test_writes_with_apply_fill_the_notify_and_message_fields():
    fake = Fake("1\tOK\t1\t1\t0\t20")
    assert rh.main(["setstage", "0x0002610C", "20", "--apply"], transport=fake) == 0
    assert fake.sent == ["1\tSETSTAGE\t0x0002610C\t20\thud\t\n"]


@pytest.mark.parametrize("command", ["savegame", "rescue", "snapshot", "apply"])
def test_saving_and_transplant_are_refused_even_with_apply(command):
    fake = Fake()
    assert rh.main([command, "--apply"], transport=fake) == 2
    assert fake.sent == []


def test_an_err_reply_exits_1_and_fields_are_unescaped(capsys):
    assert rh.main(["hello"], transport=Fake("1\tERR\ta menu is open\\tclose it")) == 1
    assert "a menu is open\tclose it" in capsys.readouterr().out


def test_fields_with_tabs_newlines_and_backslashes_round_trip():
    raw = "a\tb\nc\\d"
    assert rh.unescape(rh.escape(raw)) == raw


def test_wrong_argument_counts_and_unknown_commands_refuse():
    fake = Fake()
    for args in (["quest"], ["setoption", "x"], ["frobnicate"]):
        assert rh.main(args + ["--apply"], transport=fake) == 2
    assert fake.sent == []


def test_a_garbled_reply_is_an_error_not_a_success():
    assert rh.main(["hello"], transport=Fake("garbage")) == 2


def test_no_helper_listening_is_reported(capsys):
    def missing(line):
        raise FileNotFoundError(rh.PIPE)
    assert rh.main(["hello"], transport=missing) == 2
    assert "no ReSaverHelper is listening" in capsys.readouterr().err
