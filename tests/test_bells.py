"""Tests for the muxplex bell surface (bells.py + supervisor integration).

Two layers, same convention as test_workers.py / test_judge.py:

* REAL-tmux tests prove the ring mechanism itself — BEL written to the pane
  tty from OUTSIDE the session sets window_bell_flag=1 (trivial commands, no
  LLM). Skipped with a LOUD reason only when tmux is absent.
* Pure-logic tests inject a ring recorder (no tmux) and cover the supervisor
  join/idempotency/failure policy: late binding, ring-once across restarts,
  silent skip for unmatched packets, --no-bells, and the loud failure path.
"""

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from attention_manager import bells
from attention_manager.cli import build_parser
from attention_manager.packet import Option, Packet, Source
from attention_manager.queue import PacketQueue
from attention_manager.supervisor import Supervisor
from attention_manager.workers import Observation

TMUX_PRESENT = shutil.which("tmux") is not None
requires_tmux = pytest.mark.skipif(
    not TMUX_PRESENT,
    reason=(
        "LOUD SKIP: tmux is NOT installed on this machine — the real-tmux bell tests "
        "did NOT run. Install tmux to prove the ring mechanism for real."
    ),
)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("ATTENTION_HOME", str(root))
    return root


@pytest.fixture
def queue(queue_root) -> PacketQueue:
    return PacketQueue(queue_root)


class RingRecorder:
    """Injectable ring seam: records calls; optionally fails like a bad tty."""

    def __init__(self, fail: bool = False):
        self.calls: list[str] = []
        self.fail = fail

    def __call__(self, session: str) -> None:
        self.calls.append(session)
        if self.fail:
            raise RuntimeError("simulated tty write failure")


def make_packet(question: str = "A or B?", session_id: str | None = None) -> Packet:
    return Packet(
        question=question,
        options=[Option(id="A", label="Option A"), Option(id="B", label="Option B")],
        source=Source(kind="decision", session_id=session_id, muxplex_session="am-test"),
    )


def make_supervisor(home, queue, sessions=None, observations=None, ring=None, **kwargs) -> Supervisor:
    """Supervisor with injected (no-tmux) worker + ring backends."""
    observations = observations or {}
    return Supervisor(
        home=home,
        queue=queue,
        list_sessions=lambda: list(sessions or []),
        observe=lambda session, log: observations.get(
            session, Observation(alive=True, exit_code=None, sentinel_seen=False, session_id=None)
        ),
        ring=ring if ring is not None else RingRecorder(),
        **kwargs,
    )


def seed_worker(home: Path, session: str, judge_cmd: str | None = None) -> None:
    """Create workers/<session>/{meta.json,worker.log} so adopt_workers picks it up."""
    worker_dir = home / "workers" / session
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "worker.log").write_text("", encoding="utf-8")
    meta = {"name": session.removeprefix("am-"), "session": session, "cmd": "true", "judge_cmd": judge_cmd}
    (worker_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def events_of(sup: Supervisor, name: str) -> list[dict]:
    return [e for e in sup.state.read_events() if e["event"] == name]


OBS_RUNNING_WITH_ID = Observation(alive=True, exit_code=None, sentinel_seen=False, session_id="sid-123")
FINISHED_OK = Observation(alive=True, exit_code=0, sentinel_seen=True, session_id=None)


# -- the real thing: BEL to pane tty sets window_bell_flag ---------------------------


@requires_tmux
class TestRingBellRealTmux:
    def _spawn(self) -> str:
        session = f"am-belltest-{uuid.uuid4().hex[:8]}"
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "sleep 60"], check=True, capture_output=True)
        return session

    def _flag(self, session: str) -> str:
        proc = subprocess.run(
            ["tmux", "list-windows", "-t", f"={session}", "-F", "#{window_bell_flag}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    def _wait_flag(self, session: str, want: str, timeout_s: float = 5.0) -> str:
        deadline = time.monotonic() + timeout_s
        flag = self._flag(session)
        while flag != want and time.monotonic() < deadline:
            time.sleep(0.1)
            flag = self._flag(session)
        return flag

    def test_ring_sets_window_bell_flag_on_detached_session(self):
        """THE proof: ring from outside a detached session with a busy (sleep)
        process inside; the window must register a bell (flag=1)."""
        session = self._spawn()
        try:
            assert self._flag(session) == "0"
            bells.ring_bell(session)
            assert self._wait_flag(session, "1") == "1"
        finally:
            subprocess.run(["tmux", "kill-session", "-t", f"={session}"], capture_output=True, check=False)

    def test_ring_overrides_monitor_bell_off_and_ignores_bell_action(self):
        """Documented findings (tmux 3.4): monitor-bell off SWALLOWS the bell
        (no flag) — ring_bell handles it by enforcing monitor-bell on for the
        target window (our own am-* sessions) before writing BEL. bell-action
        only gates alert actions toward attached clients, never the flag.
        Window targeting uses #{window_id} because base-index is user-config
        (this host uses 1 — '=session:0' targeting silently fails)."""
        session = self._spawn()
        try:
            window_id = subprocess.run(
                ["tmux", "display-message", "-p", "-t", f"={session}:", "#{window_id}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            subprocess.run(["tmux", "set-option", "-w", "-t", window_id, "monitor-bell", "off"], check=True)
            # NOTE: set-option session targets reject the '=' exact-match prefix
            # (tmux 3.4: "no such session: =name") — bare name required here.
            subprocess.run(["tmux", "set-option", "-t", session, "bell-action", "none"], check=True)
            bells.ring_bell(session)  # must enforce monitor-bell on, then ring
            assert self._wait_flag(session, "1") == "1"
            monitor = subprocess.run(
                ["tmux", "show-options", "-w", "-t", window_id, "monitor-bell"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert monitor == "monitor-bell on"
        finally:
            subprocess.run(["tmux", "kill-session", "-t", f"={session}"], capture_output=True, check=False)

    def test_ring_unknown_session_raises(self):
        # Depending on tmux state, the query either exits nonzero ("can't find
        # pane") or exits 0 with EMPTY output — both must raise, never ring.
        with pytest.raises(RuntimeError, match=r"cannot (ring bell|resolve pane tty)"):
            bells.ring_bell(f"am-no-such-session-{uuid.uuid4().hex[:8]}")


# -- supervisor integration: join, late binding, idempotency ------------------------


class TestPacketBell:
    def test_rings_when_join_resolves_same_tick(self, home, queue):
        queue.write(make_packet(session_id="sid-123"))
        ring = RingRecorder()
        sup = make_supervisor(home, queue, sessions=["am-w1"], observations={"am-w1": OBS_RUNNING_WITH_ID}, ring=ring)
        sup.tick()
        assert ring.calls == ["am-w1"]
        rung = events_of(sup, "bell:rung")
        assert len(rung) == 1
        assert rung[0]["session"] == "am-w1"
        assert rung[0]["trigger"] == "packet"
        assert rung[0]["packet_id"] == queue.list_pending()[0].id

    def test_late_binding_session_id_appears_after_packet(self, home, queue):
        """The amplifier session id may show up in worker.log AFTER the packet
        is created — the join must be retried on subsequent ticks."""
        pkt = make_packet(session_id="sid-123")
        queue.write(pkt)
        observations = {"am-w1": Observation(alive=True, exit_code=None, sentinel_seen=False, session_id=None)}
        ring = RingRecorder()
        sup = make_supervisor(home, queue, sessions=["am-w1"], observations=observations, ring=ring)
        sup.tick()
        sup.tick()
        assert ring.calls == []  # id not observed yet — candidate waits, no error, no events
        assert events_of(sup, "bell:rung") == [] and events_of(sup, "bell:error") == []
        assert sup.state.ring_candidates == {pkt.id: "sid-123"}

        observations["am-w1"] = OBS_RUNNING_WITH_ID  # the log now shows "Session ID: sid-123"
        sup.tick()
        assert ring.calls == ["am-w1"]
        assert len(events_of(sup, "bell:rung")) == 1
        assert sup.state.ring_candidates == {}
        assert pkt.id in sup.state.rung_packets

    def test_ring_once_across_restart(self, home, queue):
        pkt = make_packet(session_id="sid-123")
        queue.write(pkt)
        first_ring = RingRecorder()
        first = make_supervisor(
            home, queue, sessions=["am-w1"], observations={"am-w1": OBS_RUNNING_WITH_ID}, ring=first_ring
        )
        first.tick()
        assert first_ring.calls == ["am-w1"]

        second_ring = RingRecorder()
        second = make_supervisor(  # simulated restart, state.json intact
            home, queue, sessions=["am-w1"], observations={"am-w1": OBS_RUNNING_WITH_ID}, ring=second_ring
        )
        assert pkt.id in second.state.rung_packets  # persisted (D5)
        second.tick()
        second.tick()
        assert second_ring.calls == []  # never re-rings
        assert len(events_of(second, "bell:rung")) == 1  # only the original event

    def test_unjoined_candidate_survives_restart_then_rings(self, home, queue):
        """A packet created just before a restart, whose session id binds only
        after the restart, must still ring (candidates persisted, D5)."""
        pkt = make_packet(session_id="sid-123")
        queue.write(pkt)
        first = make_supervisor(home, queue, sessions=["am-w1"], ring=RingRecorder())
        first.tick()  # candidate recorded, join unresolved
        assert first.state.ring_candidates == {pkt.id: "sid-123"}

        ring = RingRecorder()
        second = make_supervisor(
            home, queue, sessions=["am-w1"], observations={"am-w1": OBS_RUNNING_WITH_ID}, ring=ring
        )
        second.tick()
        assert ring.calls == ["am-w1"]
        assert len(events_of(second, "bell:rung")) == 1

    def test_no_matching_worker_is_silent_skip(self, home, queue):
        """Recipe gates / standalone workunits / seeded packets: no worker ever
        claims the id (or there is no id at all) → no bell, no error, no spam."""
        queue.write(make_packet(session_id="sid-orphan"))  # id no worker claims
        queue.write(make_packet("no id at all?"))  # session_id=None → never a candidate
        ring = RingRecorder()
        sup = make_supervisor(home, queue, sessions=["am-w1"], observations={"am-w1": OBS_RUNNING_WITH_ID}, ring=ring)
        for _ in range(5):
            sup.tick()
        assert ring.calls == []
        assert events_of(sup, "bell:rung") == []
        assert events_of(sup, "bell:error") == []
        assert len(sup.state.ring_candidates) == 1  # only the orphan id waits

    def test_answered_packet_retires_unrung_candidate(self, home, queue):
        pkt = make_packet(session_id="sid-orphan")
        queue.write(pkt)
        ring = RingRecorder()
        sup = make_supervisor(home, queue, ring=ring)
        sup.tick()
        assert sup.state.ring_candidates == {pkt.id: "sid-orphan"}
        queue.answer(pkt.id, "A")
        sup.tick()
        assert sup.state.ring_candidates == {}  # the human got there without the bell
        assert ring.calls == []

    def test_no_bells_disables_everything(self, home, queue):
        queue.write(make_packet(session_id="sid-123"))
        seed_worker(home, "am-failed", judge_cmd='echo "FAIL: broken"; exit 1')
        ring = RingRecorder()
        sup = make_supervisor(
            home,
            queue,
            sessions=["am-w1", "am-failed"],
            observations={"am-w1": OBS_RUNNING_WITH_ID, "am-failed": FINISHED_OK},
            ring=ring,
            bells=False,
        )
        sup.tick()
        sup.tick()
        assert len(events_of(sup, "loop:failed")) == 1  # the loop_failed trigger DID fire...
        assert ring.calls == []  # ...but bells are off: no ring
        assert events_of(sup, "bell:rung") == []
        assert events_of(sup, "bell:error") == []
        assert sup.state.ring_candidates == {}  # not even tracked

    def test_ring_failure_is_loud_once_per_session_never_crashes(self, home, queue, capsys):
        queue.write(make_packet("first?", session_id="sid-123"))
        queue.write(make_packet("second?", session_id="sid-123"))
        ring = RingRecorder(fail=True)
        sup = make_supervisor(home, queue, sessions=["am-w1"], observations={"am-w1": OBS_RUNNING_WITH_ID}, ring=ring)
        sup.tick()  # both candidates attempt; both fail; loop must survive
        sup.tick()
        assert len(ring.calls) == 2  # one attempt per packet — candidates retired, no per-tick retry
        errors = events_of(sup, "bell:error")
        assert len(errors) == 1  # ONE loud event per session
        assert errors[0]["session"] == "am-w1"
        assert "simulated tty write failure" in errors[0]["error"]
        assert "bell ring failed for am-w1" in capsys.readouterr().err
        assert events_of(sup, "bell:rung") == []
        assert sup.state.rung_packets == set()  # a failed ring is not a rung packet
        assert sup.state.ring_candidates == {}  # retired: no retry-spam


class TestLoopFailedBell:
    def test_loop_failed_rings_worker_session(self, home, queue):
        seed_worker(home, "am-failed", judge_cmd='echo "FAIL: broken"; exit 1')
        ring = RingRecorder()
        sup = make_supervisor(home, queue, sessions=["am-failed"], observations={"am-failed": FINISHED_OK}, ring=ring)
        sup.tick()
        assert len(events_of(sup, "loop:failed")) == 1
        assert ring.calls == ["am-failed"]
        rung = events_of(sup, "bell:rung")
        assert len(rung) == 1
        assert rung[0] == {**rung[0], "session": "am-failed", "trigger": "loop_failed"}
        assert "packet_id" not in rung[0]

    def test_loop_failed_ring_once_across_restart(self, home, queue):
        seed_worker(home, "am-failed", judge_cmd='echo "FAIL: broken"; exit 1')
        first_ring = RingRecorder()
        first = make_supervisor(
            home, queue, sessions=["am-failed"], observations={"am-failed": FINISHED_OK}, ring=first_ring
        )
        first.tick()
        assert first_ring.calls == ["am-failed"]

        second_ring = RingRecorder()
        second = make_supervisor(  # restart: finished flag persisted → no re-judge, no re-ring
            home, queue, sessions=["am-failed"], observations={"am-failed": FINISHED_OK}, ring=second_ring
        )
        second.tick()
        second.tick()
        assert second_ring.calls == []
        assert len(events_of(second, "bell:rung")) == 1

    def test_loop_closed_does_not_ring(self, home, queue):
        seed_worker(home, "am-good", judge_cmd='echo "PASS: verified"')
        ring = RingRecorder()
        sup = make_supervisor(home, queue, sessions=["am-good"], observations={"am-good": FINISHED_OK}, ring=ring)
        sup.tick()
        assert len(events_of(sup, "loop:closed")) == 1
        assert ring.calls == []  # finish lines are deck-green moments, not attention asks


class TestCliFlag:
    def test_bells_on_by_default(self):
        args = build_parser().parse_args(["supervise", "--once"])
        assert args.no_bells is False

    def test_no_bells_flag(self):
        args = build_parser().parse_args(["supervise", "--once", "--no-bells"])
        assert args.no_bells is True
