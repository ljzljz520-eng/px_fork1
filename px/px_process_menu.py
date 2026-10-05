"""
Interactive menu for killing or infoing a process.

Invoked from px_top.py.
"""

import os
import time
import errno
import subprocess

from . import px_pager
from . import px_process
from . import px_terminal
from . import px_processinfo

from typing import Callable

# Constants signal.SIGXXX are enums in Python 3. But we want the numbers (to
# pass as an argument to /bin/kill), so we make our own int constants.
SIGTERM = 15
SIGKILL = 9

KILL_TIMEOUT_SECONDS = 5


def get_header_line(process: px_process.PxProcess) -> str:
    header_line = "Process: "
    header_line += str(process.pid) + " " + process.command
    header_line = px_terminal.bold(header_line)
    return header_line


def kill(process: px_process.PxProcess, signo: int) -> bool:
    """
    Signal a process.

    Returns True if the signal was delivered, False otherwise (not allowed).
    """
    try:
        os.kill(process.pid, signo)
    except (IOError, OSError) as e:
        if e.errno not in [errno.EPERM, errno.EACCES]:
            raise e

        return False

    return True


def sudo_kill(process: px_process.PxProcess, signo: int) -> bool:
    """
    Signal a process as root.

    Returns True if the signal was delivered, False otherwise.
    """
    with px_terminal.normal_display():
        print(px_terminal.CLEAR_SCREEN)

        # Print process screen heading followed by an empty line
        _, columns = px_terminal.get_window_size()

        print(px_terminal.crop_ansi_string_at_length(get_header_line(process), columns))
        print("")

        # Print "sudo kill 1234"
        command = ["sudo", "kill"]
        if signo != SIGTERM:
            command += ["-" + str(signo)]
        command += [str(process.pid)]

        print("$ " + " ".join(command))

        # Invoke "sudo kill 1234"
        returncode = subprocess.call(command)
        if returncode == 0:
            return True

        # Give user time to peruse any error message
        time.sleep(1.5)
        return False


class PxProcessMenu:
    # NOTE: Must match number constants in execute_menu_entry()
    MENU_ENTRIES = [
        "Show info",
        "Kill process",
        "Kill process as root",
        "Back to process listing",
    ]

    def __init__(self, process: px_process.PxProcess) -> None:
        self.process = process

        # Immutable (PID, start fingerprint) of the selected instance. Every
        # action re-resolves this instead of trusting the PID, so a PID that
        # exited or got reused can never be inspected or signalled.
        self.identity = px_process.ProcessIdentity.capture(process)

        self.done = False

        # Shown to user, status of last operation
        self.status = ""

        # Index into MENU_ENTRIES
        self.active_entry = 0

    def stale_status(self, state: str) -> str:
        pid = self.identity.pid
        command = self.process.command
        if state == px_process.IdentityResolution.GONE:
            return f"Process {pid} <{command}> no longer exists, nothing to do"
        if state == px_process.IdentityResolution.REPLACED:
            return (
                f"PID {pid} is now held by a different process instance, "
                f"leaving it untouched"
            )

        raise ValueError(f"Not a stale state: {state!r}")

    def refresh_display(self) -> None:
        _, columns = px_terminal.get_window_size()

        lines = []

        lines += [get_header_line(self.process)]
        lines += [""]

        lines += [
            px_terminal.bold("Arrow keys")
            + " move up and down, "
            + px_terminal.bold("RETURN")
            + " selects, "
            + px_terminal.bold("ESC")
            + " to go back."
        ]
        lines += [""]

        last_entry_no = len(self.MENU_ENTRIES) - 1
        for entry_no, text in enumerate(self.MENU_ENTRIES):
            prefix = "    "
            arrow = "⇵"
            if entry_no == 0:
                arrow = "↓"
            elif entry_no == last_entry_no:
                arrow = "↑"
            if entry_no == self.active_entry:
                prefix = arrow + " ->"
                text = px_terminal.inverse_video(text)

            lines += [prefix + text]

        if self.status:
            lines += ["", "Status: " + px_terminal.bold(self.status)]

        px_terminal.draw_screen_lines(lines, columns)

    def await_and_handle_user_input(self) -> None:
        incoming = px_terminal.getch()
        if incoming is None:
            return
        assert len(incoming) > 0

        self.status = ""
        while len(incoming) > 0:
            if incoming.consume(px_terminal.KEY_UPARROW):
                self.active_entry -= 1
                if self.active_entry < 0:
                    self.active_entry = 0
            elif incoming.consume(px_terminal.KEY_DOWNARROW):
                self.active_entry += 1
                if self.active_entry >= len(self.MENU_ENTRIES):
                    self.active_entry = len(self.MENU_ENTRIES) - 1
            elif incoming.consume(px_terminal.KEY_ENTER):
                self.execute_menu_entry()
            elif incoming.consume("q"):
                self.done = True
                return
            elif incoming.consume(px_terminal.SIGWINCH_KEY):
                # After we return the screen will be refreshed anyway,
                # no need to do anything here.
                continue
            elif incoming.consume(px_terminal.KEY_ESC):
                self.done = True
                return
            else:
                # Unable to consume, give up
                break

    def start(self) -> None:
        """
        Process menu main loop
        """
        while not self.done:
            # Re-prove identity on every iteration; the menu must close
            # itself rather than act on an exited or reused PID.
            resolution = self.identity.resolve()
            if resolution.is_stale:
                self.status = self.stale_status(resolution.state)
                self.refresh_display()
                self.done = True
                return

            self.refresh_display()
            self.await_and_handle_user_input()

    def page_process_info(self) -> None:
        """
        Display process info in a pager.
        """
        resolution = self.identity.resolve()
        if resolution.is_stale:
            self.status = self.stale_status(resolution.state)
            return

        processes = px_process.get_all()
        process = px_processinfo.find_process_by_pid(self.identity.pid, processes)
        if process is None:
            self.status = self.stale_status(px_process.IdentityResolution.GONE)
            return
        if not self.identity.matches_process(process):
            # The record at our PID got replaced between the two reads
            self.status = self.stale_status(px_process.IdentityResolution.REPLACED)
            return

        with px_terminal.normal_display():
            px_pager.page_process_info(process, processes)

    def await_death(self, message: str) -> str:
        """
        Wait KILL_TIMEOUT_SECONDS for the selected process instance to die.

        Returns an IdentityResolution state: GONE if the instance went away,
        REPLACED if a different instance took over the PID while waiting (in
        which case the caller must not escalate), or MATCHED on timeout.
        """
        t0 = time.time()
        while (time.time() - t0) < KILL_TIMEOUT_SECONDS:
            resolution = self.identity.resolve()
            if resolution.state != px_process.IdentityResolution.MATCHED:
                return resolution.state

            dt_s = time.time() - t0
            countdown_s = KILL_TIMEOUT_SECONDS - dt_s
            if countdown_s <= 0:
                return px_process.IdentityResolution.MATCHED
            self.status = f"{countdown_s:.1f}s {message}"
            self.refresh_display()

            time.sleep(0.1)

        return px_process.IdentityResolution.MATCHED

    def kill_process(
        self, signal_process: Callable[[px_process.PxProcess, int], bool]
    ) -> None:
        """
        Send first SIGTERM then SIGKILL to a process.

        Wait KILL_TIMEOUT_SECONDS secods in between to give it a chance to go
        away. Identity is re-proved before each signal and before each
        liveness check, so a reused PID is never signalled and SIGKILL is
        blocked if the process got replaced after SIGTERM.
        """
        # Prove the selected instance still exists before doing anything
        resolution = self.identity.resolve()
        if resolution.is_stale:
            self.status = self.stale_status(resolution.state)
            return

        # Please go away
        if not signal_process(self.process, SIGTERM):
            self.status = (
                "Not allowed to kill <" + self.process.command + ">, try again as root!"
            )
            return

        death_state = self.await_death(
            f"Waiting for {self.process.command} to shut down after SIGTERM"
        )
        if death_state == px_process.IdentityResolution.GONE:
            return
        if death_state == px_process.IdentityResolution.REPLACED:
            # Some other process now holds the PID, never escalate at it
            self.status = self.stale_status(death_state)
            return

        # The instance is still there, re-prove identity before escalating
        resolution = self.identity.resolve()
        if resolution.state == px_process.IdentityResolution.GONE:
            return
        if resolution.state == px_process.IdentityResolution.REPLACED:
            self.status = self.stale_status(resolution.state)
            return
        assert resolution.state == px_process.IdentityResolution.MATCHED

        # Die!!
        assert signal_process(self.process, SIGKILL)
        death_state = self.await_death(
            f"Waiting for {self.process.command} to shut down after kill -9"
        )
        if death_state == px_process.IdentityResolution.GONE:
            return
        if death_state == px_process.IdentityResolution.REPLACED:
            # The replacement that took over the PID got spared
            self.status = self.stale_status(death_state)
            return

        self.status = "<" + self.process.command + "> did not die!"
        return

    def execute_menu_entry(self):
        # NOTE: Constants here must match lines in self.MENU_ENTRIES
        # at the top of this file
        if self.active_entry == 0:
            self.page_process_info()
        elif self.active_entry == 1:
            self.kill_process(kill)
        elif self.active_entry == 2:
            self.kill_process(sudo_kill)
        elif self.active_entry == 3:
            self.done = True
