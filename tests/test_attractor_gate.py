"""PacketInterviewer tests against a STUB loop-pipeline (no real dependency needed).

The stub reproduces the exact dataclass shapes of
``amplifier_module_loop_pipeline.interviewer`` (verified against the shipped
source). The real-engine integration test lives in test_workunit.py and is
skipped when the optional dependency is not installed.
"""

from __future__ import annotations

import enum
import json
import sys
import threading
import time
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from attention_manager.attractor_gate import INSTALL_HINT, PacketInterviewer, import_loop_pipeline
from attention_manager.queue import PacketQueue

# -- stub loop-pipeline -------------------------------------------------------


class StubQuestionType(enum.Enum):
    YES_NO = "yes_no"
    MULTIPLE_CHOICE = "multiple_choice"
    FREEFORM = "freeform"
    CONFIRMATION = "confirmation"


@dataclass
class StubOption:
    key: str
    label: str


@dataclass
class StubQuestion:
    text: str
    type: StubQuestionType
    options: list[StubOption] = field(default_factory=list)
    default: Any = None
    timeout_seconds: float | None = None
    stage: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StubAnswer:
    value: Any = ""
    selected_option: Any = None
    text: str = ""


def _purge_lp_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        k
        for k in sys.modules
        if k == "amplifier_module_loop_pipeline" or k.startswith("amplifier_module_loop_pipeline.")
    ]:
        monkeypatch.delitem(sys.modules, key)


@pytest.fixture
def stub_lp(monkeypatch) -> types.ModuleType:
    """Install a stub amplifier_module_loop_pipeline into sys.modules."""
    pkg = types.ModuleType("amplifier_module_loop_pipeline")
    pkg.__path__ = []  # mark as package so submodule imports resolve via sys.modules
    interviewer = types.ModuleType("amplifier_module_loop_pipeline.interviewer")
    interviewer.QuestionType = StubQuestionType  # type: ignore[attr-defined]
    interviewer.Option = StubOption  # type: ignore[attr-defined]
    interviewer.Question = StubQuestion  # type: ignore[attr-defined]
    interviewer.Answer = StubAnswer  # type: ignore[attr-defined]
    pkg.interviewer = interviewer  # type: ignore[attr-defined]
    _purge_lp_modules(monkeypatch)
    monkeypatch.setitem(sys.modules, "amplifier_module_loop_pipeline", pkg)
    monkeypatch.setitem(sys.modules, "amplifier_module_loop_pipeline.interviewer", interviewer)
    return interviewer


@pytest.fixture
def events():
    """Recording events emitter with the state.append_event call shape."""
    records: list[dict[str, Any]] = []

    def emit(event: str, **fields: Any) -> None:
        records.append({"event": event, **fields})

    emit.records = records  # type: ignore[attr-defined]
    return emit


def _mc_question(**overrides: Any) -> StubQuestion:
    defaults: dict[str, Any] = dict(
        text="Approve the work unit?",
        type=StubQuestionType.MULTIPLE_CHOICE,
        options=[StubOption("A", "[A] Approve"), StubOption("R", "[R] Reject")],
        stage="gate",
    )
    defaults.update(overrides)
    return StubQuestion(**defaults)


# -- Question -> packet mapping -----------------------------------------------


async def test_multiple_choice_roundtrip(stub_lp, queue_root, answer_when_pending, events):
    queue = PacketQueue(queue_root)
    interviewer = PacketInterviewer(queue, "portfix", events_emitter=events, poll_s=0.05)
    answer_when_pending(queue_root, "A", rationale="ship it")

    answer = await interviewer.async_ask(_mc_question())

    assert isinstance(answer, StubAnswer)
    assert answer.value == "A"

    # the resolved packet has the full mapping
    answered = list((queue_root / "answered").glob("pkt-*.json"))
    assert len(answered) == 1
    packet = json.loads(answered[0].read_text(encoding="utf-8"))
    assert packet["source"]["kind"] == "attractor-gate"
    assert packet["source"]["work_unit"] == "portfix"
    assert [o["id"] for o in packet["options"]] == ["A", "R"]
    assert [o["label"] for o in packet["options"]] == ["[A] Approve", "[R] Reject"]
    assert packet["question"] == "Approve the work unit?"
    assert "stage: gate" in packet["context"]
    # no timeout declared -> no deadline, no on_timeout
    assert "deadline" not in packet["urgency"]
    assert "on_timeout" not in packet["urgency"]

    # events: created then answered, correlated by stage + packet_id
    names = [r["event"] for r in events.records]
    assert names == ["gate:packet_created", "gate:answered"]
    created, resolved = events.records
    assert created["work_unit"] == "portfix"
    assert created["stage"] == "gate"
    assert created["packet_id"] == packet["id"]
    assert resolved["packet_id"] == packet["id"]
    assert resolved["answer"] == "A"


def test_option_dedupe_keeps_first(stub_lp, queue_root, events):
    interviewer = PacketInterviewer(PacketQueue(queue_root), "wu", events_emitter=events)
    question = _mc_question(
        options=[
            StubOption("A", "[A] Approve"),
            StubOption("A", "[A] Abort"),  # pathological duplicate key
            StubOption("R", "[R] Reject"),
        ]
    )
    packet = interviewer.build_packet(question)
    assert [o.id for o in packet.options] == ["A", "R"]
    assert packet.options[0].label == "[A] Approve"  # first occurrence wins


def test_metadata_description_lands_in_context(stub_lp, queue_root, events):
    interviewer = PacketInterviewer(PacketQueue(queue_root), "wu", events_emitter=events)
    packet = interviewer.build_packet(_mc_question(metadata={"description": "extra decision material"}))
    assert "stage: gate" in packet.context
    assert "extra decision material" in packet.context


async def test_confirmation_synthesizes_yes_no(stub_lp, queue_root, answer_when_pending, events):
    queue = PacketQueue(queue_root)
    interviewer = PacketInterviewer(queue, "wu", events_emitter=events, poll_s=0.05)
    question = StubQuestion(text="Proceed?", type=StubQuestionType.CONFIRMATION, stage="confirm")
    answer_when_pending(queue_root, "yes")

    answer = await interviewer.async_ask(question)

    # "yes"/"no" are exactly the AnswerValue.YES/.NO string values
    assert answer.value == "yes"
    answered = json.loads(next((queue_root / "answered").glob("pkt-*.json")).read_text(encoding="utf-8"))
    assert [o["id"] for o in answered["options"]] == ["yes", "no"]


def test_yes_no_type_also_synthesizes(stub_lp, queue_root, events):
    interviewer = PacketInterviewer(PacketQueue(queue_root), "wu", events_emitter=events)
    packet = interviewer.build_packet(StubQuestion(text="Ok?", type=StubQuestionType.YES_NO, stage="s"))
    assert [o.id for o in packet.options] == ["yes", "no"]


def test_freeform_rejected_loud(stub_lp, queue_root, events):
    interviewer = PacketInterviewer(PacketQueue(queue_root), "wu-free", events_emitter=events)
    question = StubQuestion(text="Say anything", type=StubQuestionType.FREEFORM, stage="freeform-gate")
    with pytest.raises(ValueError, match=r"freeform-gate.*wu-free.*FREEFORM.*not supported"):
        interviewer.build_packet(question)
    # nothing was written
    assert not list((queue_root / "pending").glob("pkt-*.json")) if (queue_root / "pending").exists() else True


def test_single_option_gate_rejected_loud(stub_lp, queue_root, events):
    interviewer = PacketInterviewer(PacketQueue(queue_root), "wu", events_emitter=events)
    question = _mc_question(options=[StubOption("A", "[A] Approve")], stage="lonely-gate")
    with pytest.raises(ValueError, match=r"lonely-gate.*at least 2"):
        interviewer.build_packet(question)


# -- resolution flow ----------------------------------------------------------


def test_misroute_protection_invalid_answer_raises(stub_lp, queue_root, events):
    """An answered/ file whose answer is NOT a declared option must raise, never
    return: the engine silently routes unrecognized answers to the first edge."""
    queue = PacketQueue(queue_root)
    interviewer = PacketInterviewer(queue, "wu", events_emitter=events, poll_s=0.05)

    def corrupt_answer() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pending = list((queue_root / "pending").glob("pkt-*.json")) if (queue_root / "pending").exists() else []
            if pending:
                data = json.loads(pending[0].read_text(encoding="utf-8"))
                data["resolution"] = {
                    "answer": "Z",  # not one of A/R — bypasses queue.answer() validation
                    "answered_by": "human",
                    "answered_at": "2026-07-27T00:00:00Z",
                }
                answered_dir = queue_root / "answered"
                answered_dir.mkdir(parents=True, exist_ok=True)
                (answered_dir / pending[0].name).write_text(json.dumps(data), encoding="utf-8")
                pending[0].unlink()
                return
            time.sleep(0.02)
        raise TimeoutError("no pending packet appeared to corrupt")

    thread = threading.Thread(target=corrupt_answer, daemon=True)
    thread.start()
    with pytest.raises(RuntimeError, match=r"'Z'.*NOT one of.*\['A', 'R'\].*misroute"):
        interviewer.ask(_mc_question())
    thread.join(timeout=10)
    # gate:answered must NOT have been emitted for the corrupt resolution
    assert [r["event"] for r in events.records] == ["gate:packet_created"]


async def test_timeout_declared_and_fail_loud(stub_lp, queue_root, events):
    queue = PacketQueue(queue_root)
    interviewer = PacketInterviewer(queue, "wu", events_emitter=events, poll_s=0.02)
    question = _mc_question(timeout_seconds=0.15)

    with pytest.raises(TimeoutError):
        await interviewer.async_ask(question)

    # the packet declared the policy explicitly and is STILL pending (nothing
    # fabricated an answer)
    pending = list((queue_root / "pending").glob("pkt-*.json"))
    assert len(pending) == 1
    packet = json.loads(pending[0].read_text(encoding="utf-8"))
    assert packet["urgency"]["deadline"]
    assert packet["urgency"]["on_timeout"] == {"action": "fail-loud"}


def test_no_timeout_waits_beyond_poll(stub_lp, queue_root, answer_when_pending, events):
    """No timeout by default: the sync ask blocks until answered."""
    queue = PacketQueue(queue_root)
    interviewer = PacketInterviewer(queue, "wu", events_emitter=events, poll_s=0.05)
    answer_when_pending(queue_root, "R")
    answer = interviewer.ask(_mc_question())
    assert answer.value == "R"


def test_ask_multiple_sequential(stub_lp, queue_root, events):
    queue = PacketQueue(queue_root)
    interviewer = PacketInterviewer(queue, "wu", events_emitter=events, poll_s=0.02)

    def answer_all() -> None:
        answered = 0
        deadline = time.monotonic() + 15
        while answered < 2 and time.monotonic() < deadline:
            root_queue = PacketQueue(queue_root)
            pending = root_queue.list_pending()
            if pending:
                root_queue.answer(pending[0].id, "A")
                answered += 1
            time.sleep(0.02)

    thread = threading.Thread(target=answer_all, daemon=True)
    thread.start()
    answers = interviewer.ask_multiple([_mc_question(), _mc_question(stage="gate2")])
    thread.join(timeout=15)
    assert [a.value for a in answers] == ["A", "A"]


def test_inform_appends_event_never_print_only(stub_lp, queue_root, events, capsys):
    interviewer = PacketInterviewer(PacketQueue(queue_root), "wu-inform", events_emitter=events)
    interviewer.inform("halfway there")
    assert events.records == [{"event": "workunit:inform", "work_unit": "wu-inform", "message": "halfway there"}]
    assert capsys.readouterr().out == ""  # not printed


# -- lazy import helper ---------------------------------------------------------


def test_import_helper_actionable_error(monkeypatch):
    _purge_lp_modules(monkeypatch)
    monkeypatch.setitem(sys.modules, "amplifier_module_loop_pipeline", None)
    monkeypatch.setitem(sys.modules, "amplifier_module_loop_pipeline.interviewer", None)
    with pytest.raises(ImportError, match=r"amplifier-attention-manager\[attractor\]"):
        import_loop_pipeline("interviewer")
    assert "attractor" in INSTALL_HINT
