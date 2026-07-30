"""Worker lifecycle — launch + observation via plain tmux (design §Tier 1, D2).

Workers are separate interactive CLI processes launched into PLAIN TMUX
sessions named ``am-*``. muxplex is a dashboard over a tmux server, so these
sessions appear in muxplex automatically whenever it runs — targeting tmux
directly IS the design's muxplex integration path for step 2, keeps this
module dependency-free (stdlib only), and honors the design's ``am-*``
naming/allowlist convention. The muxplex agent HTTP API client (bells,
``/api/view`` polling, input allowlist) is a later step.

Launch shape: ``tmux new-session -d -s am-<name>`` running a bash wrapper
around the worker command; ``tmux pipe-pane`` captures pane output to
``workers/am-<name>/worker.log``. The wrapper echoes an exit sentinel
(``__AM_WORKER_EXIT:<code>__``) to the pane AND appends it directly to the
log file — the direct append closes the race where a fast-exiting command
finishes before pipe-pane attaches, so the sentinel is never lost.

Fail loud (D7): if tmux is not installed, launch/observe raise RuntimeError
with an explicit message. There is no degraded mode.

Environment note: tmux panes inherit the tmux SERVER's environment, not the
dispatching client's. launch() forwards ATTENTION_QUEUE_DIR / ATTENTION_HOME
explicitly via ``new-session -e`` (tmux >= 3.2) so workers see the same queue
the dispatcher sees.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .packet import utc_now_iso
from .queue import ENV_QUEUE_DIR
from .state import ENV_HOME, SESSION_PREFIX

EXIT_SENTINEL_TEMPLATE = "__AM_WORKER_EXIT:{code}__"
EXIT_SENTINEL_RE = re.compile(r"__AM_WORKER_EXIT:(\d+)__")
# Best-effort: amplifier CLI prints "Session ID: <uuid>" near startup.
SESSION_ID_RE = re.compile(r"Session ID:\s*([0-9a-fA-F][0-9a-fA-F-]{7,})")
# CSI + OSC escape sequences (same shape as evals/run_evals.py _strip_ansi).
# The REAL amplifier CLI styles its output: pipe-pane captures e.g.
#   \x1b[2mSession ID: \x1b[0m\x1b[2;93m<uuid>\x1b[0m
# — escape codes BETWEEN the label and the uuid, which \s* cannot cross.
# Found by DTU eval S4: session ids never extracted for real workers, so the
# packet↔worker bell join never bound and no bell rang (silently).
ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
# Bundle/module load failures kill a worker in ~2s, before any LLM turn.
# dispatch's early-death check greps the log for these. Observed live (UX
# round 1, two personas independently): "Error: Bundle '<uri>' not found.
# Available bundles: ...". Kept deliberately narrow — a pattern that matches
# ordinary worker output would turn honest fast successes into false alarms.
LOAD_FAILURE_RE = re.compile(
    r"(?:bundle|module)\s+'[^']*'\s+not\s+found|failed to (?:load|resolve) (?:bundle|module)", re.IGNORECASE
)


def require_tmux() -> str:
    """Return the tmux binary path, or fail loud. No degraded mode (D7)."""
    tmux = shutil.which("tmux")
    if not tmux:
        raise RuntimeError(
            "tmux is not installed (required to launch/observe am-* worker sessions). "
            "Install tmux and retry — the attention manager has no tmux-less degraded mode."
        )
    return tmux


def session_name(name: str) -> str:
    """Normalize a worker name to its am-* tmux session name."""
    return name if name.startswith(SESSION_PREFIX) else f"{SESSION_PREFIX}{name}"


def default_worker_cmd(task: str, bundle: str | None = None) -> str:
    """Default worker command: ``amplifier run [-B <bundle-uri>] '<task>'``."""
    parts = ["amplifier", "run"]
    if bundle:
        parts += ["-B", bundle]
    parts.append(task)
    return shlex.join(parts)


def list_am_sessions() -> list[str]:
    """Names of live am-* tmux sessions. Empty list when no server is running."""
    tmux = require_tmux()
    proc = subprocess.run(
        [tmux, "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
        check=False,  # nonzero = "no server running" (normal empty state, handled below)
    )
    if proc.returncode != 0:
        # "no server running" is a normal empty state, not an error.
        return []
    return [line for line in proc.stdout.splitlines() if line.startswith(SESSION_PREFIX)]


def session_alive(session: str) -> bool:
    """True if the exact tmux session exists (``=`` prefix forces exact match)."""
    tmux = require_tmux()
    proc = subprocess.run([tmux, "has-session", "-t", f"={session}"], capture_output=True, check=False)
    return proc.returncode == 0


def kill_session(session: str) -> None:
    """Best-effort kill of a worker session (used by cleanup paths)."""
    tmux = require_tmux()
    subprocess.run([tmux, "kill-session", "-t", f"={session}"], capture_output=True, check=False)


def launch(name: str, cmd: str, home: Path, task: str | None = None, judge_cmd: str | None = None) -> dict[str, Any]:
    """Launch a worker command into a detached am-* tmux session.

    Creates ``<home>/workers/am-<name>/`` with worker.log + meta.json, starts
    the session with the sentinel wrapper, and attaches pipe-pane to the log.
    ``judge_cmd`` (design §The Judge Requirement) is persisted in meta.json;
    the supervisor runs it when the worker finishes to gate loop closure.
    Returns the meta dict. Raises RuntimeError (loud) on any tmux failure or
    if the session already exists.
    """
    tmux = require_tmux()
    session = session_name(name)
    if session_alive(session):
        raise RuntimeError(f"worker session {session!r} already exists — pick another name or kill it first")

    worker_dir = home / "workers" / session
    worker_dir.mkdir(parents=True, exist_ok=True)
    log_path = worker_dir / "worker.log"
    log_path.touch()
    qlog = shlex.quote(str(log_path))

    # Sentinel goes to the pane (visible to a human hopping in via tmux/muxplex)
    # AND straight into the log (race-free even if pipe-pane attaches late).
    # The leading guard sleep lets pipe-pane attach before the command produces
    # output — without it, a near-instant command's pane output is emitted
    # before capture starts and is lost from the log.
    wrapped = f'sleep 0.5; {cmd}; ec=$?; echo "__AM_WORKER_EXIT:${{ec}}__"; echo "__AM_WORKER_EXIT:${{ec}}__" >> {qlog}; sleep 3'

    env_args: list[str] = []
    for var in (ENV_QUEUE_DIR, ENV_HOME, "PYTHONPATH"):
        value = os.environ.get(var)
        if value:
            env_args += ["-e", f"{var}={value}"]

    try:
        subprocess.run(
            [tmux, "new-session", "-d", "-s", session, *env_args, "bash", "-c", wrapped],
            check=True,
            capture_output=True,
            text=True,
        )
        # pipe-pane needs a pane-shaped target: "=session:" (exact session,
        # default window/pane). Bare "=session" is rejected with "can't find pane".
        subprocess.run(
            [tmux, "pipe-pane", "-o", "-t", f"={session}:", f"cat >> {qlog}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"tmux failed launching {session!r}: {e.stderr or e}") from e

    meta: dict[str, Any] = {
        "name": name,
        "session": session,
        "cmd": cmd,
        "task": task,
        "judge_cmd": judge_cmd,
        "started_at": utc_now_iso(),
    }
    tmp = worker_dir / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, worker_dir / "meta.json")
    return meta


# -- observation ---------------------------------------------------------------


@dataclass
class Observation:
    """One point-in-time look at a worker: tmux liveness + log-derived facts."""

    alive: bool
    exit_code: int | None
    sentinel_seen: bool
    session_id: str | None


def strip_ansi(text: str) -> str:
    """Remove CSI/OSC escape sequences from pipe-pane captured output."""
    return ANSI_RE.sub("", text)


def parse_exit_sentinel(text: str) -> int | None:
    """Extract the exit code from log text. Last match wins (most recent).

    DELIBERATELY no ANSI stripping here: the sentinel is emitted by our own
    plain bash wrapper (launch()) — its bytes are written contiguously, so
    escape sequences from surrounding output cannot appear INSIDE the token —
    and the wrapper additionally appends a raw copy straight to the log
    (race-free, never terminal-processed). Stripping would be harmless but
    unnecessary; the parser stays byte-faithful to our own wrapper contract.
    """
    matches = EXIT_SENTINEL_RE.findall(text)
    return int(matches[-1]) if matches else None


def extract_session_id(text: str) -> str | None:
    """Best-effort amplifier session-id extraction from worker log output.

    Strips ANSI escape sequences first: the real amplifier CLI styles this
    line (dim label, colored uuid), so the raw pipe-pane bytes have escape
    codes between "Session ID:" and the uuid (see ANSI_RE note).
    """
    match = SESSION_ID_RE.search(strip_ansi(text))
    return match.group(1) if match else None


def observe(session: str, log_path: Path) -> Observation:
    """Observe one worker: session liveness + sentinel/session-id from its log."""
    text = ""
    if log_path.exists():
        # pipe-pane output may contain ANSI/partial bytes; never crash on them.
        text = log_path.read_text(encoding="utf-8", errors="replace")
    exit_code = parse_exit_sentinel(text)
    return Observation(
        alive=session_alive(session),
        exit_code=exit_code,
        sentinel_seen=exit_code is not None,
        session_id=extract_session_id(text),
    )
