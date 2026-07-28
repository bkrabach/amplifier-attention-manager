"""Single-instance supervisor lock — regression tests for the S4 DTU incident.

Observed failure mode (DTU eval, scenario 4): the harness's SIGKILL missed the
supervisor's real process group, so the OLD supervisor survived while a NEW one
was started against the same ATTENTION_HOME. Both loops observed the same queue
transitions and each emitted its own ``packet:answered`` / ``worker:finished``
events + ledger lines → duplicated events (4 instead of 2), silently violating
the single-writer invariant documented in state.py.

The fix: ``Supervisor.run()`` takes an exclusive ``flock`` on
``<home>/supervisor.lock`` and FAILS LOUD if another supervisor holds it.
flock is kernel-owned — it dies with the process (even SIGKILL), so the D5
kill/restart path still works with no stale-lock handling.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from attention_manager.packet import Option, Packet, Source
from attention_manager.queue import PacketQueue
from attention_manager.supervisor import Supervisor
from attention_manager.workers import Observation

SRC_DIR = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("ATTENTION_HOME", str(root))
    return root


@pytest.fixture
def queue(queue_root) -> PacketQueue:
    return PacketQueue(queue_root)


@pytest.fixture
def no_tmux_needed(monkeypatch):
    """run() must be testable without tmux — patch the loud precondition."""
    import attention_manager.supervisor as sup_mod

    monkeypatch.setattr(sup_mod.workers_mod, "require_tmux", lambda: "/usr/bin/tmux")


def make_packet(question: str = "A or B?") -> Packet:
    return Packet(
        question=question,
        options=[Option(id="A", label="Option A"), Option(id="B", label="Option B")],
        source=Source(kind="decision", muxplex_session="am-test"),
    )


def make_supervisor(home, queue, **kwargs) -> Supervisor:
    return Supervisor(
        home=home,
        queue=queue,
        list_sessions=list,
        observe=lambda session, log: Observation(alive=True, exit_code=None, sentinel_seen=False, session_id=None),
        **kwargs,
    )


def read_events(home: Path, name: str) -> list[dict]:
    path = home / "events.jsonl"
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [e for e in events if e.get("event") == name]


class TestInstanceLockInProcess:
    def test_second_run_refused_while_lock_held(self, home, queue):
        """The exact S4 mode at unit level: a live supervisor holds the home;
        a second run() against the same home must fail loud BEFORE writing
        any event — never silently double-write."""
        sup1 = make_supervisor(home, queue)
        sup1._acquire_instance_lock()  # what run() does first
        try:
            sup2 = make_supervisor(home, queue)
            with pytest.raises(RuntimeError, match="another supervisor"):
                sup2.run(once=True)
            # The refused supervisor must not have emitted anything.
            assert read_events(home, "supervisor:started") == []
        finally:
            sup1._release_instance_lock()

    def test_lock_released_after_once_run(self, home, queue, no_tmux_needed):
        """A cleanly-finished supervisor releases the lock — sequential runs OK."""
        make_supervisor(home, queue).run(once=True)
        make_supervisor(home, queue).run(once=True)  # must not raise
        assert len(read_events(home, "supervisor:started")) == 2

    def test_lock_file_records_holder_pid(self, home, queue):
        sup = make_supervisor(home, queue)
        sup._acquire_instance_lock()
        try:
            assert (home / "supervisor.lock").read_text(encoding="utf-8").strip() == str(os.getpid())
        finally:
            sup._release_instance_lock()


# -- subprocess tests: the real two-process mode ------------------------------------


def _spawn_supervise(home: Path, queue_root: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "ATTENTION_HOME": str(home),
        "ATTENTION_QUEUE_DIR": str(queue_root),
        "PYTHONPATH": str(SRC_DIR) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "attention_manager.cli", "supervise", "--interval", "0.2"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(predicate, timeout_s: float = 15.0, interval_s: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return False


needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required (supervise fails loud without)")


@needs_tmux
class TestInstanceLockAcrossProcesses:
    def test_second_supervise_process_refused_no_duplicate_events(self, home, queue):
        """End-to-end reproduction of the S4 duplication mode: while one
        supervise process owns the home, a second one must exit non-zero with
        the lock error — and a packet answered afterwards must produce exactly
        ONE packet:answered event (not one per supervisor)."""
        proc_a = _spawn_supervise(home, queue.root)
        second = None
        try:
            assert _wait_for(lambda: read_events(home, "supervisor:started")), "first supervisor never started"

            second = subprocess.Popen(
                [sys.executable, "-m", "attention_manager.cli", "supervise", "--interval", "0.2"],
                env={
                    **os.environ,
                    "ATTENTION_HOME": str(home),
                    "ATTENTION_QUEUE_DIR": str(queue.root),
                    "PYTHONPATH": str(SRC_DIR),
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                second.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "second supervisor RAN CONCURRENTLY against the same home "
                    "(single-instance lock missing — the S4 duplicate-events mode)"
                )
            assert second.returncode == 1, f"expected loud refusal (exit 1), got {second.returncode}"
            stderr = second.stderr.read() if second.stderr else ""
            assert "another supervisor" in stderr, f"missing lock error, stderr: {stderr!r}"

            # Only supervisor A observes the answer — exactly one event.
            pkt = make_packet()
            queue.write(pkt)
            queue.answer(pkt.id, "A", rationale="test", answered_by="human")
            assert _wait_for(lambda: len(read_events(home, "packet:answered")) >= 1), "no packet:answered emitted"
            time.sleep(1.0)  # a few more ticks — any duplicate would land now
            answered = read_events(home, "packet:answered")
            assert len(answered) == 1, f"duplicate packet:answered events: {answered}"
            assert len(read_events(home, "supervisor:started")) == 1
        finally:
            for proc in (second, proc_a):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)

    def test_sigkill_releases_lock_for_restart(self, home, queue, no_tmux_needed):
        """D5 kill/restart must keep working: flock dies with the SIGKILLed
        process, so the restarted supervisor acquires the lock cleanly."""
        proc = _spawn_supervise(home, queue.root)
        try:
            assert _wait_for(lambda: read_events(home, "supervisor:started")), "supervisor never started"
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
            # Restart (in-process, single tick) — must not raise.
            make_supervisor(home, queue).run(once=True)
            assert len(read_events(home, "supervisor:started")) == 2
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
