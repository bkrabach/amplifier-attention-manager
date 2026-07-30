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

Graduated trust (design §Triage, Phase 2 — build step 6):

- ``recommendation_matched`` is recorded on the rule_delta ledger entry AND
  acted on: a human answer matching the triage recommendation bumps the
  streak of every rulebook section cited in ``triage.rule_refs``; 5
  consecutive matches promote a section to Phase 2; any human override
  demotes the cited sections to Phase 1 with streak 0, loudly (trust.py).
- During the triage phase, a ``recommend`` verdict is AUTO-ANSWERED when ALL
  conservative bounds hold: every cited rule resolves to a Phase-2 section
  (and there is at least one), confidence is ``high``, and the packet's
  urgency tier is not ``now``. The packet moves ``pending/`` → ``answered/``
  (the canonical copy producers poll to unblock) and a review record lands in
  ``queue/auto/`` for human calibration (``auto`` CLI). Any bound failing
  falls back to the unchanged Phase-1 recommend flow.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from . import trust
from .autolog import AutoLog
from .packet import Packet, Recommendation, Resolution, Triage, utc_now_iso
from .queue import PacketQueue
from .rulebook import SECTIONS, Rulebook
from .state import SupervisorState

DEFAULT_BUNDLE_URI = "git+https://github.com/bkrabach/amplifier-attention-manager@main#subdirectory=bundles/triage.md"
ENV_BUNDLE = "ATTENTION_TRIAGE_BUNDLE"
ENV_AMPLIFIER_BIN = "ATTENTION_AMPLIFIER_BIN"

DEFAULT_TIMEOUT_S = 240.0
MAX_ATTEMPTS = 2  # one retry max PER PASS, each attempt logged loudly

# Cross-pass retry cap (defect: unbounded cost bleed). A packet that fails
# triage/rule_delta on ABANDON_AFTER_PASSES consecutive passes (each pass =
# up to MAX_ATTEMPTS sessions) is abandoned LOUDLY ONCE (event + ledger +
# stderr) and skipped on every future pass. The human answers it normally via
# the queue (Phase 1 without a recommendation — that path always works).
# Escape hatch: `attention-manager triage --retry <packet_id>` clears the
# marker. The count lives on disk in the packet's triage work dir (D5:
# disk-rebuildable); losing the marker only re-permits retries.
ABANDON_AFTER_PASSES = 3

VALID_CONFIDENCE = ("low", "medium", "high")
TRIAGE_HANDLED_BY = "manager-recommend"

# Session isolation (host defect: user-level bundle.app composition).
#
# amplifier-app-cli composes every URI in the MERGED settings' ``bundle.app``
# list onto EVERY ``amplifier run`` regardless of ``-B`` (runtime/config.py,
# "Add app bundles ... always composed"). On a real host that list can carry
# a dozen-plus unrelated bundles whose instructions bury the triage bundle's
# verdict contract — observed live: 127k-token triage inputs (vs ~64k clean)
# and invented verdict schemas ({"verdict": "escalate", ...}) that fail
# strict validation on every attempt.
#
# The isolation mechanism is the app-cli's OWN scope precedence: settings are
# deep-merged global -> project -> local, where "project" is
# ``<cwd>/.amplifier/settings.yaml`` and non-dict values (lists) are REPLACED
# by the more specific scope. The runner already sets each session's cwd to
# the per-packet work dir, so planting this file there replaces bundle.app
# with [] for triage sessions ONLY. ``config.providers`` is left undefined
# here, so provider config still merges through from the user's global
# settings (verified on host). Notifications are also disabled: a
# programmatic one-shot triage session must never ping the human.
SESSION_ISOLATION_SETTINGS = """\
# Written by attention-manager's triage runner — session isolation.
# This is PROJECT-scope settings for amplifier sessions whose cwd is this
# work dir. It replaces the user's global bundle.app list with [] so
# user-level app bundles are NOT composed onto triage sessions (they inject
# unrelated instructions that broke verdict schema compliance), and disables
# notifications for these programmatic one-shot sessions. Providers are NOT
# overridden — they merge through from global settings. Safe to delete;
# regenerated on the next triage pass.
bundle:
  app: []
config:
  notifications:
    desktop:
      enabled: false
    ntfy:
      enabled: false
"""

# Phase-2 auto-answer identity: triage.handled_by and resolution.answered_by
# both carry it, so every consumer (producers, CLI, ledger) can tell an
# auto-answer from a human or timeout answer at a glance.
AUTO_ANSWERED_BY = "manager-auto"
# The design's conservative auto-answer bound: never auto-answer a "now" packet.
AUTO_EXCLUDED_TIER = "now"
AUTO_REQUIRED_CONFIDENCE = "high"


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

# Schema restatements appended at the END of every prompt (host defect:
# schema non-compliance). Recency matters in long LLM contexts: on hosts
# where extra instructions get composed onto the session, the bundle's
# schema (loaded early) was buried and the model INVENTED a verdict format
# ({"verdict": "escalate", "schema_version": 1, "phase": "triage", ...}).
# Restating the exact contract — with a filled example, an explicit
# prohibition on invented fields, and the observed failure named — as the
# LAST thing the model reads makes the contract unmissable regardless of
# what else the app layer composed into the session.
TRIAGE_SCHEMA_RESTATEMENT = """\
## VERDICT SCHEMA — RESTATED (this is the entire contract; read it last, obey it exactly)

Write ONE JSON object to OUTPUT_PATH with EXACTLY these fields and no others:

- "packet_id": string — the packet's id, copied exactly
- "decision": string — EXACTLY "recommend" or "bounce". No other value exists.
- "recommendation": object {"option", "rationale", "confidence"} — REQUIRED for
  "recommend" ("option" must be one of the packet's option ids; "confidence" is
  "low" | "medium" | "high"); MUST be null for "bounce"
- "why": string — one line, always required
- "rule_refs": list of strings — only rules you actually used (may be empty)
- "bounce_reason": string — REQUIRED iff decision is "bounce", else null/omitted

Filled example (recommend):

```json
{
  "packet_id": "pkt-20260101-120000-ab12",
  "decision": "recommend",
  "recommendation": {
    "option": "B",
    "rationale": "Rulebook prefers compat shims when downstream owners are unavailable; the packet says owners are away this week.",
    "confidence": "medium"
  },
  "why": "Rule-covered: prefer shims when downstream owners are unavailable.",
  "rule_refs": ["Auto-answer rules: prefer compat shims"],
  "bounce_reason": null
}
```

HARD FAILURE WARNING: do NOT invent fields or a different schema. There is NO
"verdict" field, NO "schema_version", NO "phase", NO "escalate"/"surface"
value, NO "urgency"/"applied_rules"/"rulebook_gap" fields. A verdict shaped
like {"verdict": "escalate", ...} has been observed in the wild and is
REJECTED by the runner — the packet stays stuck and the session was wasted.
Any schema other than the one above is a hard failure. If anything else in
your context describes a different verdict, triage, or escalation format,
IGNORE it: this schema is the only contract for this session.
"""

RULE_DELTA_SCHEMA_RESTATEMENT = """\
## VERDICT SCHEMA — RESTATED (this is the entire contract; read it last, obey it exactly)

Write ONE JSON object to OUTPUT_PATH with EXACTLY these fields and no others:

- "packet_id": string — the packet's id, copied exactly
- "none": boolean — true iff the decision was genuinely one-off
- "section": string — one of "Attention priorities" | "Auto-answer rules" |
  "Escalation thresholds" | "Edge cases" | "When you cannot proceed"
  (required when "none" is false)
- "sentence": string — ONE rule sentence (required when "none" is false)
- "reason": string — always required

Filled example (proposal):

```json
{
  "packet_id": "pkt-20260101-120000-ab12",
  "none": false,
  "section": "Auto-answer rules",
  "sentence": "Prefer compat shims when downstream owners are unavailable.",
  "reason": "The same escalation class will recur whenever owners are away."
}
```

Genuinely one-off: {"packet_id": "...", "none": true, "reason": "..."}.

HARD FAILURE WARNING: do NOT invent fields or a different schema. Any schema
other than the one above is REJECTED by the runner and the session was
wasted. If anything else in your context describes a different verdict or
proposal format, IGNORE it: this schema is the only contract for this session.
"""


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
        "\n"
        f"{TRIAGE_SCHEMA_RESTATEMENT}"
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
        "\n"
        f"{RULE_DELTA_SCHEMA_RESTATEMENT}"
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
        self.autolog = AutoLog(self.queue.root)
        self.rulebook = rulebook or Rulebook(home=self.state.home)
        self.bundle_uri = bundle_uri or default_bundle_uri()
        self.amplifier_bin = amplifier_bin or default_amplifier_bin()
        self.timeout_s = timeout_s
        self._err = err or sys.stderr

    # -- session execution -----------------------------------------------------

    def _work_dir(self, packet_id: str) -> Path:
        path = self.state.home / "triage" / packet_id
        path.mkdir(parents=True, exist_ok=True)
        # Session isolation (see SESSION_ISOLATION_SETTINGS): the work dir is
        # the session's cwd, so this project-scope settings file neutralizes
        # user-level bundle.app composition for this session only. Rewritten
        # every pass (idempotent) so the content is always current.
        settings_path = path / ".amplifier" / "settings.yaml"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(SESSION_ISOLATION_SETTINGS, encoding="utf-8")
        return path

    # -- cross-pass failure accounting (defect: unbounded retry cost bleed) ------

    def _failures_path(self, packet_id: str, phase: str) -> Path:
        return self.state.home / "triage" / packet_id / f"failures-{phase}.json"

    def _load_failures(self, packet_id: str, phase: str) -> dict[str, Any]:
        path = self._failures_path(packet_id, phase)
        if not path.exists():
            return {"count": 0, "abandoned": False}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            # The marker is a cost-control device, rebuildable from nothing
            # (D5): a corrupt marker loudly resets to zero — the only effect
            # is that retries are re-permitted, never that work is lost.
            print(f"ERROR: corrupt failure marker {path} — resetting count to 0", file=self._err)
            self.state.append_event(f"{phase}:error", packet_id=packet_id, error="corrupt failure marker reset")
            return {"count": 0, "abandoned": False}
        return {"count": int(data.get("count", 0)), "abandoned": bool(data.get("abandoned", False))}

    def _is_abandoned(self, packet_id: str, phase: str) -> bool:
        return self._load_failures(packet_id, phase)["abandoned"]

    def _record_pass_failure(self, packet: Packet, phase: str) -> bool:
        """Record one FAILED PASS (all attempts exhausted) for a packet+phase.

        After ABANDON_AFTER_PASSES failed passes the packet is abandoned:
        ONE loud ``<phase>:abandoned`` event + ledger entry + stderr line, and
        every future pass skips it (no more LLM sessions). Returns True iff
        the packet was abandoned by THIS failure.
        """
        failures = self._load_failures(packet.id, phase)
        count = failures["count"] + 1
        abandoned = count >= ABANDON_AFTER_PASSES
        path = self._failures_path(packet.id, phase)
        payload = {
            "packet_id": packet.id,
            "phase": phase,
            "count": count,
            "abandoned": abandoned,
            "updated_at": utc_now_iso(),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        if abandoned and not failures["abandoned"]:
            self.state.append_event(f"{phase}:abandoned", packet_id=packet.id, failures=count)
            self.state.ledger_append(f"{phase}_abandoned", packet_id=packet.id, failures=count)
            print(
                f"ABANDONED: {phase} for {packet.id} after {count} failed passes "
                f"({count * MAX_ATTEMPTS} sessions) — skipping it on future passes. "
                "Answer it via the queue, or clear with: attention-manager triage --retry " + packet.id,
                file=self._err,
            )
        return abandoned

    def _clear_failures(self, packet_id: str, phase: str) -> None:
        self._failures_path(packet_id, phase).unlink(missing_ok=True)

    def clear_abandon_markers(self, packet_id: str) -> list[str]:
        """Manual escape hatch (``triage --retry <packet_id>``): clear the
        failure markers for a packet so the next pass re-attempts it.
        Returns the phases that had a marker."""
        cleared = []
        for phase in ("triage", "rule_delta"):
            path = self._failures_path(packet_id, phase)
            if path.exists():
                path.unlink()
                cleared.append(phase)
        if cleared:
            self.state.append_event("triage:retry_cleared", packet_id=packet_id, phases=cleared)
        return cleared

    def _run_session(self, prompt: str, work_dir: Path, log_path: Path) -> None:
        """Run one amplifier session; stdout/stderr go to log_path (diagnostics).

        Raises VerdictError on launch failure / non-zero exit / timeout — the
        caller treats every failure identically (attempt failed, maybe retry).
        """
        cmd = [self.amplifier_bin, "run", "-B", self.bundle_uri, prompt]
        try:
            with open(log_path, "w", encoding="utf-8") as log:
                proc = subprocess.run(
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
            abandoned = self._record_pass_failure(packet, "triage")
            detail = (
                f"ABANDONED after {ABANDON_AFTER_PASSES} failed passes — answer via queue, or 'triage --retry'"
                if abandoned
                else "no valid verdict after retries (see triage:error events)"
            )
            return Outcome(packet.id, "triage", "error", detail)
        self._clear_failures(packet.id, "triage")

        why: str = verdict["why"]
        rule_refs: list[str] = list(verdict.get("rule_refs") or [])

        if verdict["decision"] == "recommend":
            rec = verdict["recommendation"]
            # Phase 2 (graduated trust): auto-answer when EVERY conservative
            # bound holds; any failure falls through to the unchanged Phase-1
            # recommend flow.
            auto_sections = self._auto_answer_sections(packet, rec, rule_refs)
            if auto_sections is not None:
                return self._auto_answer(packet, rec, rule_refs, why, auto_sections)
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
        return self._bounce(packet, verdict, why, rule_refs)

    def _bounce(self, packet: Packet, verdict: dict[str, Any], why: str, rule_refs: list[str]) -> Outcome:
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

    # -- Phase-2 auto-answer (graduated trust; conservative bounds by design) ----

    def _auto_answer_sections(self, packet: Packet, rec: dict[str, Any], rule_refs: list[str]) -> list[str] | None:
        """Return the resolved phase-2 sections if the packet may be auto-answered.

        ALL bounds must hold (conservative by design — any doubt means Phase-1
        recommend flow):

        - triage confidence is ``high``;
        - the packet's urgency tier is not ``now``;
        - there is at least ONE cited rule, and EVERY cited rule resolves to a
          rulebook section that is currently Phase 2+ (an unresolvable ref
          fails the bound — never guess).

        Returns None when any bound fails.
        """
        if rec.get("confidence") != AUTO_REQUIRED_CONFIDENCE:
            return None
        if packet.urgency.tier == AUTO_EXCLUDED_TIER:
            return None
        if not rule_refs:
            return None
        sections: list[str] = []
        for ref in rule_refs:
            section = self.rulebook.resolve_ref_to_section(ref)
            if section is None:
                return None  # unresolvable ref — cannot prove phase-2 coverage
            phase, _ = self.rulebook.get_section_state(section)
            if phase < 2:
                return None
            if section not in sections:
                sections.append(section)
        return sections

    def _auto_answer(
        self, packet: Packet, rec: dict[str, Any], rule_refs: list[str], why: str, sections: list[str]
    ) -> Outcome:
        """Auto-answer a rule-covered packet (Phase 2).

        File-op order (crash-safe, mirrors queue.answer()): write the resolved
        packet to ``answered/`` FIRST — that is the canonical copy producers
        poll to unblock — then remove ``pending/``, then append the review
        record to ``queue/auto/``. answered/ is authoritative whenever it
        exists (context/packet-schema.md).
        """
        triage_why = f"recommend {rec['option']} ({rec['confidence']}): {why}"
        packet.triage = Triage(handled_by=AUTO_ANSWERED_BY, rule_refs=rule_refs, why=triage_why)
        if packet.recommendation is None:
            packet.recommendation = Recommendation(
                option=rec["option"], rationale=rec["rationale"], confidence=rec["confidence"]
            )
        packet.resolution = Resolution(
            answer=rec["option"],
            answered_by=AUTO_ANSWERED_BY,
            answered_at=utc_now_iso(),
            rationale=why,
        )
        pending_path = self.queue.path_for(packet.id, "pending")
        self.queue.write(packet, subdir="answered")  # canonical — producers unblock on this
        pending_path.unlink(missing_ok=True)
        self.autolog.append_record(
            packet_id=packet.id, answer=rec["option"], why=why, rule_refs=rule_refs, sections=sections
        )
        self.state.append_event(
            "triage:auto_answered",
            packet_id=packet.id,
            option=rec["option"],
            confidence=rec["confidence"],
            rule_refs=rule_refs,
            sections=sections,
            why=why,
        )
        self.state.ledger_append(
            "triage_auto_answered",
            packet_id=packet.id,
            option=rec["option"],
            sections=sections,
            why=why,
            recommendation_matched=None,  # no human answered — nothing to match against
        )
        return Outcome(packet.id, "triage", "auto_answered", f"{rec['option']} (phase-2 sections {sections}): {why}")

    # -- phase 2 of the pass: rule_delta proposals -------------------------------

    def _rule_delta_one(self, packet: Packet, rulebook_content: str) -> Outcome:
        work_dir = self._work_dir(packet.id)
        verdict_path = work_dir / "rule_delta.json"
        prompt = build_rule_delta_prompt(packet, rulebook_content, verdict_path)
        verdict = self._run_with_retry("rule_delta", packet, prompt, verdict_path)
        if verdict is None:
            abandoned = self._record_pass_failure(packet, "rule_delta")
            detail = (
                f"ABANDONED after {ABANDON_AFTER_PASSES} failed passes — 'triage --retry' to re-attempt"
                if abandoned
                else "no valid verdict after retries (see rule_delta:error events)"
            )
            return Outcome(packet.id, "rule_delta", "error", detail)
        self._clear_failures(packet.id, "rule_delta")

        # Phase-promotion data (design §Triage): did the human's answer match
        # the triage recommendation embedded in triage.why? Recorded on the
        # ledger AND acted on (graduated trust — see _apply_trust below).
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
            self._apply_trust(packet, matched)
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
        self._apply_trust(packet, matched)
        return Outcome(packet.id, "rule_delta", "proposed", f"[{record['section']}] {record['sentence']}")

    # -- graduated trust: streak updates on HUMAN answers (design §Triage) --------

    def _apply_trust(self, packet: Packet, matched: bool | None) -> None:
        """Update the trust ladder from one newly-answered, triaged packet.

        Only HUMAN answers move the ladder (auto/timeout answers carry no
        calibration signal here — auto-answers are calibrated via ``auto
        confirm``/``auto reject``), and only when triage actually recommended
        (matched is None when it did not).

        Runs AFTER the rule_delta proposal record is written: the record is
        the shared idempotency key, so a crash between record and trust update
        UNDER-counts (conservative) rather than double-counting a match.
        """
        if matched is None:
            return
        if packet.resolution is None or packet.resolution.answered_by != "human":
            return
        refs = list(packet.triage.rule_refs or []) if packet.triage is not None else []
        sections = trust.sections_for_refs(self.rulebook, self.state, packet.id, refs)
        if not sections:
            return
        if matched:
            trust.record_match(self.rulebook, self.state, packet.id, sections, source="rule_delta")
        else:
            trust.record_override(self.rulebook, self.state, packet.id, sections, source="rule_delta", err=self._err)

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
            if self._is_abandoned(packet.id, "triage"):
                continue  # abandoned loudly once — human answers via queue; 'triage --retry' re-enables
            outcomes.append(self._triage_one(packet, rulebook_content))

        proposed_ids = self.rulebook.proposal_packet_ids()
        for packet in self._scan("answered"):
            if packet.triage is None:
                continue  # never triaged — rule_delta needs the triage why for calibration
            if packet.resolution is not None and packet.resolution.answered_by == AUTO_ANSWERED_BY:
                # Auto-answered: the rule that answered it already exists, so a
                # rule_delta proposal is meaningless; calibration happens via
                # the `auto confirm` / `auto reject` CLI instead.
                continue
            if packet.id in proposed_ids:
                continue  # already has a proposal record (incl. explicit 'none')
            if self._is_abandoned(packet.id, "rule_delta"):
                continue  # abandoned loudly once — 'triage --retry' re-enables
            outcomes.append(self._rule_delta_one(packet, rulebook_content))

        return outcomes

    def preflight(self) -> None:
        """Fail loud upfront if the amplifier binary is not available at all."""
        if shutil.which(self.amplifier_bin) is None:
            raise RuntimeError(
                f"amplifier binary {self.amplifier_bin!r} not found on PATH — triage shells out to the "
                f"installed amplifier CLI (D8). Install amplifier or set ${ENV_AMPLIFIER_BIN}."
            )
