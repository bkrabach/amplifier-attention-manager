"""Tests for supervisor state: home resolution, atomic snapshot, events, ledger,
rebuild-from-disk (D5), and worker adoption."""

import json
from pathlib import Path

import pytest
from attention_manager.state import SupervisorState
from attention_manager.state import default_home
from attention_manager.state import new_worker_record
from attention_manager.state import utc_today


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "attention-home"
    monkeypatch.setenv("ATTENTION_HOME", str(root))
    return root


class TestHomeResolution:
    def test_env_override(self, home):
        assert default_home() == home
        assert SupervisorState().home == home

    def test_default_fallback(self, monkeypatch):
        monkeypatch.delenv("ATTENTION_HOME", raising=False)
        resolved = default_home()
        assert resolved == Path("~/.amplifier/attention").expanduser()
        assert "~" not in str(resolved)

    def test_explicit_home_wins(self, home, tmp_path):
        explicit = tmp_path / "elsewhere"
        assert SupervisorState(explicit).home == explicit


class TestSnapshot:
    def test_save_load_roundtrip(self, home):
        state = SupervisorState(home)
        state.seen_pending = {"pkt-a", "pkt-b"}
        state.seen_answered = {"pkt-a"}
        state.workers["am-x"] = new_worker_record("x", "am-x", cmd="true", task="t")
        state.workers["am-x"]["started_event_emitted"] = True
        state.save()

        fresh = SupervisorState(home)
        fresh.load()
        assert fresh.loaded_from_snapshot is True
        assert fresh.seen_pending == {"pkt-a", "pkt-b"}
        assert fresh.seen_answered == {"pkt-a"}
        assert fresh.workers["am-x"]["started_event_emitted"] is True

    def test_save_is_atomic_no_tmp_leftover(self, home):
        state = SupervisorState(home)
        state.save()
        assert (home / "state.json").exists()
        assert not list(home.glob("*.tmp"))

    def test_load_missing_is_fresh(self, home):
        state = SupervisorState(home)
        state.load()
        assert state.loaded_from_snapshot is False
        assert state.seen_pending == set()

    def test_load_corrupt_fails_loud(self, home):
        home.mkdir(parents=True)
        (home / "state.json").write_text("{not json", encoding="utf-8")
        state = SupervisorState(home)
        with pytest.raises(ValueError, match="corrupt state file"):
            state.load()


class TestEventsAndLedger:
    def test_append_and_read_events(self, home):
        state = SupervisorState(home)
        state.append_event("packet:created", packet_id="pkt-1")
        state.append_event("worker:finished", session="am-x", exit_code=0, judged=False)
        events = state.read_events()
        assert [e["event"] for e in events] == ["packet:created", "worker:finished"]
        assert all("ts" in e for e in events)
        assert events[1]["judged"] is False

    def test_events_are_one_json_object_per_line(self, home):
        state = SupervisorState(home)
        state.append_event("a")
        state.append_event("b")
        lines = (home / "events.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert all(isinstance(json.loads(line), dict) for line in lines)

    def test_ledger_append_read_today(self, home):
        state = SupervisorState(home)
        state.ledger_append("dispatched", session="am-x")
        state.ledger_append("worker_finished", session="am-x", exit_code=0)
        entries = state.ledger_read()
        assert [e["kind"] for e in entries] == ["dispatched", "worker_finished"]
        assert state.ledger_path().name == f"{utc_today()}.jsonl"

    def test_ledger_read_specific_date_empty(self, home):
        state = SupervisorState(home)
        assert state.ledger_read("1999-01-01") == []


class TestRebuildFromDisk:
    def test_answered_seeded_pending_not(self, home):
        state = SupervisorState(home)
        counts = state.rebuild_seen_from_queue(pending_ids=["pkt-p1", "pkt-p2"], answered_ids=["pkt-a1"])
        # answered history is pre-marked seen (never replayed)...
        assert "pkt-a1" in state.seen_answered
        assert "pkt-a1" in state.seen_pending
        # ...but still-pending packets are NOT (they still need attention).
        assert "pkt-p1" not in state.seen_pending
        assert counts == {"pending_unseen": 2, "answered_seeded": 1}


class TestAdoptWorkers:
    def _write_meta(self, home: Path, session: str, **extra) -> None:
        d = home / "workers" / session
        d.mkdir(parents=True)
        meta = {"name": session.removeprefix("am-"), "session": session, "cmd": "true", "task": "t", **extra}
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def test_adopt_from_dirs_and_tmux(self, home):
        self._write_meta(home, "am-disk")
        state = SupervisorState(home)
        added = state.adopt_workers(["am-live", "other-session"])
        assert sorted(added) == ["am-disk", "am-live"]
        assert state.workers["am-disk"]["cmd"] == "true"
        assert state.workers["am-live"]["adopted_without_meta"] is True
        assert "other-session" not in state.workers  # non-am-* ignored

    def test_adopt_is_idempotent_and_preserves_flags(self, home):
        self._write_meta(home, "am-disk")
        state = SupervisorState(home)
        state.adopt_workers([])
        state.workers["am-disk"]["finished"] = True
        assert state.adopt_workers(["am-disk"]) == []
        assert state.workers["am-disk"]["finished"] is True

    def test_malformed_meta_is_loud_but_nonfatal(self, home):
        d = home / "workers" / "am-broken"
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{nope", encoding="utf-8")
        state = SupervisorState(home)
        assert state.adopt_workers([]) == []
        events = state.read_events()
        assert events and events[0]["event"] == "state:error"
