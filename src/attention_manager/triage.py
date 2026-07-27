"""Cold triage runner — Phase 1 (recommend-only) of the graduated-trust ladder.

Architecture (design decision D8): the runner shells out to the ALREADY
INSTALLED ``amplifier`` CLI for one-shot LLM sessions instead of embedding
amplifier-foundation as a library. The root package stays stdlib-only, the
environment's existing provider config is reused, and all state stays on disk
(D5).

Verdict protocol (no stdout parsing — LLM output goes to disk):

1. The runner builds a prompt containing the rulebook, ONE packet, and an
   exact output path (machine-greppable ``OUTPUT_PATH:`` line).
2. It invokes ``amplifier run -B <triage-bundle-uri> '<prompt>'`` as a
   subprocess (cwd = a per-packet work dir, so the bundle's cwd-scoped write
   permission covers exactly the verdict location), capturing stdout/stderr
   to a log file for diagnostics.
3. It reads and strictly validates the verdict JSON file. A missing or
   invalid verdict after session exit is a triage FAILURE for that packet —
   event ``triage:error``, packet untouched. Never fabricate a verdict.
   One retry max, explicitly logged (D7: fail loud, no silent retries).

Two phases per pass:

- **recommend/bounce** — every pending packet with empty triage fields gets a
  cold verdict: ``recommend`` fills ``packet.triage`` (+ ``recommendation``
  only if the producer supplied none — a producer recommendation is kept, the
  triage one lives in ``triage.why``); ``bounce`` moves the packet to
  ``bounced/`` with the reason merged into ``triage.why``.
- **rule_delta** — every ANSWERED packet that went through triage and has no
  proposal record yet gets ONE proposed rule sentence (or an explicit,
  logged "none — genuinely one-off"). Proposals are recorded in the rulebook
  proposals file; a human applies/rejects them (Phase 1: nothing is applied
  automatically). Idempotent across passes via the proposals packet-id set.

Phase-promotion data (design §Triage): ``recommendation_matched`` is RECORDED
on the rule_delta ledger entry (did the human answer match the triage
recommendation?) but never acted on — promotion automation is out of scope.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import TextIO

from .packet import Packet
from .packet import Recommendation
from .packet import Triage
from .queue import PacketQueue
from .rulebook import SECTIONS
from .rulebook import Rulebook
from .state import SupervisorState

DEFAULT_BUNDLE_URI = "git+https://github.com/bkrabach/amplifier-attention-manager@main#subdirectory=bundles/triage.md"
ENV_BUNDLE = "ATTENTION_TRIAGE_BUNDLE"
ENV_AMPLIFIER_BIN = "ATTENTION_AMPLIFIER_BIN"

DEFAULT_TIMEOUT_S = 240.0
MAX_ATTEMPTS = 2  # one retry max, each attempt logged loudly

VALID_CONFIDENCE = ("low", "medium", "high")
TRIAGE_HANDLED_BY = "manager-recommend"


class VerdictError(ValueError):
    """A verdict file is missing, unparsable, or violates the verdict schema."""


@dataclass
class Outcome:
    """One per-packet outcome from a triage pass (for CLI display)."""

    packet_id: str
    phase: str  # "triage" | "rule_delta"
    outcome: str  # "recommended" | "bounced" | "proposed" | "none" | "error"
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"packet_id": self.packet_id, "phase": self.phase, "outcome": self.outcome, "detail": self.detail}


def default_bundle_uri() -> str:
    return os.environ.get(ENV_BUNDLE) or DEFAULT_BUNDLE_URI


def default_amplifier_bin() -> str:
    return os.environ.get(ENV_AMPLIFIER_BIN) or "amplifier"


# -- prompt construction (machine-greppable header lines are part of the contract) --


def build_triage_prompt(packet: Packet, rulebook_content: str, output_path: Path) -> str:
    return (
        "PHASE: triage\n"
        f"PACKET_ID: {packet.id}\n"
        f"OUTPUT_PATH: {output_path}\n"
        "\n"
        "Triage the packet below cold, from the packet and the rulebook ONLY,\n"
        "per your instructions. Write your verdict JSON to exactly the\n"
        "OUTPUT_PATH above using the write_file tool, and do nothing else.\n"
        "\n"
        "## Rulebook\n"
        "\n"
        f"{rulebook_content}\n"
        "\n"
        "## Packet\n"
        "\n"
        "```json\n"
        f"{packet.to_json()}"
        "```\n"
    )


def build_rule_delta_prompt(packet: Packet, rulebook_content: str, output_path: Path) -> str:
    return (
        "PHASE: rule_delta\n"
        f"PACKET_ID: {packet.id}\n"
        f"OUTPUT_PATH: {output_path}\n"
        "\n"
        "The packet below has been ANSWERED (see its resolution). Propose the\n"
        "ONE rulebook sentence that would have prevented this escalation, or\n"
        "an explicit none if it was genuinely one-off, per your instructions.\n"
        "Write your verdict JSON to exactly the OUTPUT_PATH above using the\n"
        "write_file tool, and do nothing else.\n"
        "\n"
        "## Rulebook\n"
        "\n"
        f"{rulebook_content}\n"
        "\n"
        "## Answered packet\n"
        "\n"
        "```json\n"
        f"{packet.to_json()}"
        "```\n"
    )


# -- verdict validation (strict; fail loud) ------------------------------------


def _load_verdict_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise VerdictError(f"verdict file {path} was not written by the session")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise VerdictError(f"verdict file {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise VerdictError(f"verdict file {path} must contain a JSON object, got {type(data).__name__}")
    return data


def validate_triage_verdict(data: dict[str, Any], packet: Packet) -> dict[str, Any]:
    """Strictly validate a PHASE:triage verdict against the packet. Raises VerdictError."""
    if data.get("packet_id") != packet.id:
        raise VerdictError(f"verdict packet_id {data.get('packet_id')!r} does not match packet {packet.id!r}")
    decision = data.get("decision")
    if decision not in ("recommend", "bounce"):
        raise VerdictError(f"verdict decision {decision!r} not in ('recommend', 'bounce')")
    why = data.get("why")
    if not isinstance(why, str) or not why.strip():
        raise VerdictError("verdict 'why' is required and must be a non-empty string")
    rule_refs = data.get("rule_refs", [])
    if not isinstance(rule_refs, list) or not all(isinstance(r, str) for r in rule_refs):
        raise VerdictError("verdict 'rule_refs' must be a list of strings (may be empty)")

    if decision == "recommend":
        rec = data.get("recommendation")
        if not isinstance(rec, dict):
            raise VerdictError("decision 'recommend' requires a 'recommendation' object")
        option = rec.get("option")
        if option not in packet.option_ids():
            raise VerdictError(
                f"recommendation.option {option!r} is not one of the packet options {packet.option_ids()} "
                "— a triage agent must never invent options"
            )
        if rec.get("confidence") not in VALID_CONFIDENCE:
            raise VerdictError(f"recommendation.confidence {rec.get('confidence')!r} not in {VALID_CONFIDENCE}")
        if not isinstance(rec.get("rationale"), str) or not rec["rationale"].strip():
            raise VerdictError("recommendation.rationale is required and must be non-empty")
    else:  # bounce
        if data.get("recommendation") not in (None,):
            raise VerdictError("decision 'bounce' must carry recommendation: null")
        bounce_reason = data.get("bounce_reason")
        if not isinstance(bounce_reason, str) or not bounce_reason.strip():
            raise VerdictError("decision 'bounce' requires a non-empty 'bounce_reason'")
    return data


def validate_rule_delta_verdict(data: dict[str, Any], packet: Packet) -> dict[str, Any]:
    """Strictly validate a PHASE:rule_delta verdict. Raises VerdictError."""
    if data.get("packet_id") != packet.id:
        raise VerdictError(f"verdict packet_id {data.get('packet_id')!r} does not match packet {packet.id!r}")
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise VerdictError("rule_delta verdict 'reason' is required and must be non-empty")
    if data.get("none") is True:
        return data
    section = data.get("section")
    if section not in SECTIONS:
        raise VerdictError(f"rule_delta section {section!r} not one of {list(SECTIONS)}")
    sentence = data.get("sentence")
    if not isinstance(sentence, str) or not sentence.strip():
        raise VerdictError("rule_delta verdict 'sentence' is required and must be non-empty (or set none: true)")
    return data


# -- the runner ------------------------------------------------------------------


class TriageRunner:
    """Runs cold-triage sessions via the installed ``amplifier`` CLI (D8)."""

    def __init__(
        self,
        home: str | Path | None = None,
        queue: PacketQueue | None = None,
        state: SupervisorState | None = None,
        rulebook: Rulebook | None = None,
        bundle_uri: str | None = None,
        amplifier_bin: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        err: TextIO | None = None,
    ):
        # state is shared with the supervise loop when running inside it; the
        # standalone CLI path creates its own (append-only event/ledger writes
        # only — the runner NEVER calls state.save(), preserving the
        # single-writer invariant on state.json).
        self.state = state or SupervisorState(home)
        self.queue = queue or PacketQueue()
        self.rulebook = rulebook or Rulebook(home=self.state.home)
        self.bundle_uri = bundle_uri or default_bundle_uri()
        self.amplifier_bin = amplifier_bin or default_amplifier_bin()
        self.timeout_s = timeout_s
        self._err = err or sys.stderr

    # -- session execution -----------------------------------------------------

    def _work_dir(self, packet_id: str) -> Path:
        path = self.state.home / "triage" / packet_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _run_session(self, prompt: str, work_dir: Path, log_path: Path) -> None:
        """Run one amplifier session; stdout/stderr go to log_path (diagnostics).

        Raises VerdictError on launch failure / non-zero exit / timeout — the
        caller treats every failure identically (attempt failed, maybe retry).
        """
        cmd = [self.amplifier_bin, "run", "-B", self.bundle_uri, prompt]
        try:
            with open(log_path, "w", encoding="utf-8") as log:
                proc = subprocess.run(  # noqa: S603 — command is our own CLI invocation
                    cmd,
                    cwd=work_dir,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=self.timeout_s,
                    check=False,
                )
        except FileNotFoundError as e:
            raise VerdictError(
                f"amplifier binary {self.amplifier_bin!r} not found — install amplifier or set ${ENV_AMPLIFIER_BIN}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise VerdictError(f"triage session timed out after {self.timeout_s}s (log: {log_path})") from e
        if proc.returncode != 0:
            raise VerdictError(f"triage session exited {proc.returncode} (log: {log_path})")

    def _run_with_retry(self, phase: str, packet: Packet, prompt: str, verdict_path: Path) -> dict[str, Any] | None:
        """Run a session and read+validate its verdict; one retry max, each
        failure logged loudly. Returns the verdict, or None after final failure
        (event ``triage:error`` / ``rule_delta:error`` already emitted)."""
        work_dir = verdict_path.parent
        event_name = "triage:error" if phase == "triage" else "rule_delta:error"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            verdict_path.unlink(missing_ok=True)  # never misread a stale verdict
            log_path = work_dir / f"session-{phase}-{attempt}.log"
            try:
                self._run_session(prompt, work_dir, log_path)
                data = _load_verdict_file(verdict_path)
                if phase == "triage":
                    return validate_triage_verdict(data, packet)
                return validate_rule_delta_verdict(data, packet)
            except VerdictError as e:
                retrying = attempt < MAX_ATTEMPTS
                self.state.append_event(
                    event_name, packet_id=packet.id, error=str(e), attempt=attempt, retrying=retrying
                )
                print(
                    f"ERROR: {phase} attempt {attempt}/{MAX_ATTEMPTS} failed for {packet.id}: {e}"
                    + (" — retrying once" if retrying else " — giving up (packet untouched)"),
                    file=self._err,
                )
        return None

    # -- phase 1: recommend / bounce --------------------------------------------

    def _triage_one(self, packet: Packet, rulebook_content: str) -> Outcome:
        work_dir = self._work_dir(packet.id)
        verdict_path = work_dir / "verdict.json"
        prompt = build_triage_prompt(packet, rulebook_content, verdict_path)
        verdict = self._run_with_retry("triage", packet, prompt, verdict_path)
        if verdict is None:
            return Outcome(packet.id, "triage", "error", "no valid verdict after retries (see triage:error events)")

        why: str = verdict["why"]
        rule_refs: list[str] = list(verdict.get("rule_refs") or [])

        if verdict["decision"] == "recommend":
            rec = verdict["recommendation"]
            # The triage recommendation always lives in triage.why; the packet's
            # recommendation field is only filled when the producer supplied none
            # (a producer recommendation is kept — both remain visible).
            triage_why = f"recommend {rec['option']} ({rec['confidence']}): {why}"
            packet.triage = Triage(handled_by=TRIAGE_HANDLED_BY, rule_refs=rule_refs, why=triage_why)
            if packet.recommendation is None:
                packet.recommendation = Recommendation(
                    option=rec["option"], rationale=rec["rationale"], confidence=rec["confidence"]
                )
            self.queue.write(packet, subdir="pending")  # atomic rewrite in place
            self.state.append_event(
                "triage:recommended",
                packet_id=packet.id,
                option=rec["option"],
                confidence=rec["confidence"],
                rule_refs=rule_refs,
                why=why,
            )
            self.state.ledger_append("triage_recommended", packet_id=packet.id, option=rec["option"], why=why)
            return Outcome(packet.id, "triage", "recommended", f"{rec['option']} ({rec['confidence']}): {why}")

        # bounce: failed the cold-reader test — move pending/ -> bounced/ with why.
        bounce_reason: str = verdict["bounce_reason"]
        packet.triage = Triage(
            handled_by=TRIAGE_HANDLED_BY, rule_refs=rule_refs, why=f"{why} | bounce: {bounce_reason}"
        )
        pending_path = self.queue.path_for(packet.id, "pending")
        self.queue.write(packet, subdir="bounced")  # bounced/ written first; then remove pending
        pending_path.unlink(missing_ok=True)
        self.state.append_event("triage:bounced", packet_id=packet.id, bounce_reason=bounce_reason, why=why)
        self.state.ledger_append("triage_bounced", packet_id=packet.id, bounce_reason=bounce_reason)
        return Outcome(packet.id, "triage", "bounced", bounce_reason)

    # -- phase 2 of the pass: rule_delta proposals -------------------------------

    def _rule_delta_one(self, packet: Packet, rulebook_content: str) -> Outcome:
        work_dir = self._work_dir(packet.id)
        verdict_path = work_dir / "rule_delta.json"
        prompt = build_rule_delta_prompt(packet, rulebook_content, verdict_path)
        verdict = self._run_with_retry("rule_delta", packet, prompt, verdict_path)
        if verdict is None:
            return Outcome(
                packet.id, "rule_delta", "error", "no valid verdict after retries (see rule_delta:error events)"
            )

        # Phase-promotion DATA (recorded, never acted on): did the human's
        # answer match the triage recommendation embedded in triage.why?
        matched: bool | None = None
        if packet.resolution is not None and packet.triage is not None and packet.triage.why:
            prefix = packet.triage.why.split(":", 1)[0]  # "recommend <opt> (<conf>)"
            if prefix.startswith("recommend "):
                recommended_option = prefix.split()[1]
                matched = packet.resolution.answer == recommended_option

        if verdict.get("none") is True:
            record = self.rulebook.record_none(packet.id, verdict["reason"])
            self.state.append_event(
                "rule_delta:none", packet_id=packet.id, proposal_id=record["id"], reason=verdict["reason"]
            )
            self.state.ledger_append(
                "rule_delta_none", packet_id=packet.id, reason=verdict["reason"], recommendation_matched=matched
            )
            return Outcome(packet.id, "rule_delta", "none", verdict["reason"])

        record = self.rulebook.append_proposal(
            packet_id=packet.id,
            section=verdict["section"],
            sentence=verdict["sentence"],
            reason=verdict["reason"],
        )
        self.state.append_event(
            "rule_delta:proposed",
            packet_id=packet.id,
            proposal_id=record["id"],
            section=record["section"],
            sentence=record["sentence"],
        )
        self.state.ledger_append(
            "rule_delta_proposed",
            packet_id=packet.id,
            proposal_id=record["id"],
            section=record["section"],
            sentence=record["sentence"],
            recommendation_matched=matched,
        )
        return Outcome(packet.id, "rule_delta", "proposed", f"[{record['section']}] {record['sentence']}")

    # -- the pass -----------------------------------------------------------------

    def _scan(self, subdir: str) -> list[Packet]:
        """Load packets from a queue subdir, skipping (loudly) malformed files."""
        packets: list[Packet] = []
        for path in sorted(self.queue.dir(subdir).glob("pkt-*.json")):
            try:
                packets.append(Packet.from_json(path.read_text(encoding="utf-8")))
            except ValueError as e:
                self.state.append_event("triage:error", packet_id=path.stem, error=f"malformed packet file: {e}")
                print(f"ERROR: skipping malformed packet file {path}: {e}", file=self._err)
        return sorted(packets, key=lambda p: p.id)

    def triage_pass(self) -> list[Outcome]:
        """One full pass: recommend/bounce all untriaged pending packets, then
        propose rule deltas for answered+triaged packets without one. Idempotent
        across runs — triage fields and the proposals packet-id set are the
        guards; nothing is ever double-processed."""
        rulebook_content, _ = self.rulebook.read()
        outcomes: list[Outcome] = []

        for packet in self._scan("pending"):
            if packet.triage is not None:
                continue  # already triaged
            outcomes.append(self._triage_one(packet, rulebook_content))

        proposed_ids = self.rulebook.proposal_packet_ids()
        for packet in self._scan("answered"):
            if packet.triage is None:
                continue  # never triaged — rule_delta needs the triage why for calibration
            if packet.id in proposed_ids:
                continue  # already has a proposal record (incl. explicit 'none')
            outcomes.append(self._rule_delta_one(packet, rulebook_content))

        return outcomes

    def preflight(self) -> None:
        """Fail loud upfront if the amplifier binary is not available at all."""
        if shutil.which(self.amplifier_bin) is None:
            raise RuntimeError(
                f"amplifier binary {self.amplifier_bin!r} not found on PATH — triage shells out to the "
                f"installed amplifier CLI (D8). Install amplifier or set ${ENV_AMPLIFIER_BIN}."
            )
