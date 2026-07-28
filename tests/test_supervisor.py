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
    """Supervisor with an injected (no-tmux) worker backend."""
    observations = observations or {}
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
