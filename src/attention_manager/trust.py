"""Graduated-trust mechanics — Phase 1 → 2 promotion per rulebook section.

Design §Triage ("phase transitions are operational, not vibes"):

* A rule SECTION graduates Phase 1 → 2 after **5 consecutive** human answers
  matching the triage recommendation, with zero overrides in that section.
* Any human override demotes the cited sections back to Phase 1 immediately
  AND resets the streak to 0 — loudly (event + ledger + stderr).
* Promotion state (phase + streak) lives ONLY in the rulebook section heading
  annotation (rulebook.py) — visible, auditable, never in memory.

rule_refs → section resolution is conservative: a ref that cannot be resolved
to exactly one section is SKIPPED with a logged ``trust:ref_unresolved`` event
— never guessed. Mapping a ref to the wrong section would corrupt the ladder.

Callers: the triage rule_delta phase (human answers) and the ``auto confirm``
/ ``auto reject`` CLI (calibration reviews of Phase-2 auto-answers).
"""

from __future__ import annotations

import sys
from typing import TextIO

from .rulebook import Rulebook, resolve_ref
from .state import SupervisorState

# The design's operational threshold: 5 consecutive matches promote a section.
PROMOTE_STREAK = 5
PROMOTED_PHASE = 2
DEMOTED_PHASE = 1


def sections_for_refs(
    rulebook: Rulebook,
    state: SupervisorState,
    packet_id: str,
    refs: list[str],
) -> list[str]:
    """Resolve rule_refs to a de-duplicated section list, skipping loudly.

    Unresolvable refs emit a ``trust:ref_unresolved`` event and are skipped —
    the trust ladder must never be updated on a guessed section.
    """
    content, _ = rulebook.read()
    sections: list[str] = []
    for ref in refs:
        section = resolve_ref(content, ref)
        if section is None:
            state.append_event("trust:ref_unresolved", packet_id=packet_id, rule_ref=ref)
            continue
        if section not in sections:
            sections.append(section)
    return sections


def record_match(
    rulebook: Rulebook,
    state: SupervisorState,
    packet_id: str,
    sections: list[str],
    source: str,
) -> list[dict[str, int | str | bool]]:
    """A human answer MATCHED the triage recommendation: streak+1 per section.

    A Phase-1 section reaching ``PROMOTE_STREAK`` is promoted to Phase 2
    (heading annotation edit + ``trust:promoted`` event + ledger). Phase-2+
    sections keep counting their streak but are never re-promoted.

    Returns per-section outcome dicts (for CLI display).
    """
    outcomes: list[dict[str, int | str | bool]] = []
    for section in sections:
        phase, streak = rulebook.get_section_state(section)
        streak += 1
        promoted = False
        if phase < PROMOTED_PHASE and streak >= PROMOTE_STREAK:
            phase = PROMOTED_PHASE
            promoted = True
        rulebook.set_section_state(section, phase, streak)
        if promoted:
            state.append_event(
                "trust:promoted", packet_id=packet_id, section=section, phase=phase, streak=streak, source=source
            )
            state.ledger_append(
                "trust_promoted", packet_id=packet_id, section=section, phase=phase, streak=streak, source=source
            )
        outcomes.append({"section": section, "phase": phase, "streak": streak, "promoted": promoted})
    return outcomes


def record_override(
    rulebook: Rulebook,
    state: SupervisorState,
    packet_id: str,
    sections: list[str],
    source: str,
    err: TextIO | None = None,
) -> list[dict[str, int | str | bool]]:
    """A human OVERRODE the recommendation (or rejected an auto-answer): demote.

    Every cited section drops to Phase 1 with streak 0 immediately — loud
    (``trust:demoted`` event + ledger + stderr). An override is the strongest
    calibration signal there is; it is never softened.
    """
    err = err or sys.stderr
    outcomes: list[dict[str, int | str | bool]] = []
    for section in sections:
        old_phase, old_streak = rulebook.get_section_state(section)
        rulebook.set_section_state(section, DEMOTED_PHASE, 0)
        state.append_event(
            "trust:demoted",
            packet_id=packet_id,
            section=section,
            from_phase=old_phase,
            phase=DEMOTED_PHASE,
            streak=0,
            source=source,
        )
        state.ledger_append(
            "trust_demoted",
            packet_id=packet_id,
            section=section,
            from_phase=old_phase,
            phase=DEMOTED_PHASE,
            source=source,
        )
        print(
            f"TRUST DEMOTED: section {section!r} phase {old_phase}->{DEMOTED_PHASE}, "
            f"streak {old_streak}->0 (override on {packet_id}, via {source})",
            file=err,
        )
        outcomes.append({"section": section, "phase": DEMOTED_PHASE, "streak": 0, "promoted": False})
    return outcomes
