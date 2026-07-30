"""Tests for the supervisor tick loop: seen-set diffing, restart rebuild,
worker lifecycle events, notification wiring. tmux is NOT required here —
the worker-observation backend is injected (the tick logic is what's under
test; real tmux is covered by test_workers.py and the smoke script)."""

from pathlib import Path

import pytest

from attention_manager.notify import BatchItem
from attention_manager.packet import Option, Packet, Source
from attention_manager.queue import PacketQueue
from attention_manager.supervisor import Supervisor
from attention_manager.workers import Observation


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("ATTENTION_HOME", str(root))
    return root


@pytest.fixture
def queue(queue_root) -> PacketQueue:
    return PacketQueue(queue_root)


def make_packet(question: str = "A or B?") -> Packet:
    return Packet(
        question=question,
        options=[Option(id="A", label="Option A"), Option(id="B", label="Option B")],
        source=Source(kind="decision", muxplex_session="am-test"),
    )


def make_supervisor(home, queue, sessions=None, observations=None, **kwargs) -> Supervisor:
    """Supervisor with an injected (no-tmux) worker backend.

    Creates a workers/<session>/ dir for each injected session — adoption is
    HOME-SCOPED (D10): only sessions dispatched by this home are ours.
    """
    observations = observations or {}
    for session in sessions or []:
        (Path(home) / "workers" / session).mkdir(parents=True, exist_ok=True)
    return Supervisor(
        home=home,
        queue=queue,
        list_sessions=lambda: list(sessions or []),
        observe=lambda session, log: observations.get(
            session, Observation(alive=True, exit_code=None, sentinel_seen=False, session_id=None)
        ),
        **kwargs,
    )


def events_of(supervisor: Supervisor, name: str) -> list[dict]:
    return [e for e in supervisor.state.read_events() if e["event"] == name]


class TestPacketDiffing:
    def test_packet_created_emitted_once(self, home, queue):
        pkt = make_packet()
        queue.write(pkt)
        sup = make_supervisor(home, queue)
        sup.tick()
        sup.tick()  # second tick must NOT re-emit
        created = events_of(sup, "packet:created")
        assert len(created) == 1
        assert created[0]["packet_id"] == pkt.id
        assert created[0]["question"] == pkt.question
        ledger = [e for e in sup.state.ledger_read() if e["kind"] == "packet_created"]
        assert len(ledger) == 1

    def test_packet_answered_with_latency_and_resolution(self, home, queue):
        pkt = make_packet()
        queue.write(pkt)
        sup = make_supervisor(home, queue)
        sup.tick()
        queue.answer(pkt.id, "B", rationale="test", answered_by="human")
        sup.tick()
        sup.tick()
        answered = events_of(sup, "packet:answered")
        assert len(answered) == 1
        assert answered[0]["resolution"]["answer"] == "B"
        assert answered[0]["latency_s"] is not None and answered[0]["latency_s"] >= 0
        ledger = [e for e in sup.state.ledger_read() if e["kind"] == "packet_answered"]
        assert len(ledger) == 1

    def test_first_sight_in_answered_emits_only_answered(self, home, queue):
        sup = make_supervisor(home, queue)  # supervisor already running...
        pkt = make_packet()
        queue.write(pkt)
        queue.answer(pkt.id, "A")  # ...packet created AND answered between ticks
        sup.tick()
        assert len(events_of(sup, "packet:answered")) == 1
        assert len(events_of(sup, "packet:created")) == 0

    def test_restart_no_duplicate_events(self, home, queue):
        """D5: state written by one instance, fresh instance rebuilds — no dupes."""
        pkt = make_packet()
        queue.write(pkt)
        first = make_supervisor(home, queue)
        first.tick()  # sees the packet, persists state.json

        second = make_supervisor(home, queue)  # simulated restart
        assert second.state.loaded_from_snapshot is True
        second.tick()
        second.tick()
        assert len(events_of(second, "packet:created")) == 1  # only the original

    def test_fresh_home_existing_answered_not_replayed(self, home, queue):
        pkt = make_packet()
        queue.write(pkt)
        queue.answer(pkt.id, "A")
        sup = make_supervisor(home, queue)  # no state.json → rebuild from queue dirs
        sup.tick()
        assert len(events_of(sup, "packet:answered")) == 0  # history, not replayed
        assert len(events_of(sup, "state:rebuilt")) == 1

    def test_malformed_packet_file_loud_but_nonfatal(self, home, queue):
        (queue.dir("pending") / "pkt-bad.json").write_text("{nope", encoding="utf-8")
        good = make_packet()
        queue.write(good)
        sup = make_supervisor(home, queue)
        sup.tick()
        sup.tick()
        assert len(events_of(sup, "queue:error")) == 1  # reported once, not every tick
        assert len(events_of(sup, "packet:created")) == 1  # good packet still tracked


class TestWorkerLifecycle:
    def test_started_then_finished_judged_false(self, home, queue):
        observations = {"am-w1": Observation(alive=True, exit_code=None, sentinel_seen=False, session_id=None)}
        sup = make_supervisor(home, queue, sessions=["am-w1"], observations=observations)
        sup.tick()
        assert len(events_of(sup, "worker:started")) == 1
        assert len(events_of(sup, "worker:finished")) == 0

        observations["am-w1"] = Observation(alive=True, exit_code=0, sentinel_seen=True, session_id=None)
        sup.tick()
        sup.tick()  # finished workers are not re-observed
        assert len(events_of(sup, "worker:started")) == 1
        finished = events_of(sup, "worker:finished")
        assert len(finished) == 1
        assert finished[0]["exit_code"] == 0
        assert finished[0]["judged"] is False  # loop:closed is judge-gated (step 4)
        ledger = [e for e in sup.state.ledger_read() if e["kind"] == "worker_finished"]
        assert len(ledger) == 1 and ledger[0]["judged"] is False

    def test_dead_session_without_sentinel_is_loud(self, home, queue):
        observations = {"am-w1": Observation(alive=False, exit_code=None, sentinel_seen=False, session_id=None)}
        sup = make_supervisor(home, queue, sessions=["am-w1"], observations=observations)
        sup.tick()
        finished = events_of(sup, "worker:finished")
        assert len(finished) == 1
        assert finished[0]["exit_code"] is None
        assert finished[0]["sentinel_missing"] is True

    def test_worker_flags_survive_restart(self, home, queue):
        observations = {"am-w1": Observation(alive=True, exit_code=0, sentinel_seen=True, session_id=None)}
        first = make_supervisor(home, queue, sessions=["am-w1"], observations=observations)
        first.tick()

        second = make_supervisor(home, queue, sessions=["am-w1"], observations=observations)
        second.tick()
        # started/finished were already emitted by the first instance
        all_events = first.state.read_events()  # same events.jsonl file
        assert len([e for e in all_events if e["event"] == "worker:started"]) == 1
        assert len([e for e in all_events if e["event"] == "worker:finished"]) == 1

    def test_session_id_captured_from_log(self, home, queue):
        observations = {"am-w1": Observation(alive=True, exit_code=None, sentinel_seen=False, session_id="abc-123-def")}
        sup = make_supervisor(home, queue, sessions=["am-w1"], observations=observations)
        sup.tick()
        assert sup.state.workers["am-w1"]["amplifier_session_id"] == "abc-123-def"

    def test_judge_result_persisted_on_worker_record(self, home, queue):
        """Loop outcome must survive on the worker record (not just the
        ledger) so `status` can render closed/failed — defect (UX round 2,
        Sam STRIKE #2): a judge-failed loop was indistinguishable from a
        success in status."""
        obs = Observation(alive=False, exit_code=0, sentinel_seen=True, session_id=None)
        sup = make_supervisor(home, queue, sessions=["am-w1"], observations={"am-w1": obs})
        sup.state.adopt_workers(["am-w1"])
        sup.state.workers["am-w1"]["judge_cmd"] = "false"  # real judge, fails
        sup.tick()
        record = sup.state.workers["am-w1"]
        assert record["judged"] is True and record["judge_result"] == "failed"

        # And a passing judge persists "closed".
        (home / "workers" / "am-w2").mkdir(parents=True, exist_ok=True)
        sup2 = make_supervisor(home, queue, sessions=["am-w2"], observations={"am-w2": obs})
        sup2.state.adopt_workers(["am-w2"])
        sup2.state.workers["am-w2"]["judge_cmd"] = "true"  # real judge, passes
        sup2.tick()
        record2 = sup2.state.workers["am-w2"]
        assert record2["judged"] is True and record2["judge_result"] == "closed"

    def test_unjudged_finish_persists_no_judge_result(self, home, queue):
        obs = Observation(alive=False, exit_code=0, sentinel_seen=True, session_id=None)
        sup = make_supervisor(home, queue, sessions=["am-w1"], observations={"am-w1": obs})
        sup.tick()
        record = sup.state.workers["am-w1"]
        assert record["judged"] is False and record["judge_result"] is None


class TestUnjudgedFailureIsLoud:
    """Defect (UX round 1): an unjudged worker dying rc=1 produced ZERO signal
    while its judged twin got a loud LOOP FAILED. Unjudged failure is now loud:
    worker:failed event + worker_failed ledger + notify (kind worker_failed) +
    bell (trigger worker_failed). Unjudged exit-0 stays quiet — that's success."""

    def _sup(self, home, queue, obs, rings, **kwargs):
        sup = make_supervisor(
            home,
            queue,
            sessions=["am-w1"],
            observations={"am-w1": obs},
            ring=rings.append,
            **kwargs,
        )
        return sup

    def test_nonzero_exit_unjudged_is_loud(self, home, queue, capsys):
        rings: list[str] = []
        obs = Observation(alive=False, exit_code=3, sentinel_seen=True, session_id=None)
        sup = self._sup(home, queue, obs, rings, notify_spec="console", batch_window_s=0.0)
        sup.tick()
        sup.tick()  # finished workers are not re-observed — everything fires ONCE

        failed = events_of(sup, "worker:failed")
        assert len(failed) == 1
        assert failed[0]["exit_code"] == 3 and "exited 3" in failed[0]["reason"]
        ledger = [e for e in sup.state.ledger_read() if e["kind"] == "worker_failed"]
        assert len(ledger) == 1 and ledger[0]["session"] == "am-w1"
        # Bell rang with the worker_failed trigger, exactly once.
        assert rings == ["am-w1"]
        rung = events_of(sup, "bell:rung")
        assert len(rung) == 1 and rung[0]["trigger"] == "worker_failed"
        # Notification enqueued with the worker_failed kind (console sink prints it).
        out = capsys.readouterr().out
        assert "WORKER FAILED" in out and "[worker_failed]" in out

    def test_dead_session_without_sentinel_is_worker_failed(self, home, queue):
        rings: list[str] = []
        obs = Observation(alive=False, exit_code=None, sentinel_seen=False, session_id=None)
        sup = self._sup(home, queue, obs, rings)
        sup.tick()
        failed = events_of(sup, "worker:failed")
        assert len(failed) == 1
        assert "without an exit sentinel" in failed[0]["reason"]
        assert rings == ["am-w1"]

    def test_unjudged_exit_zero_stays_quiet(self, home, queue):
        rings: list[str] = []
        obs = Observation(alive=False, exit_code=0, sentinel_seen=True, session_id=None)
        sup = self._sup(home, queue, obs, rings)
        sup.tick()
        assert events_of(sup, "worker:failed") == []
        assert rings == []
        assert [e for e in sup.state.ledger_read() if e["kind"] == "worker_failed"] == []

    def test_judged_failure_is_loop_failed_not_worker_failed(self, home, queue):
        """A judged worker's failure is the judge's story (loop:failed) —
        never double-reported as worker_failed."""
        rings: list[str] = []
        obs = Observation(alive=False, exit_code=1, sentinel_seen=True, session_id=None)
        sup = self._sup(home, queue, obs, rings)
        sup.state.adopt_workers(["am-w1"])
        sup.state.workers["am-w1"]["judge_cmd"] = "false"  # judge fails
        sup.tick()
        assert events_of(sup, "worker:failed") == []
        assert len(events_of(sup, "loop:failed")) == 1
        rung = events_of(sup, "bell:rung")
        assert [e["trigger"] for e in rung] == ["loop_failed"]

    def test_bells_off_suppresses_worker_failed_ring(self, home, queue):
        rings: list[str] = []
        obs = Observation(alive=False, exit_code=2, sentinel_seen=True, session_id=None)
        sup = self._sup(home, queue, obs, rings, bells=False)
        sup.tick()
        assert len(events_of(sup, "worker:failed")) == 1  # still loud on event/ledger
        assert rings == []


class TestHomeScopedAdoption:
    """D10: only sessions dispatched by THIS home (workers/<session>/ dir under
    it) are adopted. Another home's am-* sessions on the same tmux server must
    never appear in this home's status, events, or ledger."""

    def test_foreign_am_session_is_ignored(self, home, queue):
        sup = Supervisor(
            home=home,
            queue=queue,
            list_sessions=lambda: ["am-someone-elses"],
            observe=lambda s, log: Observation(alive=True, exit_code=None, sentinel_seen=False, session_id=None),
        )
        sup.tick()
        assert "am-someone-elses" not in sup.state.workers
        assert events_of(sup, "worker:started") == []

    def test_own_session_with_dir_is_adopted(self, home, queue):
        sup = make_supervisor(home, queue, sessions=["am-mine"])  # helper creates the dir
        sup.tick()
        assert "am-mine" in sup.state.workers
        assert len(events_of(sup, "worker:started")) == 1


class TestNotificationWiring:
    def test_created_packet_enqueued_and_flushed(self, home, queue, tmp_path):
        notify_file = tmp_path / "notify.jsonl"
        pkt = make_packet()
        queue.write(pkt)
        sup = make_supervisor(
            home, queue, notify_spec=f"file:{notify_file}", batch_window_s=0.0
        )  # window 0 → flush immediately
        sup.tick()
        assert notify_file.exists()
        assert pkt.id in notify_file.read_text(encoding="utf-8")
        assert len(events_of(sup, "notify:batch_sent")) == 1
        ledger = [e for e in sup.state.ledger_read() if e["kind"] == "notified_batch"]
        assert len(ledger) == 1

    def test_no_sink_warns_loudly_once(self, home, queue, capsys):
        queue.write(make_packet())
        sup = make_supervisor(home, queue)  # no notify spec
        sup.tick()
        queue.write(make_packet("second?"))
        sup.tick()
        assert len(events_of(sup, "notify:disabled")) == 1  # one-time, not per packet

    def test_sink_failure_logged_and_retried(self, home, queue):
        class FlakySink:
            name = "flaky"

            def __init__(self):
                self.fail = True

            def deliver(self, items: list[BatchItem]) -> None:
                if self.fail:
                    raise ConnectionError("down")

        pkt = make_packet()
        queue.write(pkt)
        sup = make_supervisor(home, queue, notify_spec="console", batch_window_s=0.0)
        flaky = FlakySink()
        assert sup.batcher is not None
        sup.batcher.sink = flaky
        sup.tick()
        assert len(events_of(sup, "notify:error")) == 1
        assert sup.batcher is not None and len(sup.batcher.items) == 1  # retained

        flaky.fail = False
        sup.tick()
        assert len(events_of(sup, "notify:batch_sent")) == 1
        assert sup.batcher is not None and sup.batcher.items == []
