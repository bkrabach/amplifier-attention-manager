"""CLI tests — invoke main() directly with ATTENTION_QUEUE_DIR isolation."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from attention_manager import cli, workers
from attention_manager.cli import format_week_metric, main, week_metric
from attention_manager.packet import Option, Packet, Source
from attention_manager.queue import PacketQueue
from attention_manager.state import SupervisorState
from attention_manager.workers import Observation


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("ATTENTION_HOME", str(root))
    return root


def make_pending(queue_root, **overrides) -> Packet:
    queue = PacketQueue(queue_root)
    packet = Packet(
        question="A or B?",
        options=[Option(id="A", label="Option A"), Option(id="B", label="Option B")],
        source=overrides.pop("source", Source(kind="decision")),
        **overrides,
    )
    queue.write(packet)
    return packet


def make_bounced(queue_root) -> Packet:
    queue = PacketQueue(queue_root)
    packet = make_pending(queue_root)
    queue.write(packet, subdir="bounced")
    queue.path_for(packet.id, "pending").unlink()
    return packet


class TestQueueCommands:
    def test_list_empty(self, queue_root, capsys):
        assert main(["queue", "list"]) == 0
        assert "queue empty" in capsys.readouterr().out

    def test_list_empty_json(self, queue_root, capsys):
        assert main(["--json", "queue", "list"]) == 0
        assert json.loads(capsys.readouterr().out) == []

    def test_list_shows_pending(self, queue_root, capsys):
        packet = make_pending(queue_root)
        assert main(["queue", "list"]) == 0
        out = capsys.readouterr().out
        assert packet.id in out
        assert "decision" in out
        assert "batch" in out
        assert "A or B?" in out

    def test_list_json_roundtrips(self, queue_root, capsys):
        packet = make_pending(queue_root)
        assert main(["--json", "queue", "list"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1 and data[0]["id"] == packet.id

    def test_show(self, queue_root, capsys):
        packet = make_pending(queue_root)
        assert main(["queue", "show", packet.id]) == 0
        out = capsys.readouterr().out
        assert packet.id in out and "Option A" in out

    def test_show_unknown_id_errors(self, queue_root, capsys):
        assert main(["queue", "show", "pkt-nope"]) == 1
        assert "error:" in capsys.readouterr().err

    def test_path(self, queue_root, capsys):
        assert main(["queue", "path"]) == 0
        assert str(queue_root) in capsys.readouterr().out


class TestQueueListBounced:
    """Bounced packets are visible in queue list (marked BOUNCED) — a triage
    bounce must never be invisible to a human who doesn't know to look in
    bounced/."""

    def test_bounced_listed_and_marked(self, queue_root, capsys):
        pending = make_pending(queue_root)
        bounced = make_bounced(queue_root)
        assert main(["queue", "list"]) == 0
        out = capsys.readouterr().out
        assert pending.id in out and bounced.id in out
        assert "BOUNCED" in out

    def test_bounced_in_json_with_subdir(self, queue_root, capsys):
        bounced = make_bounced(queue_root)
        assert main(["--json", "queue", "list"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert [(d["id"], d["queue_subdir"]) for d in data] == [(bounced.id, "bounced")]

    def test_source_column_shows_work_unit(self, queue_root, capsys):
        make_pending(queue_root, source=Source(kind="decision", work_unit="portfix"))
        assert main(["queue", "list"]) == 0
        out = capsys.readouterr().out
        assert "SOURCE" in out and "portfix" in out

    def test_source_column_falls_back_to_muxplex_session(self, queue_root, capsys):
        make_pending(queue_root, source=Source(kind="decision", muxplex_session="am-portfix"))
        assert main(["queue", "list"]) == 0
        assert "am-portfix" in capsys.readouterr().out


class TestAnswerBouncedViaCli:
    def test_answer_bounced_packet(self, queue_root, capsys):
        bounced = make_bounced(queue_root)
        assert main(["answer", bounced.id, "A", "--rationale", "human override"]) == 0
        assert "answered" in capsys.readouterr().out
        queue = PacketQueue(queue_root)
        assert queue.locate(bounced.id)[0] == "answered"
        # And it disappears from queue list.
        assert main(["queue", "list"]) == 0
        assert "queue empty" in capsys.readouterr().out


class TestDispatchEarlyDeath:
    """dispatch must not print success for a worker that dies instantly on a
    bundle load failure or nonzero exit (defect: 'dispatched am-X' / exit 0
    while the worker died in ~2s; truth only in the dead worker's log)."""

    @pytest.fixture(autouse=True)
    def _no_wait(self, monkeypatch):
        monkeypatch.setattr(cli, "DISPATCH_EARLY_DEATH_WAIT_S", 0.0)

    def _obs(self, alive: bool, exit_code: int | None) -> Observation:
        return Observation(alive=alive, exit_code=exit_code, sentinel_seen=exit_code is not None, session_id=None)

    def test_nonzero_sentinel_warns(self, tmp_path):
        log = tmp_path / "worker.log"
        log.write_text("boom\n__AM_WORKER_EXIT:2__\n", encoding="utf-8")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(workers, "observe", lambda s, p: self._obs(alive=True, exit_code=2))
            warning = cli._early_death_warning("am-x", log)
        assert warning is not None and "exit 2" in warning and str(log) in warning

    def test_dead_with_load_failure_pattern_warns(self, tmp_path):
        log = tmp_path / "worker.log"
        log.write_text(
            "Error: Bundle '/root/repo/bundles/test-worker.md' not found. Available bundles: foundation\n",
            encoding="utf-8",
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(workers, "observe", lambda s, p: self._obs(alive=False, exit_code=None))
            warning = cli._early_death_warning("am-x", log)
        assert warning is not None and "load failure" in warning

    def test_fast_success_stays_quiet(self, tmp_path):
        log = tmp_path / "worker.log"
        log.write_text("done\n__AM_WORKER_EXIT:0__\n", encoding="utf-8")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(workers, "observe", lambda s, p: self._obs(alive=False, exit_code=0))
            assert cli._early_death_warning("am-x", log) is None

    def test_still_running_stays_quiet(self, tmp_path):
        log = tmp_path / "worker.log"
        log.write_text("Session ID: abc\n", encoding="utf-8")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(workers, "observe", lambda s, p: self._obs(alive=True, exit_code=None))
            assert cli._early_death_warning("am-x", log) is None

    def test_dispatch_exits_nonzero_on_early_death(self, home, queue_root, monkeypatch, capsys):
        session = "am-dead"

        def fake_launch(name, cmd, home_path, task=None, judge_cmd=None):
            d = home_path / "workers" / session
            d.mkdir(parents=True, exist_ok=True)
            (d / "worker.log").write_text("oops\n__AM_WORKER_EXIT:1__\n", encoding="utf-8")
            return {"name": name, "session": session, "cmd": cmd, "task": task, "judge_cmd": judge_cmd}

        monkeypatch.setattr(workers, "launch", fake_launch)
        monkeypatch.setattr(workers, "observe", lambda s, p: self._obs(alive=False, exit_code=1))
        assert main(["dispatch", "dead", "--task", "t", "--worker-cmd", "false"]) == 1
        err = capsys.readouterr().err
        assert "ERROR" in err and "exit 1" in err
        events = SupervisorState(home).read_events()
        assert any(e["event"] == "worker:dispatch_failed" for e in events)


class TestWeekMetric:
    """The north-star instrument: escalations per HEALTHY work unit, this ISO
    week vs last, failures excluded from the denominator (metric integrity:
    failures can never improve the number)."""

    def _seed(self, home, day: date, kinds: list[str]) -> None:
        state = SupervisorState(home)
        path = state.ledger_dir / f"{day.isoformat()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.writelines(json.dumps({"ts": f"{day.isoformat()}T00:00:00Z", "kind": kind}) + "\n" for kind in kinds)

    def test_week_metric_excludes_failed_units(self, home):
        today = datetime.now(UTC).date()
        last_week_day = today - timedelta(days=7)
        # This week: 4 units (3 dispatched + 1 workunit), 2 failed, 3 escalations.
        self._seed(
            home,
            today,
            ["dispatched", "dispatched", "dispatched", "workunit_finished", "loop_failed", "worker_failed"]
            + ["packet_created"] * 3,
        )
        # Last week: 2 units, 0 failed, 1 escalation.
        self._seed(home, last_week_day, ["dispatched", "dispatched", "packet_created"])

        metric = week_metric(SupervisorState(home), today)
        this = metric["this_week"]
        assert (this["units"], this["failed"], this["healthy"], this["escalations"]) == (4, 2, 2, 3)
        assert this["per_healthy_unit"] == 1.5
        last = metric["last_week"]
        assert (last["units"], last["failed"], last["healthy"]) == (2, 0, 2)
        assert last["per_healthy_unit"] == 0.5

        line = format_week_metric(metric)
        assert "escalations/work-unit (healthy): 1.50 (4 units, 2 failed excluded) | last week: 0.50" == line

    def test_zero_healthy_units_is_na(self, home):
        today = datetime.now(UTC).date()
        self._seed(home, today, ["dispatched", "loop_failed", "packet_created"])
        metric = week_metric(SupervisorState(home), today)
        assert metric["this_week"]["per_healthy_unit"] is None
        assert format_week_metric(metric).startswith("escalations/work-unit (healthy): n/a")

    def test_ledger_summary_includes_week_line_and_json(self, home, queue_root, capsys):
        today = datetime.now(UTC).date()
        self._seed(home, today, ["dispatched", "packet_created"])
        assert main(["ledger", "--summary"]) == 0
        out = capsys.readouterr().out
        assert "escalations/work-unit (healthy): 1.00 (1 units, 0 failed excluded)" in out

        assert main(["--json", "ledger", "--summary"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["week_metric"]["this_week"]["per_healthy_unit"] == 1.0

    def test_summary_separates_failed_workers_from_unjudged(self, home, queue_root, capsys):
        state = SupervisorState(home)
        state.ledger_append("worker_finished", session="am-ok", exit_code=0, judged=False)
        state.ledger_append("worker_finished", session="am-dead", exit_code=1, judged=False)
        state.ledger_append("worker_failed", session="am-dead", exit_code=1, reason="worker exited 1")
        assert main(["ledger", "--summary"]) == 0
        out = capsys.readouterr().out
        assert "Workers failed unjudged (1):" in out and "am-dead" in out
        # am-dead is NOT flattened into the unjudged-success bucket.
        unjudged_section = out.split("Workers finished unjudged", 1)[1]
        assert "am-ok" in unjudged_section and "am-dead" not in unjudged_section


class TestAnswerCommand:
    def test_answer_happy_path(self, queue_root, capsys):
        packet = make_pending(queue_root)
        assert main(["answer", packet.id, "B", "--rationale", "safer"]) == 0
        assert "answered" in capsys.readouterr().out
        answered = PacketQueue(queue_root).get(packet.id)
        assert answered.resolution is not None
        assert answered.resolution.answer == "B"
        assert answered.resolution.rationale == "safer"

    def test_answer_json_output(self, queue_root, capsys):
        packet = make_pending(queue_root)
        assert main(["--json", "answer", packet.id, "A"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["resolution"]["answer"] == "A"

    def test_answer_bad_option_exits_1(self, queue_root, capsys):
        packet = make_pending(queue_root)
        assert main(["answer", packet.id, "Z"]) == 1
        err = capsys.readouterr().err
        assert "error:" in err and "Z" in err

    def test_answer_unknown_packet_exits_1(self, queue_root, capsys):
        assert main(["answer", "pkt-nope", "A"]) == 1
        assert "error:" in capsys.readouterr().err
