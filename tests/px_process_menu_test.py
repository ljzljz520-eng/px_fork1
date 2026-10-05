"""
Tests that every process menu action re-proves ProcessIdentity before
inspecting or signalling anything, and that a reused PID can never be touched
by an old menu selection.
"""

from px import px_process
from px import px_process_menu

from . import testutils

from typing import List
from typing import Tuple


REUSE_PID = 47536
OLD_TIMESTRING = testutils.TIMESTRING
NEW_TIMESTRING = "Mon May  7 09:33:11 2010"


def _create_menu(monkeypatch, timestring=OLD_TIMESTRING):
    # Fabricated PIDs don't exist, so tick reads come back empty and the ps
    # timestamp is used as the fingerprint fallback.
    monkeypatch.setattr(px_process, "read_start_tick", lambda pid: None)
    process = testutils.create_process(pid=REUSE_PID, timestring=timestring)
    menu = px_process_menu.PxProcessMenu(process)

    # Don't touch the terminal while waiting for processes to die
    monkeypatch.setattr(menu, "refresh_display", lambda: None)

    return menu, process


def _signal_recorder(signalled):
    def signal_process(process, signo):
        signalled.append((process.pid, signo))
        return True

    return signal_process


def test_kill_gone_before_action_sends_no_signal(monkeypatch):
    """PID disappeared before the action: stale state, no signal."""
    menu, _ = _create_menu(monkeypatch)
    monkeypatch.setattr(px_process, "get_process", lambda pid: None)

    signalled: List[Tuple[int, int]] = []
    menu.kill_process(_signal_recorder(signalled))

    assert signalled == []
    assert "no longer exists" in menu.status


def test_kill_replaced_before_action_sends_no_signal(monkeypatch):
    """Same PID, different start fingerprint: no signal, stale state."""
    menu, _ = _create_menu(monkeypatch)
    replacement = testutils.create_process(pid=REUSE_PID, timestring=NEW_TIMESTRING)
    monkeypatch.setattr(px_process, "get_process", lambda pid: replacement)

    signalled: List[Tuple[int, int]] = []
    menu.kill_process(_signal_recorder(signalled))

    assert signalled == []
    assert "different process instance" in menu.status


def test_kill_dies_after_sigterm_sends_no_sigkill(monkeypatch):
    """Existing TERM-then-die behavior: one SIGTERM is enough."""
    menu, original = _create_menu(monkeypatch)

    records = iter([original, None])
    monkeypatch.setattr(px_process, "get_process", lambda pid: next(records))

    signalled: List[Tuple[int, int]] = []
    menu.kill_process(_signal_recorder(signalled))

    assert signalled == [(REUSE_PID, px_process_menu.SIGTERM)]
    assert menu.status == ""


def test_kill_escalates_to_sigkill_when_still_alive(monkeypatch):
    """Existing TERM-then-KILL behavior when the process survives SIGTERM."""
    menu, original = _create_menu(monkeypatch)
    monkeypatch.setattr(px_process_menu, "KILL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(px_process, "get_process", lambda pid: original)

    signalled: List[Tuple[int, int]] = []
    menu.kill_process(_signal_recorder(signalled))

    assert signalled == [
        (REUSE_PID, px_process_menu.SIGTERM),
        (REUSE_PID, px_process_menu.SIGKILL),
    ]
    assert "did not die" in menu.status


def test_kill_replaced_while_waiting_blocks_sigkill(monkeypatch):
    """
    Replacement between SIGTERM and the escalation check: the replacement
    must never receive SIGKILL.
    """
    menu, original = _create_menu(monkeypatch)
    replacement = testutils.create_process(pid=REUSE_PID, timestring=NEW_TIMESTRING)

    records = iter([original, replacement])
    monkeypatch.setattr(px_process, "get_process", lambda pid: next(records))

    signalled: List[Tuple[int, int]] = []
    menu.kill_process(_signal_recorder(signalled))

    assert signalled == [(REUSE_PID, px_process_menu.SIGTERM)]
    assert "different process instance" in menu.status


def test_kill_replaced_between_phases_blocks_sigkill(monkeypatch):
    """
    The instance survives SIGTERM (wait times out), but by the time we would
    escalate the PID has been reused: SIGKILL must be blocked.
    """
    menu, original = _create_menu(monkeypatch)
    replacement = testutils.create_process(pid=REUSE_PID, timestring=NEW_TIMESTRING)
    monkeypatch.setattr(px_process_menu, "KILL_TIMEOUT_SECONDS", 0)

    records = iter([original, replacement])
    monkeypatch.setattr(px_process, "get_process", lambda pid: next(records))

    signalled: List[Tuple[int, int]] = []
    menu.kill_process(_signal_recorder(signalled))

    assert signalled == [(REUSE_PID, px_process_menu.SIGTERM)]
    assert "different process instance" in menu.status


def test_kill_dies_after_sigkill(monkeypatch):
    """TERM, wait (still there), KILL, then gone: both signals, clean status."""
    menu, original = _create_menu(monkeypatch)
    signalled: List[Tuple[int, int]] = []

    def fake_get_process(pid):
        # Report gone only once SIGKILL has actually been sent
        if (REUSE_PID, px_process_menu.SIGKILL) in signalled:
            return None
        return original

    monkeypatch.setattr(px_process_menu, "KILL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(px_process_menu.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(px_process, "get_process", fake_get_process)

    menu.kill_process(_signal_recorder(signalled))

    assert signalled == [
        (REUSE_PID, px_process_menu.SIGTERM),
        (REUSE_PID, px_process_menu.SIGKILL),
    ]
    assert "did not die" not in menu.status


def test_page_process_info_stale_shows_nothing(monkeypatch):
    menu, _ = _create_menu(monkeypatch)
    monkeypatch.setattr(px_process, "get_process", lambda pid: None)

    def fail_get_all():
        raise AssertionError("get_all() must not run for a stale identity")

    monkeypatch.setattr(px_process, "get_all", fail_get_all)
    monkeypatch.setattr(
        px_process_menu.px_pager,
        "page_process_info",
        lambda process, processes: pytest_fail(),
    )

    menu.page_process_info()
    assert "no longer exists" in menu.status


def test_page_process_info_replaced_shows_nothing(monkeypatch):
    menu, original = _create_menu(monkeypatch)
    replacement = testutils.create_process(pid=REUSE_PID, timestring=NEW_TIMESTRING)

    # First (minimal) read still sees the old instance, but the full snapshot
    # taken right after contains the replacement
    monkeypatch.setattr(px_process, "get_process", lambda pid: original)
    monkeypatch.setattr(px_process, "get_all", lambda: [replacement])
    monkeypatch.setattr(
        px_process_menu.px_pager,
        "page_process_info",
        lambda process, processes: pytest_fail(),
    )

    menu.page_process_info()
    assert "different process instance" in menu.status


def test_page_process_info_matched(monkeypatch):
    menu, original = _create_menu(monkeypatch)
    monkeypatch.setattr(px_process, "get_process", lambda pid: original)
    monkeypatch.setattr(px_process, "get_all", lambda: [original])

    paged = []
    monkeypatch.setattr(
        px_process_menu.px_pager,
        "page_process_info",
        lambda process, processes: paged.append(process),
    )

    # Skip the TTY mode switching around the pager
    class DummyDisplay:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        px_process_menu.px_terminal, "normal_display", lambda: DummyDisplay()
    )

    menu.page_process_info()
    assert paged == [original]


def test_start_closes_when_target_is_gone(monkeypatch):
    menu, _ = _create_menu(monkeypatch)

    def stale_resolve(self, processes=None):
        return px_process.IdentityResolution(self, px_process.IdentityResolution.GONE)

    monkeypatch.setattr(px_process.ProcessIdentity, "resolve", stale_resolve)
    monkeypatch.setattr(menu, "await_and_handle_user_input", lambda: None)

    menu.start()
    assert menu.done
    assert "no longer exists" in menu.status


def pytest_fail():
    raise AssertionError("pager must not be invoked")
