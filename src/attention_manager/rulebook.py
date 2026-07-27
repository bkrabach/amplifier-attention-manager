"""Rulebook management — the self-growing triage rules file (design §Rulebook Contract).

The rulebook is ONE markdown file (``$ATTENTION_HOME/rulebook.md``), structured
in the design's five sections, read by every triage pass. It has an explicit
token cap: hitting the cap REFUSES the append with a loud error instructing
consolidation ("3+ citations of one rule = one badly written rule").

Rule-change PROPOSALS live in ``$ATTENTION_HOME/rulebook-proposals.jsonl`` —
one JSON record per packet. Phase 1 (recommend-only): the manager PROPOSES rule
deltas after human answers; a human applies or rejects them via the CLI. The
rulebook file itself is only ever modified through :meth:`Rulebook.apply`
(never automatically).

Token counting uses the ``len(content) // 4`` characters-per-token heuristic —
deliberately crude; the cap exists to force consolidation pressure, not to
meter an exact budget. Documented here so nobody mistakes it for real
tokenization.

Fail-loud (D7): every refusal raises ``ValueError`` with a specific message.
All file mutations are atomic (tmp + os.replace, as everywhere else).
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from .packet import utc_now_iso
from .state import default_home

RULEBOOK_FILENAME = "rulebook.md"
PROPOSALS_FILENAME = "rulebook-proposals.jsonl"

DEFAULT_TOKEN_CAP = 2000

# The design's five sections, in canonical order (§Rulebook Contract).
SECTIONS = (
    "Attention priorities",
    "Auto-answer rules",
    "Escalation thresholds",
    "Edge cases",
    "When you cannot proceed",
)

_SECTION_INTROS = {
    "Attention priorities": "What reaches the human first, and in what order. Highest-signal rules only.",
    "Auto-answer rules": (
        "What the manager may decide itself, with explicit bounds. "
        "Phase 1: these are recommendations only — nothing is auto-answered."
    ),
    "Escalation thresholds": "When a routine situation stops being routine and must surface to the human.",
    "Edge cases": "Known exceptions to the rules above. If this section grows fast, a rule above is badly written.",
    "When you cannot proceed": (
        "What to do when no rule applies: bounce malformed packets, surface genuine decisions with a why. "
        "Never invent an answer."
    ),
}

TEMPLATE_HEADER = """\
# Attention Manager Rulebook

Read by every triage pass (packet + THIS FILE only — cold). Every human answer
should compound into a rule here: answered once, rule added, same class never
asked again. Rules are single sentences under a section, as `- ` bullets.
"""


def _template() -> str:
    parts = [TEMPLATE_HEADER]
    for section in SECTIONS:
        parts.append(f"\n## {section}\n\n_{_SECTION_INTROS[section]}_\n")
    return "".join(parts)


def approx_tokens(text: str) -> int:
    """Approximate token count: len(text) // 4 (chars-per-token heuristic)."""
    return len(text) // 4


def new_proposal_id(now: datetime | None = None) -> str:
    """Sortable unique proposal id: rp-<UTC yyyymmdd-HHMMSS>-<4 hex>."""
    now = now or datetime.now(timezone.utc)
    return f"rp-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Rulebook:
    """Rulebook file + proposals ledger under the attention home."""

    def __init__(self, home: str | Path | None = None, token_cap: int = DEFAULT_TOKEN_CAP):
        self.home = Path(home).expanduser() if home is not None else default_home()
        self.path = self.home / RULEBOOK_FILENAME
        self.proposals_path = self.home / PROPOSALS_FILENAME
        self.token_cap = token_cap

    # -- rulebook file ---------------------------------------------------------

    def ensure(self) -> Path:
        """Create the rulebook from the template on first use. Returns its path."""
        if not self.path.exists():
            _write_atomic(self.path, _template())
        return self.path

    def read(self) -> tuple[str, int]:
        """Return (content, approximate token count). Creates from template if absent."""
        self.ensure()
        content = self.path.read_text(encoding="utf-8")
        return content, approx_tokens(content)

    def append_rule(self, section: str, sentence: str) -> None:
        """Append ``- <sentence>`` to a section. Refuses loud over the token cap.

        Raises ValueError if the section is unknown, the sentence is empty, or
        the resulting rulebook would exceed the token cap (consolidate first —
        3+ citations of one rule usually means one badly written rule; rewrite
        the sentence instead of adding more).
        """
        if section not in SECTIONS:
            raise ValueError(f"unknown rulebook section {section!r}; expected one of {list(SECTIONS)}")
        sentence = sentence.strip()
        if not sentence:
            raise ValueError("rule sentence must be non-empty")

        content, _ = self.read()
        new_content = self._insert_into_section(content, section, f"- {sentence}")
        new_tokens = approx_tokens(new_content)
        if new_tokens > self.token_cap:
            raise ValueError(
                f"rulebook would be ~{new_tokens} tokens; cap is {self.token_cap}. "
                f"REFUSING the append — consolidate first: rules cited in 3+ packets are one "
                f"badly written rule (rewrite the sentence), and stale edge cases should be "
                f"folded into the rules above them. Edit {self.path} directly, then retry."
            )
        _write_atomic(self.path, new_content)

    @staticmethod
    def _insert_into_section(content: str, section: str, bullet: str) -> str:
        """Insert a bullet line at the end of ``## <section>`` (before the next ##)."""
        lines = content.splitlines()
        heading = f"## {section}"
        start = next((i for i, line in enumerate(lines) if line.strip() == heading), None)
        if start is None:
            raise ValueError(f"rulebook at hand has no '## {section}' heading — file was edited out of shape")
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].startswith("## "):
                end = i
                break
        # Trim trailing blank lines inside the section, append bullet + blank.
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        new_lines = lines[:insert_at] + [bullet] + [""] + lines[insert_at:end] + lines[end:]
        # lines[insert_at:end] is only trailing blanks; drop them to avoid growth.
        new_lines = lines[:insert_at] + [bullet, ""] + lines[end:]
        return "\n".join(new_lines) + ("\n" if content.endswith("\n") else "")

    # -- proposals -------------------------------------------------------------

    def _read_proposals(self) -> list[dict[str, Any]]:
        if not self.proposals_path.exists():
            return []
        records: list[dict[str, Any]] = []
        for i, line in enumerate(self.proposals_path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"malformed proposals line {i} in {self.proposals_path}: {e}") from e
        return records

    def _write_proposals(self, records: list[dict[str, Any]]) -> None:
        text = "".join(json.dumps(r, sort_keys=False) + "\n" for r in records)
        _write_atomic(self.proposals_path, text)

    def list_proposals(self) -> list[dict[str, Any]]:
        return self._read_proposals()

    def proposal_packet_ids(self) -> set[str]:
        """Packet ids that already have ANY proposal record (incl. status 'none').

        This is the triage runner's idempotency key: a packet with a record here
        is never re-proposed.
        """
        return {r["packet_id"] for r in self._read_proposals() if "packet_id" in r}

    def _require_new_packet(self, packet_id: str) -> None:
        if packet_id in self.proposal_packet_ids():
            raise ValueError(f"packet {packet_id!r} already has a rulebook proposal record — never double-propose")

    def append_proposal(self, packet_id: str, section: str, sentence: str, reason: str) -> dict[str, Any]:
        """Record a proposed rule delta for a packet (status: proposed)."""
        if section not in SECTIONS:
            raise ValueError(f"unknown rulebook section {section!r}; expected one of {list(SECTIONS)}")
        if not sentence.strip():
            raise ValueError("proposal sentence must be non-empty")
        self._require_new_packet(packet_id)
        record = {
            "id": new_proposal_id(),
            "packet_id": packet_id,
            "section": section,
            "sentence": sentence.strip(),
            "reason": reason,
            "status": "proposed",
            "created_at": utc_now_iso(),
        }
        records = self._read_proposals()
        records.append(record)
        self._write_proposals(records)
        return record

    def record_none(self, packet_id: str, reason: str) -> dict[str, Any]:
        """Record an explicit 'no rule delta — genuinely one-off' outcome.

        The design requires even 'none' to be logged as such; recording it also
        makes the rule_delta pass idempotent for this packet.
        """
        self._require_new_packet(packet_id)
        record = {
            "id": new_proposal_id(),
            "packet_id": packet_id,
            "status": "none",
            "reason": reason,
            "created_at": utc_now_iso(),
        }
        records = self._read_proposals()
        records.append(record)
        self._write_proposals(records)
        return record

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        for record in self._read_proposals():
            if record.get("id") == proposal_id:
                return record
        raise KeyError(f"proposal {proposal_id!r} not found in {self.proposals_path}")

    def apply(self, proposal_id: str) -> dict[str, Any]:
        """Apply a proposed rule: append to the rulebook (cap-checked), mark applied.

        The append happens FIRST; if it refuses (cap), the proposal stays
        'proposed' — nothing is half-applied.
        """
        records = self._read_proposals()
        record = next((r for r in records if r.get("id") == proposal_id), None)
        if record is None:
            raise KeyError(f"proposal {proposal_id!r} not found in {self.proposals_path}")
        if record.get("status") != "proposed":
            raise ValueError(f"proposal {proposal_id!r} has status {record.get('status')!r}; only 'proposed' applies")
        self.append_rule(record["section"], record["sentence"])
        record["status"] = "applied"
        record["applied_at"] = utc_now_iso()
        self._write_proposals(records)
        return record

    def reject(self, proposal_id: str, reason: str) -> dict[str, Any]:
        """Reject a proposed rule with a reason (kept visible for calibration)."""
        if not reason.strip():
            raise ValueError("a rejection requires a reason — it is calibration data")
        records = self._read_proposals()
        record = next((r for r in records if r.get("id") == proposal_id), None)
        if record is None:
            raise KeyError(f"proposal {proposal_id!r} not found in {self.proposals_path}")
        if record.get("status") != "proposed":
            raise ValueError(f"proposal {proposal_id!r} has status {record.get('status')!r}; only 'proposed' rejects")
        record["status"] = "rejected"
        record["reject_reason"] = reason
        record["rejected_at"] = utc_now_iso()
        self._write_proposals(records)
        return record
