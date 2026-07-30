"""Tier-3 muxplex surface: ring the tmux bell for a worker session (design §Tier 3).

muxplex is a dashboard over the tmux server; its needs-attention primitive is
the per-window tmux bell state (``window_bell_flag`` → ``last_fired_at`` /
``unseen_count``; ``sort=attention`` floats belled sessions, muxplex-deck
shows amber). Our workers already live in ``am-*`` tmux sessions, so making a
session "need attention" reduces to making its window register a bell.

Mechanism: write BEL (``\\a``) to the pane's tty (the pty *slave* side). tmux
reads the pty master, processes the BEL as pane output, and sets
``window_bell_flag=1``. Chosen over the alternatives because:

* ``tmux send-keys`` injects *input* into the busy worker process — unsafe.
* ``tmux run-shell`` output does not pass through the pane's terminal state
  machine, so it never registers a bell.
* A tty write works from OUTSIDE the session (the supervisor is not in tmux;
  no client needs to be attached) and touches nothing but the bell flag.

Option findings on tmux 3.4 (proven by tests/test_bells.py real-tmux tests):

* ``monitor-bell`` (window option, default ``on``) DOES gate the flag: with
  it off, a BEL never sets ``window_bell_flag``. Handled: ring_bell enforces
  ``monitor-bell on`` on the target window before writing BEL — the ``am-*``
  sessions are launched and owned by the manager, so pinning this option on
  our own windows is policy we're entitled to set.
* ``bell-action`` (session option) does NOT gate the flag — even ``none``
  only suppresses alert actions toward attached clients.
* Detached sessions keep the flag set — no attached client views the window,
  so nothing clears it. The human (or muxplex UI) clears bells; the manager
  NEVER does.
* Window targeting must use the window id (``@N`` from ``#{window_id}``),
  never ``session:0`` — ``base-index`` is user-configurable (often 1).
* pipe-pane (attached by workers.launch) captures the BEL byte into
  worker.log — harmless: the sentinel/session-id regexes are unaffected.

Fail loud (D7): any failure raises RuntimeError. Policy lives in the caller
(the supervisor emits one ``bell:error`` per session and keeps the loop alive).
"""

from __future__ import annotations

import os
import subprocess

from .workers import require_tmux

BEL = b"\a"


def _tmux_query(tmux: str, tmux_session: str, fmt: str) -> str:
    """display-message -p against the session's active pane, or fail loud."""
    proc = subprocess.run(
        [tmux, "display-message", "-p", "-t", f"={tmux_session}:", fmt],
        capture_output=True,
        text=True,
        check=False,  # nonzero is handled explicitly below (fail loud with detail)
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "tmux display-message failed"
        raise RuntimeError(f"cannot resolve pane tty for {tmux_session!r}: {detail}")
    return proc.stdout.strip()


def ring_bell(tmux_session: str) -> None:
    """Ring the target session's bell (window_bell_flag=1) from outside it.

    Enforces ``monitor-bell on`` for the target window (without it the BEL
    would be swallowed — see module docstring), then writes BEL to the pane
    tty. Raises RuntimeError on any failure (unknown session, unwritable
    tty). Never injects input into the worker process.
    """
    tmux = require_tmux()
    fields = _tmux_query(tmux, tmux_session, "#{window_id} #{pane_tty}")
    window_id, _, tty = fields.partition(" ")
    if not window_id.startswith("@") or not tty.startswith("/dev/"):
        raise RuntimeError(f"cannot ring bell for {tmux_session!r}: unexpected window/tty {fields!r}")

    proc = subprocess.run(
        [tmux, "set-option", "-w", "-t", window_id, "monitor-bell", "on"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "tmux set-option failed"
        raise RuntimeError(f"cannot ring bell for {tmux_session!r}: enforcing monitor-bell on failed: {detail}")

    try:
        # O_NOCTTY: never adopt the worker's tty as our controlling terminal.
        fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY)
        try:
            os.write(fd, BEL)
        finally:
            os.close(fd)
    except OSError as e:
        raise RuntimeError(f"cannot ring bell for {tmux_session!r}: writing BEL to {tty} failed: {e}") from e
