"""Triage runner unit tests — NO LLM: a FAKE amplifier binary (stub script)
parses the machine-greppable prompt header (PHASE/PACKET_ID/OUTPUT_PATH) and
writes canned verdict files, driven by the FAKE_TRIAGE_MODE / FAKE_DELTA_MODE
environment variables. This exercises the whole verdict protocol: subprocess
invocation, verdict-file read + strict validation, retry-once discipline,
packet mutation atomicity, events, ledger, and idempotency.
"""

import json
import stat
from pathlib import Path

import pytest

from attention_manager.packet import Option, Packet, Recommendation, Source
from attention_manager.queue import PacketQueue
from attention_manager.rulebook import Rulebook
from attention_manager.state import SupervisorState
from attention_manager.triage import TriageRunner, build_rule_delta_prompt, build_triage_prompt

# The fake amplifier CLI. Accepts `run -B <bundle> <prompt>`, greps the prompt
# header, writes a canned verdict per FAKE_TRIAGE_MODE / FAKE_DELTA_MODE.
FAKE_AMPLIFIER = r"""#!/usr/bin/env python3
import json, os, re, sys

prompt = sys.argv[-1]
phase = re.search(r"^PHASE: (\S+)", prompt, re.M).group(1)
packet_id = re.search(r"^PACKET_ID: (\S+)", prompt, re.M).group(1)
out = re.search(r"^OUTPUT_PATH: (.+)$", prompt, re.M).group(1).strip()

def write(data):
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f)

if phase == "triage":
    mode = os.environ.get("FAKE_TRIAGE_MODE", "recommend")
    if mode == "fail-then-recommend":
        counter = os.environ["FAKE_COUNTER"]
        n = int(open(counter).read()) if os.path.exists(counter) else 0
        open(counter, "w").write(str(n + 1))
        mode = "missing" if n == 0 else "recommend"
    if mode == "recommend":
        write({"packet_id": packet_id, "decision": "recommend",
               "recommendation": {"option": "B", "rationale": "shim is safer", "confidence": "medium"},
               "why": "rulebook prefers shims", "rule_refs": ["Auto-answer rules: prefer shims"]})
    elif mode == "bounce":
        write({"packet_id": packet_id, "decision": "bounce", "recommendation": None,
               "why": "cold-reader test failed", "rule_refs": [],
               "bounce_reason": "options lack consequences"})
    elif mode == "invalid-json":
        with open(out, "w", encoding="utf-8") as f:
            f.write("{not json")
    elif mode == "wrong-option":
        write({"packet_id": packet_id, "decision": "recommend",
               "recommendation": {"option": "Z", "rationale": "made up", "confidence": "high"},
               "why": "fabricated option", "rule_refs": []})
    elif mode == "missing":
        pass  # write nothing — the session "forgot" the verdict
    elif mode == "verdict-then-fail":
        # Valid verdict but nonzero exit — the runner MUST treat this as a
        # failed attempt (success requires exit 0 AND valid verdict; the
        # DTU-found bug class is exactly "the exit code lies").
        write({"packet_id": packet_id, "decision": "recommend",
               "recommendation": {"option": "B", "rationale": "looks fine", "confidence": "high"},
               "why": "verdict written but session errored after", "rule_refs": []})
        sys.exit(1)
    else:
        raise SystemExit(f"unknown FAKE_TRIAGE_MODE {mode}")
else:  # rule_delta
    mode = os.environ.get("FAKE_DELTA_MODE", "propose")
    if mode == "propose":
        write({"packet_id": packet_id, "none": False, "section": "Auto-answer rules",
               "sentence": "Prefer compat shims when downstream owners are unavailable.",
               "reason": "same class recurs"})
    elif mode == "none":
        write({"packet_id": packet_id, "none": True, "reason": "genuinely one-off"})
    elif mode == "missing":
        pass
    else:
        raise SystemExit(f"unknown FAKE_DELTA_MODE {mode}")
"""


@pytest.fixture
def fake_amplifier(tmp_path) -> Path:
    stub = tmp_path / "bin" / "fake-amplifier"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(FAKE_AMPLIFIER, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("ATTENTION_HOME", str(path))
    return path


@pytest.fixture
def runner(home, queue_root, fake_amplifier) -> TriageRunner:
    return TriageRunner(
        home=home,
        queue=PacketQueue(queue_root),
        amplifier_bin=str(fake_amplifier),
        bundle_uri="test://triage-bundle",
        timeout_s=30,
    )


def make_packet(recommendation: Recommendation | None = None) -> Packet:
    return Packet(
        question="Migrate the config parser now, or keep the compat shim?",
        options=[
            Option(id="A", label="Migrate now", consequence="breaks two downstream repos"),
            Option(id="B", label="Keep shim", consequence="carries buggy path ~2 weeks"),
        ],
        source=Source(kind="decision", muxplex_session="am-test"),
        recommendation=recommendation,
        context="Downstream owners are unavailable this week.",
    )


def events_named(home: Path, name: str) -> list[dict]:
    return [e for e in SupervisorState(home).read_events() if e["event"] == name]


class TestPromptConstruction:
    def test_triage_prompt_has_header_rulebook_packet(self, home):
        packet = make_packet()
        rulebook_content, _ = Rulebook(home=home).read()
        prompt = build_triage_prompt(packet, rulebook_content, Path("/tmp/out.json"))
        assert "PHASE: triage\n" in prompt
        assert f"PACKET_ID: {packet.id}\n" in prompt
        assert "OUTPUT_PATH: /tmp/out.json\n" in prompt
        assert "## Attention priorities" in prompt  # rulebook is inline
        assert packet.question in prompt  # packet JSON is inline

    def test_rule_delta_prompt_has_header_and_resolution(self, home, queue_root):
        queue = PacketQueue(queue_root)
        packet = make_packet()
        queue.write(packet)
        answered = queue.answer(packet.id, "B", answered_by="human")
        rulebook_content, _ = Rulebook(home=home).read()
        prompt = build_rule_delta_prompt(answered, rulebook_content, Path("/tmp/rd.json"))
        assert "PHASE: rule_delta\n" in prompt
        assert f"PACKET_ID: {packet.id}\n" in prompt
        assert "OUTPUT_PATH: /tmp/rd.json\n" in prompt
        assert '"answered_by": "human"' in prompt  # resolution present


class TestRecommendPath:
    def test_fills_triage_fields_in_place_atomically(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "recommend")
        packet = make_packet()
        runner.queue.write(packet)

        outcomes = runner.triage_pass()
        assert [o.outcome for o in outcomes] == ["recommended"]

        # Packet is STILL pending (recommend-only Phase 1), triage fields filled.
        subdir, path = runner.queue.locate(packet.id)
        assert subdir == "pending"
        assert not path.with_suffix(".json.tmp").exists()  # atomic write left no tmp
        updated = runner.queue.get(packet.id)
        assert updated.triage is not None
        assert updated.triage.handled_by == "manager-recommend"
        assert updated.triage.rule_refs == ["Auto-answer rules: prefer shims"]
        assert updated.triage.why.startswith("recommend B (medium):")
        # Packet had no producer recommendation -> triage recommendation lands there.
        assert updated.recommendation is not None
        assert updated.recommendation.option == "B"

        assert len(events_named(home, "triage:recommended")) == 1
        ledger = SupervisorState(home).ledger_read()
        assert any(e["kind"] == "triage_recommended" and e["packet_id"] == packet.id for e in ledger)

    def test_producer_recommendation_is_kept(self, runner, monkeypatch):
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "recommend")
        packet = make_packet(recommendation=Recommendation(option="A", rationale="producer says migrate"))
        runner.queue.write(packet)
        runner.triage_pass()
        updated = runner.queue.get(packet.id)
        assert updated.recommendation is not None
        assert updated.recommendation.option == "A"  # producer's kept
        assert updated.recommendation.rationale == "producer says migrate"
        assert updated.triage is not None and "recommend B" in updated.triage.why  # triage's in triage fields

    def test_idempotent_second_pass_does_nothing(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "recommend")
        packet = make_packet()
        runner.queue.write(packet)
        assert len(runner.triage_pass()) == 1
        assert len(runner.triage_pass()) == 0  # already triaged — skipped
        assert len(events_named(home, "triage:recommended")) == 1


class TestBouncePath:
    def test_moves_to_bounced_with_reason_merged(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "bounce")
        packet = make_packet()
        runner.queue.write(packet)

        outcomes = runner.triage_pass()
        assert [o.outcome for o in outcomes] == ["bounced"]

        subdir, _ = runner.queue.locate(packet.id)
        assert subdir == "bounced"
        assert not runner.queue.path_for(packet.id, "pending").exists()
        bounced = runner.queue.get(packet.id)
        assert bounced.triage is not None
        assert "bounce: options lack consequences" in bounced.triage.why
        assert len(events_named(home, "triage:bounced")) == 1
        assert events_named(home, "triage:bounced")[0]["bounce_reason"] == "options lack consequences"


class TestFailurePaths:
    @pytest.mark.parametrize("mode", ["missing", "invalid-json", "wrong-option"])
    def test_invalid_verdict_errors_loud_packet_untouched(self, runner, home, monkeypatch, mode):
        monkeypatch.setenv("FAKE_TRIAGE_MODE", mode)
        packet = make_packet()
        runner.queue.write(packet)

        outcomes = runner.triage_pass()
        assert [o.outcome for o in outcomes] == ["error"]

        # Packet untouched: still pending, no triage fields, no fabricated verdict.
        subdir, _ = runner.queue.locate(packet.id)
        assert subdir == "pending"
        assert runner.queue.get(packet.id).triage is None
        # One retry max: exactly TWO triage:error events, both explicit.
        errors = events_named(home, "triage:error")
        assert len(errors) == 2
        assert [e["attempt"] for e in errors] == [1, 2]
        assert [e["retrying"] for e in errors] == [True, False]

    def test_nonzero_exit_with_valid_verdict_is_still_a_failure(self, runner, home, monkeypatch):
        """Success requires exit 0 AND a valid verdict. A session that writes a
        perfectly valid verdict but exits nonzero is a failed attempt — the
        verdict file must never be read past a lying exit code (the inverse of
        the DTU bug, where a successful turn carried exit 1)."""
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "verdict-then-fail")
        packet = make_packet()
        runner.queue.write(packet)

        outcomes = runner.triage_pass()
        assert [o.outcome for o in outcomes] == ["error"]
        assert runner.queue.get(packet.id).triage is None  # valid-looking verdict never used
        errors = events_named(home, "triage:error")
        assert len(errors) == 2  # one retry max, both logged
        assert all("exited 1" in e["error"] for e in errors)

    def test_retry_succeeds_on_second_attempt(self, runner, home, monkeypatch, tmp_path):
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "fail-then-recommend")
        monkeypatch.setenv("FAKE_COUNTER", str(tmp_path / "counter"))
        packet = make_packet()
        runner.queue.write(packet)

        outcomes = runner.triage_pass()
        assert [o.outcome for o in outcomes] == ["recommended"]
        errors = events_named(home, "triage:error")
        assert len(errors) == 1
        assert errors[0]["attempt"] == 1 and errors[0]["retrying"] is True
        assert len(events_named(home, "triage:recommended")) == 1

    def test_missing_binary_preflight_fails_loud(self, home, queue_root):
        runner = TriageRunner(home=home, queue=PacketQueue(queue_root), amplifier_bin="no-such-amplifier-bin")
        with pytest.raises(RuntimeError, match="not found on PATH"):
            runner.preflight()


class TestRuleDeltaPhase:
    def _answered_triaged_packet(self, runner, monkeypatch, answer: str = "B") -> Packet:
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "recommend")
        packet = make_packet()
        runner.queue.write(packet)
        runner.triage_pass()  # fills triage fields (recommend B)
        return runner.queue.answer(packet.id, answer, answered_by="human")

    def test_proposes_exactly_once(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_DELTA_MODE", "propose")
        packet = self._answered_triaged_packet(runner, monkeypatch)

        outcomes = runner.triage_pass()
        assert [(o.phase, o.outcome) for o in outcomes] == [("rule_delta", "proposed")]

        proposals = runner.rulebook.list_proposals()
        assert len(proposals) == 1
        assert proposals[0]["packet_id"] == packet.id
        assert proposals[0]["section"] == "Auto-answer rules"
        assert proposals[0]["status"] == "proposed"
        assert len(events_named(home, "rule_delta:proposed")) == 1

        # Idempotency across passes: never double-propose.
        assert runner.triage_pass() == []
        assert len(runner.rulebook.list_proposals()) == 1

    def test_recommendation_matched_recorded_not_acted_on(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_DELTA_MODE", "propose")
        self._answered_triaged_packet(runner, monkeypatch, answer="A")  # human OVERRODE the B recommendation
        runner.triage_pass()
        ledger = SupervisorState(home).ledger_read()
        entry = next(e for e in ledger if e["kind"] == "rule_delta_proposed")
        assert entry["recommendation_matched"] is False  # data recorded; no promotion/demotion machinery

    def test_none_outcome_logged_and_idempotent(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_DELTA_MODE", "none")
        packet = self._answered_triaged_packet(runner, monkeypatch)
        outcomes = runner.triage_pass()
        assert [(o.phase, o.outcome) for o in outcomes] == [("rule_delta", "none")]
        records = runner.rulebook.list_proposals()
        assert records[0]["status"] == "none" and records[0]["packet_id"] == packet.id
        assert len(events_named(home, "rule_delta:none")) == 1
        assert runner.triage_pass() == []  # never re-proposed

    def test_answered_packet_without_triage_is_skipped(self, runner, monkeypatch):
        monkeypatch.setenv("FAKE_DELTA_MODE", "propose")
        packet = make_packet()
        runner.queue.write(packet)
        runner.queue.answer(packet.id, "B", answered_by="human")  # answered BEFORE any triage
        assert runner.triage_pass() == []
        assert runner.rulebook.list_proposals() == []

    def test_delta_failure_errors_loud_no_record(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_DELTA_MODE", "missing")
        self._answered_triaged_packet(runner, monkeypatch)
        outcomes = runner.triage_pass()
        assert [(o.phase, o.outcome) for o in outcomes] == [("rule_delta", "error")]
        assert runner.rulebook.list_proposals() == []  # no record — will retry next pass, loudly
        assert len(events_named(home, "rule_delta:error")) == 2  # one retry max, both logged


class TestSessionInvocation:
    def test_session_log_captured_and_cwd_is_workdir(self, runner, monkeypatch):
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "recommend")
        packet = make_packet()
        runner.queue.write(packet)
        runner.triage_pass()
        work_dir = runner.state.home / "triage" / packet.id
        assert (work_dir / "verdict.json").exists()
        assert (work_dir / "session-triage-1.log").exists()

    def test_stale_verdict_removed_before_attempt(self, runner, monkeypatch):
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "missing")
        packet = make_packet()
        runner.queue.write(packet)
        # Pre-plant a stale (valid-looking) verdict; the runner must NOT read it.
        work_dir = runner.state.home / "triage" / packet.id
        work_dir.mkdir(parents=True)
        (work_dir / "verdict.json").write_text(
            json.dumps(
                {
                    "packet_id": packet.id,
                    "decision": "recommend",
                    "recommendation": {"option": "A", "rationale": "stale", "confidence": "high"},
                    "why": "stale verdict",
                    "rule_refs": [],
                }
            ),
            encoding="utf-8",
        )
        outcomes = runner.triage_pass()
        assert [o.outcome for o in outcomes] == ["error"]  # stale file never misread
        assert runner.queue.get(packet.id).triage is None


class TestCliTriage:
    def test_triage_once_via_cli(self, home, queue_root, fake_amplifier, monkeypatch, capsys):
        from attention_manager.cli import main

        monkeypatch.setenv("FAKE_TRIAGE_MODE", "recommend")
        monkeypatch.setenv("ATTENTION_AMPLIFIER_BIN", str(fake_amplifier))
        monkeypatch.setenv("ATTENTION_TRIAGE_BUNDLE", "test://triage-bundle")
        packet = make_packet()
        PacketQueue(queue_root).write(packet)

        assert main(["triage", "--once"]) == 0
        out = capsys.readouterr().out
        assert "recommended" in out

    def test_triage_requires_once_flag(self, capsys):
        from attention_manager.cli import main

        with pytest.raises(SystemExit):
            main(["triage"])

    def test_rulebook_cli_show_apply_reject(self, home, monkeypatch, capsys):
        from attention_manager.cli import main

        assert main(["rulebook", "show"]) == 0
        assert "## Attention priorities" in capsys.readouterr().out

        rulebook = Rulebook(home=home)
        keep = rulebook.append_proposal("pkt-1", "Edge cases", "keep this rule", "why")
        drop = rulebook.append_proposal("pkt-2", "Edge cases", "drop this rule", "why")

        assert main(["rulebook", "proposals"]) == 0
        out = capsys.readouterr().out
        assert keep["id"] in out and drop["id"] in out

        assert main(["rulebook", "apply", keep["id"]]) == 0
        capsys.readouterr()
        content, _ = rulebook.read()
        assert "- keep this rule" in content

        assert main(["rulebook", "reject", drop["id"], "--reason", "too specific"]) == 0
        assert rulebook.get_proposal(drop["id"])["status"] == "rejected"

        # Ledger recorded both decisions (calibration trail).
        kinds = [e["kind"] for e in SupervisorState(home).ledger_read()]
        assert "rule_applied" in kinds and "rule_rejected" in kinds

    def test_rulebook_reject_requires_reason_flag(self):
        from attention_manager.cli import main

        with pytest.raises(SystemExit):
            main(["rulebook", "reject", "rp-x"])


def _no_observation(session, log_path):
    from attention_manager.workers import Observation

    return Observation(alive=True, exit_code=None, sentinel_seen=False, session_id=None)


class TestSupervisorTriageWiring:
    def test_supervise_runs_triage_every_n_ticks(self, home, queue_root, fake_amplifier, monkeypatch):
        from attention_manager.supervisor import Supervisor

        monkeypatch.setenv("FAKE_TRIAGE_MODE", "recommend")
        queue = PacketQueue(queue_root)
        packet = make_packet()
        queue.write(packet)

        runner = TriageRunner(
            home=home, queue=queue, amplifier_bin=str(fake_amplifier), bundle_uri="test://triage-bundle", timeout_s=30
        )
        supervisor = Supervisor(
            home=home,
            queue=queue,
            triage_every=2,
            triage_runner=runner,
            list_sessions=list,
            observe=_no_observation,
        )

        supervisor.tick()  # tick 0 -> triage runs (0 % 2 == 0)
        assert queue.get(packet.id).triage is not None

    def test_triage_off_by_default(self, home, queue_root):
        from attention_manager.supervisor import Supervisor

        supervisor = Supervisor(home=home, queue=PacketQueue(queue_root))
        assert supervisor.triage_every is None
        assert supervisor.triage_runner is None

    def test_triage_crash_does_not_kill_tick(self, home, queue_root, fake_amplifier):
        from attention_manager.supervisor import Supervisor

        class Boom(TriageRunner):
            def triage_pass(self):
                raise OSError("disk fell off")

        boom = Boom(home=home, queue=PacketQueue(queue_root), amplifier_bin=str(fake_amplifier))
        supervisor = Supervisor(
            home=home,
            queue=PacketQueue(queue_root),
            triage_every=1,
            triage_runner=boom,
            list_sessions=list,
            observe=_no_observation,
        )
        supervisor.tick()  # must not raise
        errors = [e for e in supervisor.state.read_events() if e["event"] == "triage:error"]
        assert errors and "disk fell off" in errors[0]["error"]


class TestSessionIsolation:
    """Defect (host): user-level bundle.app composition broke verdict schema
    compliance. The runner plants project-scope settings in the session cwd
    (the per-packet work dir) that replace bundle.app with [] and disable
    notifications — providers are NOT overridden, so they merge from global."""

    def test_work_dir_writes_isolation_settings(self, runner):
        packet = make_packet()
        work_dir = runner._work_dir(packet.id)
        settings = work_dir / ".amplifier" / "settings.yaml"
        assert settings.exists()
        content = settings.read_text(encoding="utf-8")
        assert "app: []" in content  # user-level app bundles neutralized
        assert "enabled: false" in content  # notifications off for programmatic sessions
        assert "providers" not in content  # providers must merge through from global

    def test_isolation_settings_present_after_real_pass(self, runner, monkeypatch):
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "recommend")
        packet = make_packet()
        runner.queue.write(packet)
        runner.triage_pass()
        settings = runner.state.home / "triage" / packet.id / ".amplifier" / "settings.yaml"
        assert settings.exists()


class TestSchemaRestatement:
    """Defect (host): the triage LLM invented a verdict schema when composed
    app bundles buried the contract. Both prompts now END with an exact
    schema restatement + filled example + named observed failure."""

    def test_triage_prompt_ends_with_schema_restatement(self, home):
        packet = make_packet()
        rulebook_content, _ = Rulebook(home=home).read()
        prompt = build_triage_prompt(packet, rulebook_content, Path("/tmp/out.json"))
        idx = prompt.index("VERDICT SCHEMA — RESTATED")
        assert idx > prompt.index(packet.question)  # restatement comes AFTER the packet
        tail = prompt[idx:]
        assert '"decision": "recommend"' in tail  # filled example present
        assert '{"verdict": "escalate"' in tail  # observed failure named
        assert "hard failure" in tail
        assert "IGNORE" in tail  # composed foreign instructions explicitly overridden

    def test_rule_delta_prompt_ends_with_schema_restatement(self, home, queue_root):
        queue = PacketQueue(queue_root)
        packet = make_packet()
        queue.write(packet)
        answered = queue.answer(packet.id, "B", answered_by="human")
        rulebook_content, _ = Rulebook(home=home).read()
        prompt = build_rule_delta_prompt(answered, rulebook_content, Path("/tmp/rd.json"))
        idx = prompt.index("VERDICT SCHEMA — RESTATED")
        assert idx > prompt.index('"answered_by": "human"')
        tail = prompt[idx:]
        assert '"none": false' in tail  # filled example present
        assert "hard failure" in tail.lower()


class TestCrossPassAbandon:
    """Defect: the one-retry cap was per PASS — a consistently-failing packet
    re-cost 2 LLM sessions every pass forever. Now 3 failed passes abandon
    the packet LOUDLY ONCE and skip it until 'triage --retry'."""

    def _failing_packet(self, runner, monkeypatch) -> Packet:
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "missing")
        packet = make_packet()
        runner.queue.write(packet)
        return packet

    def test_three_failed_passes_abandon_loudly_once(self, runner, home, monkeypatch):
        packet = self._failing_packet(runner, monkeypatch)

        for expected_count in (1, 2):
            outcomes = runner.triage_pass()
            assert [(o.phase, o.outcome) for o in outcomes] == [("triage", "error")]
            marker = json.loads(
                (runner.state.home / "triage" / packet.id / "failures-triage.json").read_text(encoding="utf-8")
            )
            assert marker["count"] == expected_count
            assert marker["abandoned"] is False
        assert events_named(home, "triage:abandoned") == []

        outcomes = runner.triage_pass()  # third failed pass -> abandon
        assert len(outcomes) == 1 and "ABANDONED" in outcomes[0].detail
        abandoned = events_named(home, "triage:abandoned")
        assert len(abandoned) == 1
        assert abandoned[0]["packet_id"] == packet.id
        assert abandoned[0]["failures"] == 3
        ledger = [e for e in runner.state.ledger_read() if e["kind"] == "triage_abandoned"]
        assert len(ledger) == 1 and ledger[0]["packet_id"] == packet.id

        # Fourth pass: SKIPPED — no outcome, no new sessions, no second event.
        error_events_before = len(events_named(home, "triage:error"))
        assert runner.triage_pass() == []
        assert len(events_named(home, "triage:error")) == error_events_before
        assert len(events_named(home, "triage:abandoned")) == 1

        # The packet is untouched in pending/ — the human answers it normally.
        assert runner.queue.get(packet.id).triage is None

    def test_success_clears_failure_marker(self, runner, monkeypatch):
        packet = self._failing_packet(runner, monkeypatch)
        runner.triage_pass()  # one failed pass -> count 1
        marker = runner.state.home / "triage" / packet.id / "failures-triage.json"
        assert marker.exists()

        monkeypatch.setenv("FAKE_TRIAGE_MODE", "recommend")
        outcomes = runner.triage_pass()
        assert [(o.phase, o.outcome) for o in outcomes] == [("triage", "recommended")]
        assert not marker.exists()  # intermittent failures never accumulate to abandon

    def test_corrupt_marker_resets_loudly(self, runner, home, monkeypatch):
        packet = self._failing_packet(runner, monkeypatch)
        work_dir = runner._work_dir(packet.id)
        (work_dir / "failures-triage.json").write_text("{not json", encoding="utf-8")
        assert runner._is_abandoned(packet.id, "triage") is False
        errors = events_named(home, "triage:error")
        assert errors and "corrupt failure marker" in errors[-1]["error"]

    def test_rule_delta_abandon_path(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "recommend")
        monkeypatch.setenv("FAKE_DELTA_MODE", "missing")
        packet = make_packet()
        runner.queue.write(packet)
        runner.triage_pass()  # recommend
        runner.queue.answer(packet.id, "B", answered_by="human")

        for _ in range(3):
            runner.triage_pass()  # rule_delta fails each pass
        abandoned = events_named(home, "rule_delta:abandoned")
        assert len(abandoned) == 1 and abandoned[0]["failures"] == 3
        assert runner.triage_pass() == []  # skipped from now on

    def test_cli_retry_clears_markers_and_reenables(self, runner, home, queue_root, fake_amplifier, monkeypatch):
        from attention_manager.cli import main

        monkeypatch.setenv("ATTENTION_AMPLIFIER_BIN", str(fake_amplifier))
        monkeypatch.setenv("ATTENTION_TRIAGE_BUNDLE", "test://triage-bundle")
        packet = self._failing_packet(runner, monkeypatch)
        for _ in range(3):
            runner.triage_pass()
        assert runner.triage_pass() == []  # abandoned

        assert main(["triage", "--retry", packet.id]) == 0
        assert not (runner.state.home / "triage" / packet.id / "failures-triage.json").exists()
        monkeypatch.setenv("FAKE_TRIAGE_MODE", "recommend")
        outcomes = runner.triage_pass()  # re-attempted and now succeeds
        assert [(o.phase, o.outcome) for o in outcomes] == [("triage", "recommended")]

    def test_cli_retry_unknown_packet_fails(self, home, capsys):
        from attention_manager.cli import main

        assert main(["triage", "--retry", "pkt-never-existed"]) == 1
        assert "no failure markers" in capsys.readouterr().err
