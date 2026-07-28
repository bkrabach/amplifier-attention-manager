"""Auto-answer review log — ``queue/auto/`` (Phase 2 calibration surface).

Since Phase 2 (build step 6), ``queue/auto/`` holds REVIEW RECORDS, not
packets. An auto-answered packet itself lives in ``answered/`` — that is the
canonical copy producers poll to unblock. The auto/ record exists so the human
can review every auto-answer at their convenience (design §Triage Phase 2:
"rejected/auto-handled items stay visible for calibration").

Record format (documented in context/packet-schema.md):

    {
      "packet_id": "pkt-...",
      "answer": "B",
      "why": "<triage why>",
      "rule_refs": ["..."],          # verdict rule_refs, verbatim
      "sections": ["Auto-answer rules"],  # resolved phase-2 sections
      "auto_at": "2026-07-28T06:00:00Z",
      "reviewed": false,
      // after review:
      "review": {"action": "confirmed" | "rejected",
                 "correct_option": "A",   # rejected only
                 "reason": "...",         # rejected only
                 "reviewed_at": "..."}
    }

One record per file (``auto/<packet_id>.json``), atomic writes, rebuilt from
the filesystem on every scan — same discipline as the packet queue (D5).

HONESTY NOTE: reviewing a record is calibration only. The producing worker
already unblocked on the auto answer the moment ``answered/<id>.json``
appeared — a rejection cannot un-answer it. Rejection demotes the cited
sections' trust so the same class stops being auto-answered.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .packet import utc_now_iso
from .queue import default_queue_root


class AutoLog:
    """File-backed auto-answer review records under ``<queue root>/auto/``."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser() if root is not None else default_queue_root()

    def dir(self) -> Path:
        path = self.root / "auto"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path_for(self, packet_id: str) -> Path:
        return self.dir() / f"{packet_id}.json"

    # -- write -----------------------------------------------------------------

    def _write_atomic(self, path: Path, record: dict[str, Any]) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def append_record(
        self,
        packet_id: str,
        answer: str,
        why: str,
        rule_refs: list[str],
        sections: list[str],
    ) -> dict[str, Any]:
        """Create the unreviewed record for a fresh auto-answer."""
        path = self.path_for(packet_id)
        if path.exists():
            raise ValueError(f"auto record for {packet_id!r} already exists at {path} — never double-record")
        record: dict[str, Any] = {
            "packet_id": packet_id,
            "answer": answer,
            "why": why,
            "rule_refs": list(rule_refs),
            "sections": list(sections),
            "auto_at": utc_now_iso(),
            "reviewed": False,
        }
        self._write_atomic(path, record)
        return record

    # -- read ------------------------------------------------------------------

    def _load(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"malformed auto record {path}: {e}") from e
        if not isinstance(data, dict) or not data.get("packet_id"):
            raise ValueError(f"malformed auto record {path}: not an object with a packet_id")
        return data

    def get(self, packet_id: str) -> dict[str, Any]:
        path = self.path_for(packet_id)
        if not path.exists():
            raise KeyError(f"no auto record for {packet_id!r} in {self.dir()}")
        return self._load(path)

    def list_records(self, include_reviewed: bool = False) -> list[dict[str, Any]]:
        """All records sorted by packet id (ids are time-sortable)."""
        records = [self._load(p) for p in sorted(self.dir().glob("pkt-*.json"))]
        if not include_reviewed:
            records = [r for r in records if not r.get("reviewed")]
        return sorted(records, key=lambda r: r["packet_id"])

    # -- review ----------------------------------------------------------------

    def _require_unreviewed(self, packet_id: str) -> dict[str, Any]:
        record = self.get(packet_id)
        if record.get("reviewed"):
            raise ValueError(
                f"auto record {packet_id!r} was already reviewed "
                f"({record.get('review', {}).get('action', '?')}) — a review is recorded once"
            )
        return record

    def mark_confirmed(self, packet_id: str) -> dict[str, Any]:
        """Human confirms the auto-answer was right (counts as a match)."""
        record = self._require_unreviewed(packet_id)
        record["reviewed"] = True
        record["review"] = {"action": "confirmed", "reviewed_at": utc_now_iso()}
        self._write_atomic(self.path_for(packet_id), record)
        return record

    def mark_rejected(self, packet_id: str, correct_option: str, reason: str) -> dict[str, Any]:
        """Human rejects the auto-answer, recording the correction.

        Calibration only — the worker already unblocked on the auto answer;
        this cannot un-answer the packet. The caller demotes trust.
        """
        if not reason.strip():
            raise ValueError("a rejection requires a reason — it is calibration data")
        record = self._require_unreviewed(packet_id)
        record["reviewed"] = True
        record["review"] = {
            "action": "rejected",
            "correct_option": correct_option,
            "reason": reason,
            "reviewed_at": utc_now_iso(),
        }
        self._write_atomic(self.path_for(packet_id), record)
        return record
