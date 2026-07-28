"""Tests for judge-gated finish lines (design §The Judge Requirement, step 4).

Covers: the judge runner (pass/fail/timeout/spawn-failure + env contract), the
supervisor's judge-gated loop:closed / loop:failed events + ledger +
notification items, judged:true/false on worker:finished, the judge verify CLI
(broken-test protocol, both directions), ledger --summary rendering, and a
real-tmux end-to-end run (fake worker + real judge command).

Most tests need no tmux: judge execution is plain subprocess, and the
supervisor's worker-observation backend is injected. The end-to-end test uses
REAL tmux and is loudly skipped when tmux is absent (same policy as
test_workers.py).
"""

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from attention_manager import judge as judge_mod
from attention_manager.cli import format_ledger_summary, main, summarize_ledger
from attention_manager.judge import run_judge, verify
from attention_manager.queue import PacketQueue
from attention_manager.state import SupervisorState
from attention_manager.supervisor import Supervisor
from attention_manager.workers import Observation

TMUX_PRESENT = shutil.which("tmux") is not None
requires_tmux = pytest.mark.skipif(
    not TMUX_PRESENT,
    reason=(
        "LOUD SKIP: tmux is NOT installed on this machine — the judge end-to-end "
        "integration test did NOT run. Install tmux to exercise it for real."
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


# -- run_judge (the runner itself) ------------------------------------------------


class TestRunJudge:
    def _run(self, tmp_path: Path, cmd: str, worker_exit: int | None = 0, timeout_s: float = 30.0):
        cwd = tmp_path / "worker"
        cwd.mkdir(exist_ok=True)
        return run_judge(
            cmd,
            cwd=cwd,
            home=tmp_path / "home",
            queue_root=tmp_path / "queue",
            worker_log=tmp_path / "worker" / "worker.log",
            worker_exit=worker_exit,
            timeout_s=timeout_s,
        )

    def test_exit_zero_passes(self, tmp_path):
        result = self._run(tmp_path, 'echo "PASS: all good"')
        assert result.passed is True
        assert result.exit_code == 0
        assert result.reason == ""
        assert "PASS: all good" in result.output

    def test_nonzero_fails_with_reason(self, tmp_path):
        result = self._run(tmp_path, 'echo "FAIL: broken"; exit 3')
        assert result.passed is False
        assert result.exit_code == 3
        assert result.reason == "judge exited 3"
        assert "FAIL: broken" in result.output

    def test_stderr_captured_with_stdout(self, tmp_path):
        result = self._run(tmp_path, 'echo out; echo "err-side reason" >&2; exit 1')
        assert "out" in result.output
        assert "err-side reason" in result.output

    def test_timeout_is_a_failure_never_a_skip(self, tmp_path):
        result = self._run(tmp_path, "sleep 5", timeout_s=0.3)
        assert result.passed is False
        assert result.exit_code is None
        assert "timed out" in result.reason

    def test_spawn_failure_is_a_failure(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise FileNotFoundError("bash not found")

        monkeypatch.setattr(judge_mod.subprocess, "run", boom)
        result = self._run(tmp_path, "true")
        assert result.passed is False
        assert result.exit_code is None
        assert "spawn failed" in result.reason

    def test_env_and_cwd_contract(self, tmp_path):
        """Judge sees ATTENTION_HOME, ATTENTION_QUEUE_DIR, WORKER_LOG, WORKER_EXIT; cwd = worker dir."""
        result = self._run(
            tmp_path,
            'echo "home=$ATTENTION_HOME queue=$ATTENTION_QUEUE_DIR log=$WORKER_LOG exit=$WORKER_EXIT pwd=$PWD"',
            worker_exit=7,
        )
        assert result.passed is True
        assert f"home={tmp_path / 'home'}" in result.output
        assert f"queue={tmp_path / 'queue'}" in result.output
        assert f"log={tmp_path / 'worker' / 'worker.log'}" in result.output
        assert "exit=7" in result.output
        assert f"pwd={tmp_path / 'worker'}" in result.output

    def test_worker_exit_none_becomes_empty_string(self, tmp_path):
        result = self._run(tmp_path, '[ -z "$WORKER_EXIT" ] && echo "empty-as-documented"', worker_exit=None)
        assert result.passed is True
        assert "empty-as-documented" in result.output

    def test_output_tail_bounded_to_400(self, tmp_path):
        result = self._run(tmp_path, "for i in $(seq 1 200); do echo line-$i; done")
        assert len(result.output_tail) <= 400
        assert "line-200" in result.output_tail  # tail keeps the END


# -- supervisor: judge-gated loop closure ------------------------------------------


def make_supervisor(home, queue, sessions=None, observations=None, **kwargs) -> Supervisor:
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


def seed_worker(home: Path, session: str, judge_cmd: str | None, log_text: str = "") -> None:
    """Create workers/<session>/{meta.json,worker.log} so adopt_workers picks it up."""
    worker_dir = home / "workers" / session
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "worker.log").write_text(log_text, encoding="utf-8")
    meta = {"name": session.removeprefix("am-"), "session": session, "cmd": "true", "judge_cmd": judge_cmd}
    (worker_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def events_of(sup: Supervisor, name: str) -> list[dict]:
    return [e for e in sup.state.read_events() if e["event"] == name]


def ledger_of(sup: Supervisor, kind: str) -> list[dict]:
    return [e for e in sup.state.ledger_read() if e["kind"] == kind]


FINISHED_OK = Observation(alive=True, exit_code=0, sentinel_seen=True, session_id=None)


class TestSupervisorJudgeGating:
    def test_judge_pass_closes_the_loop(self, home, queue, tmp_path):
        notify_file = tmp_path / "notify.jsonl"
        seed_worker(home, "am-good", 'echo "PASS: verified"')
        sup = make_supervisor(
            home,
            queue,
            sessions=["am-good"],
            observations={"am-good": FINISHED_OK},
            notify_spec=f"file:{notify_file}",
            batch_window_s=0.0,
        )
        sup.tick()

        closed = events_of(sup, "loop:closed")
        assert len(closed) == 1
        assert closed[0]["session"] == "am-good"
        assert closed[0]["worker_exit"] == 0
        assert "PASS: verified" in closed[0]["judge_output"]
        assert events_of(sup, "loop:failed") == []

        finished = events_of(sup, "worker:finished")
        assert len(finished) == 1
        assert finished[0]["judged"] is True
        assert finished[0]["judge_result"] == "closed"

        assert len(ledger_of(sup, "loop_closed")) == 1
        assert ledger_of(sup, "worker_finished")[0]["judge_result"] == "closed"

        # judge output persisted to judge.log
        judge_log = (home / "workers" / "am-good" / "judge.log").read_text(encoding="utf-8")
        assert "PASS: verified" in judge_log

        # notification item kind finish_line flowed through the batcher
        batch = json.loads(notify_file.read_text(encoding="utf-8").splitlines()[0])
        assert batch["packets"][0]["kind"] == "finish_line"
        assert batch["packets"][0]["id"] == "am-good"

    def test_judge_fail_is_loop_failed_and_loud(self, home, queue, tmp_path, capsys):
        notify_file = tmp_path / "notify.jsonl"
        seed_worker(home, "am-bad", 'echo "FAIL: marker missing"; exit 1')
        sup = make_supervisor(
            home,
            queue,
            sessions=["am-bad"],
            observations={"am-bad": FINISHED_OK},
            notify_spec=f"file:{notify_file}",
            batch_window_s=0.0,
        )
        sup.tick()

        failed = events_of(sup, "loop:failed")
        assert len(failed) == 1
        assert failed[0]["reason"] == "judge exited 1"
        assert "FAIL: marker missing" in failed[0]["judge_output"]
        assert events_of(sup, "loop:closed") == []

        finished = events_of(sup, "worker:finished")
        assert finished[0]["judged"] is True
        assert finished[0]["judge_result"] == "failed"

        assert len(ledger_of(sup, "loop_failed")) == 1
        assert "loop:failed" in capsys.readouterr().err  # loud on stderr

        batch = json.loads(notify_file.read_text(encoding="utf-8").splitlines()[0])
        assert batch["packets"][0]["kind"] == "finish_line_failed"

    def test_judge_timeout_is_loop_failed(self, home, queue):
        seed_worker(home, "am-slow", "sleep 5")
        sup = make_supervisor(
            home, queue, sessions=["am-slow"], observations={"am-slow": FINISHED_OK}, judge_timeout_s=0.3
        )
        sup.tick()
        failed = events_of(sup, "loop:failed")
        assert len(failed) == 1
        assert "timed out" in failed[0]["reason"]
        assert events_of(sup, "worker:finished")[0]["judge_result"] == "failed"

    def test_judge_spawn_failure_is_loop_failed_never_skipped(self, home, queue, monkeypatch):
        seed_worker(home, "am-nospawn", "true")

        def boom(*args, **kwargs):
            raise OSError("no bash on this box")

        monkeypatch.setattr(judge_mod.subprocess, "run", boom)
        sup = make_supervisor(home, queue, sessions=["am-nospawn"], observations={"am-nospawn": FINISHED_OK})
        sup.tick()
        failed = events_of(sup, "loop:failed")
        assert len(failed) == 1
        assert "spawn failed" in failed[0]["reason"]
        # NEVER silently skipped: worker:finished still says judged:true + failed
        assert events_of(sup, "worker:finished")[0]["judged"] is True
        assert events_of(sup, "worker:finished")[0]["judge_result"] == "failed"

    def test_no_judge_unchanged_judged_false(self, home, queue):
        seed_worker(home, "am-plain", judge_cmd=None)
        sup = make_supervisor(home, queue, sessions=["am-plain"], observations={"am-plain": FINISHED_OK})
        sup.tick()
        finished = events_of(sup, "worker:finished")
        assert len(finished) == 1
        assert finished[0]["judged"] is False
        assert "judge_result" not in finished[0]
        assert events_of(sup, "loop:closed") == []
        assert events_of(sup, "loop:failed") == []
        assert not (home / "workers" / "am-plain" / "judge.log").exists()

    def test_dead_session_with_judge_runs_with_empty_worker_exit(self, home, queue):
        seed_worker(home, "am-dead", 'echo "exit was: [$WORKER_EXIT]"; exit 1')
        dead = Observation(alive=False, exit_code=None, sentinel_seen=False, session_id=None)
        sup = make_supervisor(home, queue, sessions=["am-dead"], observations={"am-dead": dead})
        sup.tick()
        failed = events_of(sup, "loop:failed")
        assert len(failed) == 1
        assert "exit was: []" in failed[0]["judge_output"]  # WORKER_EXIT empty, documented
        assert failed[0]["worker_exit"] is None

    def test_judge_reads_worker_log_via_env(self, home, queue):
        seed_worker(
            home,
            "am-grep",
            'grep -q GOOD-MARKER "$WORKER_LOG" && echo "PASS: marker" || { echo "FAIL: none"; exit 1; }',
            log_text="noise\nGOOD-MARKER\nmore noise\n",
        )
        sup = make_supervisor(home, queue, sessions=["am-grep"], observations={"am-grep": FINISHED_OK})
        sup.tick()
        assert len(events_of(sup, "loop:closed")) == 1


# -- judge verify CLI (broken-test protocol) ----------------------------------------

GREP_JUDGE = 'grep -q GOOD "$ARTIFACT" && echo "PASS: marker found" || { echo "FAIL: marker missing"; exit 1; }'


@pytest.fixture
def artifacts(tmp_path) -> tuple[Path, Path]:
    good = tmp_path / "good.txt"
    good.write_text("all GOOD here\n", encoding="utf-8")
    broken = tmp_path / "broken.txt"
    broken.write_text("nothing to see\n", encoding="utf-8")
    return good, broken


class TestVerify:
    def test_working_judge_passes_both_directions(self, artifacts):
        good, broken = artifacts
        result = verify(GREP_JUDGE, good, broken)
        assert result.passed is True
        assert result.good.exit_code == 0
        assert result.broken.exit_code == 1

    def test_decoration_judge_fails_verify(self, artifacts):
        good, broken = artifacts
        result = verify('echo "PASS: always"', good, broken)
        assert result.passed is False
        assert result.good.ok is True
        assert result.broken.ok is False  # never fails = decoration

    def test_inverted_judge_fails_verify(self, artifacts):
        good, broken = artifacts
        result = verify('echo "FAIL: always"; exit 1', good, broken)
        assert result.passed is False
        assert result.good.ok is False

    def test_missing_artifact_fails_loud(self, tmp_path, artifacts):
        good, _ = artifacts
        with pytest.raises(ValueError, match="does not exist"):
            verify(GREP_JUDGE, good, tmp_path / "nope.txt")

    def test_timeout_on_broken_is_not_a_legit_fail(self, artifacts):
        good, broken = artifacts
        # Judge passes good instantly but hangs on broken: the broken direction
        # never actually judged, so verify must NOT count it as a proper fail.
        cmd = 'grep -q GOOD "$ARTIFACT" && echo "PASS: ok" || sleep 5'
        result = verify(cmd, good, broken, timeout_s=0.3)
        assert result.broken.exit_code is None
        assert result.broken.ok is False
        assert result.passed is False


class TestVerifyCli:
    def test_cli_pass(self, artifacts, capsys):
        good, broken = artifacts
        code = main(["judge", "verify", "--cmd", GREP_JUDGE, "--good", str(good), "--broken", str(broken)])
        out = capsys.readouterr().out
        assert code == 0
        assert "VERDICT: PASS" in out
        assert "[good]" in out and "[broken]" in out

    def test_cli_decoration_judge_fails(self, artifacts, capsys):
        good, broken = artifacts
        code = main(["judge", "verify", "--cmd", "echo PASS: always", "--good", str(good), "--broken", str(broken)])
        out = capsys.readouterr().out
        assert code == 1
        assert "VERDICT: FAIL" in out
        assert "decoration" in out

    def test_cli_json(self, artifacts, capsys):
        good, broken = artifacts
        code = main(["--json", "judge", "verify", "--cmd", GREP_JUDGE, "--good", str(good), "--broken", str(broken)])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["verdict"] == "PASS"
        assert payload["good"]["exit_code"] == 0
        assert payload["broken"]["exit_code"] == 1

    def test_cli_missing_artifact_errors(self, tmp_path, artifacts, capsys):
        good, _ = artifacts
        code = main(["judge", "verify", "--cmd", GREP_JUDGE, "--good", str(good), "--broken", str(tmp_path / "no")])
        assert code == 1
        assert "does not exist" in capsys.readouterr().err


# -- ledger --summary ---------------------------------------------------------------


def seed_ledger(home: Path) -> None:
    state = SupervisorState(home)
    state.ledger_append("dispatched", session="am-alpha", name="alpha")
    state.ledger_append("packet_created", packet_id="pkt-1", question="A or B?", tier="batch")
    state.ledger_append("packet_answered", packet_id="pkt-1", resolution={"answer": "A"}, latency_s=4.0)
    state.ledger_append("packet_created", packet_id="pkt-2", question="C or D?", tier="batch")
    state.ledger_append("packet_answered", packet_id="pkt-2", resolution={"answer": "D"}, latency_s=10.0)
    state.ledger_append("worker_finished", session="am-alpha", exit_code=0, judged=True, judge_result="closed")
    state.ledger_append("loop_closed", session="am-alpha", name="alpha", worker_exit=0, judge_output="PASS: ok")
    state.ledger_append("worker_finished", session="am-beta", exit_code=0, judged=True, judge_result="failed")
    state.ledger_append(
        "loop_failed", session="am-beta", name="beta", worker_exit=0, reason="judge exited 1", judge_output="FAIL"
    )
    state.ledger_append("worker_finished", session="am-gamma", exit_code=2, judged=False, judge_result=None)
    state.ledger_append("rule_applied", proposal_id="prop-1", section="Auto-answer rules", sentence="Prefer shims.")
    state.ledger_append("notified_batch", count=2, packet_ids=["pkt-1", "pkt-2"], sink="console")


class TestLedgerSummary:
    def test_summarize_ledger_shape(self, home):
        seed_ledger(home)
        summary = summarize_ledger(SupervisorState(home).ledger_read())
        assert [e["session"] for e in summary["loops_closed"]] == ["am-alpha"]
        assert summary["loops_failed"] == [{"session": "am-beta", "name": "beta", "reason": "judge exited 1"}]
        assert [e["session"] for e in summary["workers_finished_unjudged"]] == ["am-gamma"]
        assert summary["packets_created"] == 2
        assert summary["packets_answered"] == 2
        assert summary["answer_latency_median_s"] == 7.0
        assert summary["rules_applied"][0]["section"] == "Auto-answer rules"
        assert summary["notification_batches"] == 1

    def test_cli_summary_renders_names_and_reasons(self, home, capsys):
        seed_ledger(home)
        assert main(["ledger", "--summary"]) == 0
        out = capsys.readouterr().out
        assert "Loops closed (1):" in out and "am-alpha" in out
        assert "Loops failed (1):" in out and "am-beta" in out and "judge exited 1" in out
        assert "Workers finished unjudged (1):" in out and "am-gamma" in out
        assert "Packets: 2 created, 2 answered (median latency 7.0s)" in out
        assert "Rules applied (1):" in out and "Prefer shims." in out
        assert "Notification batches: 1" in out

    def test_cli_summary_json(self, home, capsys):
        seed_ledger(home)
        assert main(["--json", "ledger", "--summary"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["loops_closed"][0]["session"] == "am-alpha"

    def test_cli_summary_empty_day(self, home, capsys):
        assert main(["ledger", "--summary", "--date", "1999-01-01"]) == 0
        out = capsys.readouterr().out
        assert "Loops closed (0):" in out and "(none)" in out

    def test_format_handles_no_latency(self, home):
        text = format_ledger_summary(summarize_ledger([]), "2026-01-01", "/tmp/x.jsonl")
        assert "median latency" not in text


# -- dispatch --judge + real-tmux end-to-end ----------------------------------------


@requires_tmux
class TestDispatchJudgeTmux:
    def test_dispatch_stores_judge_cmd_in_meta(self, home, queue_root, monkeypatch):
        name = f"judgemeta-{uuid.uuid4().hex[:8]}"
        session = f"am-{name}"
        try:
            assert main(["dispatch", name, "--task", "t", "--worker-cmd", "true", "--judge", "echo PASS"]) == 0
            meta = json.loads((home / "workers" / session / "meta.json").read_text(encoding="utf-8"))
            assert meta["judge_cmd"] == "echo PASS"
            # adopt_workers carries it into the supervisor's record
            state = SupervisorState(home)
            state.adopt_workers([])
            assert state.workers[session]["judge_cmd"] == "echo PASS"
        finally:
            subprocess.run(["tmux", "kill-session", "-t", f"={session}"], capture_output=True)

    def test_end_to_end_fake_worker_real_judge(self, home, queue_root):
        """Real tmux worker writes an artifact; a REAL judge command greps it;
        the supervisor (real observe backend) closes the loop."""
        name = f"judgee2e-{uuid.uuid4().hex[:8]}"
        session = f"am-{name}"
        worker_dir = home / "workers" / session
        artifact = worker_dir / "artifact.txt"
        judge_cmd = 'grep -q AM-E2E-MARKER artifact.txt && echo "PASS: artifact marker" || { echo "FAIL"; exit 1; }'
        try:
            assert (
                main(
                    [
                        "dispatch",
                        name,
                        "--task",
                        "e2e",
                        "--worker-cmd",
                        f"echo AM-E2E-MARKER > {artifact}",
                        "--judge",
                        judge_cmd,
                    ]
                )
                == 0
            )
            sup = Supervisor(home=home, queue=PacketQueue(queue_root))  # REAL tmux backends
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                sup.tick()
                if any(e["event"] == "worker:finished" for e in sup.state.read_events()):
                    break
                time.sleep(0.5)
            finished = [e for e in sup.state.read_events() if e["event"] == "worker:finished"]
            assert len(finished) == 1, "worker never finished within budget"
            assert finished[0]["judged"] is True
            assert finished[0]["judge_result"] == "closed"
            closed = [e for e in sup.state.read_events() if e["event"] == "loop:closed"]
            assert len(closed) == 1 and closed[0]["session"] == session
            assert "PASS: artifact marker" in (worker_dir / "judge.log").read_text(encoding="utf-8")
        finally:
            subprocess.run(["tmux", "kill-session", "-t", f"={session}"], capture_output=True)
