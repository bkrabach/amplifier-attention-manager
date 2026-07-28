"""Graduated trust (Phase 2) tests — NO LLM: a FAKE amplifier binary writes
canned verdicts (same protocol as test_triage.py), driven by env vars.

Covers: rulebook section-state annotations (parse/write round-trip),
rule_ref → section resolution (incl. the unresolvable-skip path), streak /
promote / demote mechanics through the rule_delta phase, the auto-answer
happy path + every conservative bound rejecting auto, the auto review log,
and the `auto confirm` / `auto reject` CLI (incl. demotion).
"""

import json
import stat
from pathlib import Path

import pytest

from attention_manager import trust
from attention_manager.autolog import AutoLog
from attention_manager.cli import main
from attention_manager.packet import Option, Packet, Source
from attention_manager.queue import PacketQueue
from attention_manager.rulebook import Rulebook, format_section_heading, parse_section_heading, resolve_ref
from attention_manager.state import SupervisorState
from attention_manager.triage import TriageRunner

# Fake amplifier: writes a recommend verdict with env-configurable option,
# confidence, and rule_refs; rule_delta always records an explicit 'none'.
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
    write({"packet_id": packet_id, "decision": "recommend",
           "recommendation": {"option": os.environ.get("FAKE_OPTION", "A"),
                              "rationale": "rule covers this",
                              "confidence": os.environ.get("FAKE_CONFIDENCE", "high")},
           "why": "covered by the cited rules",
           "rule_refs": json.loads(os.environ.get("FAKE_RULE_REFS", '["Auto-answer rules"]'))})
else:  # rule_delta
    write({"packet_id": packet_id, "none": True, "reason": "rule already exists"})
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


def make_packet(tier: str = "batch") -> Packet:
    from attention_manager.packet import Urgency

    return Packet(
        question="Proceed with plan A or plan B?",
        options=[Option(id="A", label="Plan A"), Option(id="B", label="Plan B")],
        source=Source(kind="decision"),
        context="All facts needed to decide are right here.",
        urgency=Urgency(tier=tier),
    )


def events_named(home: Path, name: str) -> list[dict]:
    return [e for e in SupervisorState(home).read_events() if e["event"] == name]


# -- section-state annotations (Part A1) ----------------------------------------


class TestSectionState:
    def test_defaults_are_phase_1_streak_0(self, home):
        rulebook = Rulebook(home=home)
        assert rulebook.get_section_state("Auto-answer rules") == (1, 0)

    def test_set_get_round_trip_and_visible_in_file(self, home):
        rulebook = Rulebook(home=home)
        rulebook.set_section_state("Auto-answer rules", 2, 5)
        assert rulebook.get_section_state("Auto-answer rules") == (2, 5)
        content, _ = rulebook.read()
        assert "## Auto-answer rules <!-- phase:2 streak:5 -->" in content
        # Other sections untouched.
        assert rulebook.get_section_state("Edge cases") == (1, 0)

    def test_heading_parse_format_round_trip(self):
        line = format_section_heading("Auto-answer rules", 2, 3)
        assert parse_section_heading(line) == ("Auto-answer rules", 2, 3)
        assert parse_section_heading("## Edge cases") == ("Edge cases", 1, 0)
        assert parse_section_heading("not a heading") is None

    def test_append_rule_tolerates_annotated_heading(self, home):
        rulebook = Rulebook(home=home)
        rulebook.set_section_state("Auto-answer rules", 2, 4)
        rulebook.append_rule("Auto-answer rules", "Prefer plan A for smoke-class decisions.")
        content, _ = rulebook.read()
        section = content.split("## Auto-answer rules", 1)[1].split("## ", 1)[0]
        assert "- Prefer plan A for smoke-class decisions." in section
        assert rulebook.get_section_state("Auto-answer rules") == (2, 4)  # annotation preserved

    def test_unknown_section_and_bad_values_raise(self, home):
        rulebook = Rulebook(home=home)
        with pytest.raises(ValueError, match="unknown rulebook section"):
            rulebook.get_section_state("Nope")
        with pytest.raises(ValueError, match="unknown rulebook section"):
            rulebook.set_section_state("Nope", 1, 0)
        with pytest.raises(ValueError, match="phase"):
            rulebook.set_section_state("Edge cases", 9, 0)
        with pytest.raises(ValueError, match="streak"):
            rulebook.set_section_state("Edge cases", 1, -1)


# -- rule_ref → section resolution ----------------------------------------------


class TestResolveRef:
    def test_exact_section_name(self, home):
        content, _ = Rulebook(home=home).read()
        assert resolve_ref(content, "Auto-answer rules") == "Auto-answer rules"
        assert resolve_ref(content, "auto-answer rules") == "Auto-answer rules"  # case-insensitive

    def test_section_prefix_with_boundary(self, home):
        content, _ = Rulebook(home=home).read()
        assert resolve_ref(content, "Auto-answer rules: prefer shims") == "Auto-answer rules"
        assert resolve_ref(content, "Edge cases §2") == "Edge cases"

    def test_body_snippet_resolves_to_containing_section(self, home):
        rulebook = Rulebook(home=home)
        rulebook.append_rule("Escalation thresholds", "Escalate when two workers disagree.")
        content, _ = rulebook.read()
        assert resolve_ref(content, "Escalate when two workers disagree.") == "Escalation thresholds"

    def test_ambiguous_snippet_is_unresolvable(self, home):
        rulebook = Rulebook(home=home)
        rulebook.append_rule("Edge cases", "the same weird sentence")
        rulebook.append_rule("Escalation thresholds", "the same weird sentence")
        content, _ = rulebook.read()
        assert resolve_ref(content, "the same weird sentence") is None  # 2 matches — never guess

    def test_unknown_and_empty_refs_are_unresolvable(self, home):
        content, _ = Rulebook(home=home).read()
        assert resolve_ref(content, "rulebook §3.2") is None
        assert resolve_ref(content, "") is None

    def test_sections_for_refs_dedupes_and_logs_unresolved(self, home):
        rulebook = Rulebook(home=home)
        state = SupervisorState(home)
        sections = trust.sections_for_refs(
            rulebook,
            state,
            "pkt-test",
            ["Auto-answer rules", "Auto-answer rules: prefer shims", "no such rule anywhere"],
        )
        assert sections == ["Auto-answer rules"]  # deduped
        unresolved = events_named(home, "trust:ref_unresolved")
        assert len(unresolved) == 1
        assert unresolved[0]["rule_ref"] == "no such rule anywhere"


# -- streak / promote / demote mechanics ------------------------------------------


class TestTrustMechanics:
    def test_match_increments_and_promotes_at_5(self, home):
        rulebook = Rulebook(home=home)
        state = SupervisorState(home)
        for i in range(1, 5):
            outcomes = trust.record_match(rulebook, state, f"pkt-{i}", ["Auto-answer rules"], source="test")
            assert outcomes == [{"section": "Auto-answer rules", "phase": 1, "streak": i, "promoted": False}]
        outcomes = trust.record_match(rulebook, state, "pkt-5", ["Auto-answer rules"], source="test")
        assert outcomes == [{"section": "Auto-answer rules", "phase": 2, "streak": 5, "promoted": True}]
        assert rulebook.get_section_state("Auto-answer rules") == (2, 5)
        assert len(events_named(home, "trust:promoted")) == 1
        ledger = SupervisorState(home).ledger_read()
        assert any(e["kind"] == "trust_promoted" and e["section"] == "Auto-answer rules" for e in ledger)

    def test_phase_2_section_keeps_counting_without_repromotion(self, home):
        rulebook = Rulebook(home=home)
        state = SupervisorState(home)
        rulebook.set_section_state("Auto-answer rules", 2, 7)
        outcomes = trust.record_match(rulebook, state, "pkt-x", ["Auto-answer rules"], source="test")
        assert outcomes[0]["streak"] == 8 and outcomes[0]["promoted"] is False
        assert events_named(home, "trust:promoted") == []

    def test_override_demotes_to_phase_1_streak_0_loudly(self, home, capsys):
        import io

        rulebook = Rulebook(home=home)
        state = SupervisorState(home)
        rulebook.set_section_state("Auto-answer rules", 2, 9)
        err = io.StringIO()
        trust.record_override(rulebook, state, "pkt-x", ["Auto-answer rules"], source="test", err=err)
        assert rulebook.get_section_state("Auto-answer rules") == (1, 0)
        demoted = events_named(home, "trust:demoted")
        assert len(demoted) == 1 and demoted[0]["from_phase"] == 2
        assert "TRUST DEMOTED" in err.getvalue()
        ledger = SupervisorState(home).ledger_read()
        assert any(e["kind"] == "trust_demoted" for e in ledger)


# -- streak updates through the rule_delta phase (Part A2 integration) -------------


class TestRuleDeltaTrustIntegration:
    def _triage_and_answer(self, runner, option: str, answered_by: str = "human") -> Packet:
        packet = make_packet()
        runner.queue.write(packet)
        outcomes = runner.triage_pass()
        assert [o.outcome for o in outcomes] == ["recommended"]
        runner.queue.answer(packet.id, option, answered_by=answered_by)
        runner.triage_pass()  # rule_delta phase — trust update happens here
        return packet

    def test_matched_human_answer_bumps_streak(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_OPTION", "A")
        self._triage_and_answer(runner, "A")
        assert runner.rulebook.get_section_state("Auto-answer rules") == (1, 1)

    def test_five_consecutive_matches_promote(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_OPTION", "A")
        for _ in range(5):
            self._triage_and_answer(runner, "A")
        assert runner.rulebook.get_section_state("Auto-answer rules") == (2, 5)
        assert len(events_named(home, "trust:promoted")) == 1

    def test_human_override_demotes_immediately(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_OPTION", "A")
        for _ in range(3):
            self._triage_and_answer(runner, "A")
        assert runner.rulebook.get_section_state("Auto-answer rules") == (1, 3)
        self._triage_and_answer(runner, "B")  # override
        assert runner.rulebook.get_section_state("Auto-answer rules") == (1, 0)
        assert len(events_named(home, "trust:demoted")) == 1

    def test_non_human_answer_never_moves_the_ladder(self, runner, monkeypatch):
        monkeypatch.setenv("FAKE_OPTION", "A")
        self._triage_and_answer(runner, "A", answered_by="timeout-default")
        assert runner.rulebook.get_section_state("Auto-answer rules") == (1, 0)

    def test_unresolvable_ref_is_skipped_with_event_no_state_change(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_OPTION", "A")
        monkeypatch.setenv("FAKE_RULE_REFS", '["rulebook §3.2"]')
        self._triage_and_answer(runner, "A")
        assert len(events_named(home, "trust:ref_unresolved")) == 1
        for section in ("Auto-answer rules", "Edge cases", "Attention priorities"):
            assert runner.rulebook.get_section_state(section) == (1, 0)

    def test_recommendation_matched_still_recorded_on_ledger(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_OPTION", "A")
        self._triage_and_answer(runner, "A")
        ledger = SupervisorState(home).ledger_read()
        entries = [e for e in ledger if e["kind"] == "rule_delta_none"]
        assert entries and entries[0]["recommendation_matched"] is True


# -- Phase-2 auto-answer (Part A3) --------------------------------------------------


class TestAutoAnswer:
    def _promote(self, runner):
        runner.rulebook.set_section_state("Auto-answer rules", 2, 5)

    def test_happy_path_answers_and_records(self, runner, home, monkeypatch):
        monkeypatch.setenv("FAKE_OPTION", "A")
        self._promote(runner)
        packet = make_packet()
        runner.queue.write(packet)

        outcomes = runner.triage_pass()
        assert [o.outcome for o in outcomes] == ["auto_answered"]

        # answered/ holds the canonical resolved packet; pending/ is gone.
        subdir, _ = runner.queue.locate(packet.id)
        assert subdir == "answered"
        assert not runner.queue.path_for(packet.id, "pending").exists()
        answered = runner.queue.get(packet.id)
        assert answered.resolution is not None
        assert answered.resolution.answer == "A"
        assert answered.resolution.answered_by == "manager-auto"
        assert answered.triage is not None and answered.triage.handled_by == "manager-auto"

        # Producers-visible: the worker modules' minimal IO reads this exact file.
        from amplifier_module_tool_request_decision import _read_resolution

        resolution = _read_resolution(runner.queue.root, packet.id)
        assert resolution is not None and resolution["answer"] == "A"
        assert resolution["answered_by"] == "manager-auto"

        # Review record in queue/auto/, unreviewed.
        record = runner.autolog.get(packet.id)
        assert record["reviewed"] is False
        assert record["answer"] == "A"
        assert record["sections"] == ["Auto-answer rules"]
        assert record["rule_refs"] == ["Auto-answer rules"]

        # Events + ledger (recommendation_matched null — no human to match).
        assert len(events_named(home, "triage:auto_answered")) == 1
        ledger = SupervisorState(home).ledger_read()
        entries = [e for e in ledger if e["kind"] == "triage_auto_answered"]
        assert entries and entries[0]["recommendation_matched"] is None

    @pytest.mark.parametrize(
        ("env", "tier", "reason"),
        [
            ({}, "batch", "phase-1 section"),  # section not promoted
            ({"FAKE_CONFIDENCE": "medium"}, "batch", "confidence below high"),
            ({}, "now", "urgency tier now"),
            ({"FAKE_RULE_REFS": "[]"}, "batch", "no cited rules"),
            ({"FAKE_RULE_REFS": '["rulebook §3.2"]'}, "batch", "unresolvable ref"),
        ],
    )
    def test_each_bound_rejects_auto(self, runner, monkeypatch, env, tier, reason):
        monkeypatch.setenv("FAKE_OPTION", "A")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        if reason != "phase-1 section":
            self._promote(runner)
        packet = make_packet(tier=tier)
        runner.queue.write(packet)

        outcomes = runner.triage_pass()
        assert [o.outcome for o in outcomes] == ["recommended"], reason

        # Normal Phase-1 flow: still pending, triage fields filled, NOT answered.
        subdir, _ = runner.queue.locate(packet.id)
        assert subdir == "pending", reason
        updated = runner.queue.get(packet.id)
        assert updated.resolution is None
        assert updated.triage is not None and updated.triage.handled_by == "manager-recommend"

    def test_auto_answered_packet_skipped_by_rule_delta(self, runner, monkeypatch):
        monkeypatch.setenv("FAKE_OPTION", "A")
        self._promote(runner)
        packet = make_packet()
        runner.queue.write(packet)
        runner.triage_pass()
        assert runner.queue.locate(packet.id)[0] == "answered"
        # Second pass: no rule_delta outcome for the auto-answered packet, and
        # its streak is untouched (calibration is the auto CLI's job).
        assert runner.triage_pass() == []
        assert runner.rulebook.get_section_state("Auto-answer rules") == (2, 5)


# -- the auto review log + CLI (Part A4) ----------------------------------------------


class TestAutoLog:
    def test_append_get_list_and_double_append_raises(self, queue_root):
        log = AutoLog(queue_root)
        log.append_record("pkt-1", "A", "why", ["ref"], ["Auto-answer rules"])
        assert log.get("pkt-1")["answer"] == "A"
        assert [r["packet_id"] for r in log.list_records()] == ["pkt-1"]
        with pytest.raises(ValueError, match="already exists"):
            log.append_record("pkt-1", "A", "why", [], [])

    def test_review_marks_and_double_review_raises(self, queue_root):
        log = AutoLog(queue_root)
        log.append_record("pkt-1", "A", "why", [], ["Edge cases"])
        record = log.mark_confirmed("pkt-1")
        assert record["reviewed"] is True and record["review"]["action"] == "confirmed"
        assert log.list_records() == []  # unreviewed view is empty now
        assert log.list_records(include_reviewed=True)[0]["reviewed"] is True
        with pytest.raises(ValueError, match="already reviewed"):
            log.mark_rejected("pkt-1", "B", "nope")

    def test_reject_requires_reason_and_unknown_id_raises(self, queue_root):
        log = AutoLog(queue_root)
        log.append_record("pkt-1", "A", "why", [], [])
        with pytest.raises(ValueError, match="reason"):
            log.mark_rejected("pkt-1", "B", "   ")
        with pytest.raises(KeyError):
            log.get("pkt-nope")


class TestAutoCli:
    def _auto_answer_one(self, runner, monkeypatch) -> Packet:
        monkeypatch.setenv("FAKE_OPTION", "A")
        runner.rulebook.set_section_state("Auto-answer rules", 2, 5)
        packet = make_packet()
        runner.queue.write(packet)
        assert [o.outcome for o in runner.triage_pass()] == ["auto_answered"]
        return packet

    def test_list_shows_unreviewed_with_why_and_rules(self, runner, home, monkeypatch, capsys):
        packet = self._auto_answer_one(runner, monkeypatch)
        assert main(["auto", "list"]) == 0
        out = capsys.readouterr().out
        assert packet.id in out and "covered by the cited rules" in out and "Auto-answer rules" in out
        assert main(["--json", "auto", "list"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data[0]["packet_id"] == packet.id and data[0]["reviewed"] is False

    def test_confirm_counts_as_match_and_can_promote(self, runner, home, monkeypatch, capsys):
        packet = self._auto_answer_one(runner, monkeypatch)
        assert main(["auto", "confirm", packet.id]) == 0
        out = capsys.readouterr().out
        assert "confirmed" in out
        # streak 5 -> 6, still phase 2.
        assert runner.rulebook.get_section_state("Auto-answer rules") == (2, 6)
        assert runner.autolog.get(packet.id)["reviewed"] is True
        assert len(events_named(home, "auto:confirmed")) == 1
        # Second review of the same record fails loud.
        assert main(["auto", "confirm", packet.id]) == 1

    def test_confirm_promotes_a_phase_1_section_at_threshold(self, runner, home, monkeypatch, capsys):
        packet = self._auto_answer_one(runner, monkeypatch)
        # Simulate a later demotion-then-recovery: section back at phase 1 streak 4.
        runner.rulebook.set_section_state("Auto-answer rules", 1, 4)
        assert main(["auto", "confirm", packet.id]) == 0
        assert "PROMOTED" in capsys.readouterr().out
        assert runner.rulebook.get_section_state("Auto-answer rules") == (2, 5)

    def test_reject_demotes_and_records_correction(self, runner, home, monkeypatch, capsys):
        packet = self._auto_answer_one(runner, monkeypatch)
        assert main(["auto", "reject", packet.id, "--correct-option", "B", "--reason", "human disagrees"]) == 0
        out = capsys.readouterr().out
        assert "DEMOTED" in out and "cannot un-answer" in out
        assert runner.rulebook.get_section_state("Auto-answer rules") == (1, 0)
        record = runner.autolog.get(packet.id)
        assert record["review"]["action"] == "rejected"
        assert record["review"]["correct_option"] == "B"
        assert record["review"]["reason"] == "human disagrees"
        assert len(events_named(home, "trust:demoted")) == 1
        assert len(events_named(home, "auto:rejected")) == 1
        # The packet itself is UNCHANGED — still answered with the auto answer.
        answered = runner.queue.get(packet.id)
        assert answered.resolution is not None and answered.resolution.answer == "A"

    def test_reject_validates_correct_option_against_packet(self, runner, home, monkeypatch, capsys):
        packet = self._auto_answer_one(runner, monkeypatch)
        assert main(["auto", "reject", packet.id, "--correct-option", "Z", "--reason", "typo"]) == 1
        assert "Z" in capsys.readouterr().err
        assert runner.autolog.get(packet.id)["reviewed"] is False  # nothing recorded

    def test_confirm_unknown_id_errors(self, home, queue_root, capsys):
        assert main(["auto", "confirm", "pkt-nope"]) == 1
        assert "error:" in capsys.readouterr().err
