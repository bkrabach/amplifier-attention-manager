"""Supervisor state — disk-backed and rebuildable (design decision D5).

Home resolution: ``$ATTENTION_HOME`` if set, else ``~/.amplifier/attention/``.

Layout under the home:

    state.json            supervisor snapshot (seen-sets + worker flags), atomic writes
    events.jsonl          supervisor's own append-only event log (one JSON object/line)
    ledger/<date>.jsonl   daily ledger (one JSON object per line)
    workers/<session>/    per-worker dir: worker.log + meta.json

Rebuildability contract (D5 — "kill at 60%, restart, resume at 60%"):

* ``state.json`` is a *cache of tracking flags*, not the source of truth.
* Workers are re-adopted on startup from ``workers/`` dirs plus ``tmux ls``
  (``am-*`` prefix) — see :meth:`SupervisorState.adopt_workers`.
* Packet seen-sets: if ``state.json`` exists it is authoritative (no duplicate
  events after restart). If it is missing entirely, the sets are rebuilt from
  the queue dirs (:meth:`rebuild_seen_from_queue`): packets already in
  ``answered/``/``auto/`` are history and are pre-marked seen; packets still in
  ``pending/`` are deliberately NOT pre-marked — they still need attention, so
  re-announcing them is the honest behavior (re-announce beats silent drop).

Concurrency note: ``state.json`` is written ONLY by the supervise loop. The
``dispatch`` CLI command never writes it — it writes ``workers/<s>/meta.json``
plus append-only event/ledger lines, and the supervisor adopts the new worker
on its next tick. This keeps two processes from racing on the snapshot.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from .packet import utc_now_iso

DEFAULT_HOME = "~/.amplifier/attention"
ENV_HOME = "ATTENTION_HOME"

STATE_SCHEMA_VERSION = 1
SESSION_PREFIX = "am-"


def default_home() -> Path:
    """Resolve the supervisor home: $ATTENTION_HOME else ~/.amplifier/attention."""
    return Path(os.environ.get(ENV_HOME) or DEFAULT_HOME).expanduser()


def utc_today() -> str:
    """Today's date (UTC) as YYYY-MM-DD — the ledger file key."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _write_atomic(path: Path, text: str) -> None:
    """Same tmp+os.replace pattern as queue.py — never a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON object as a single line (O_APPEND; safe for small lines)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file, failing loud on malformed lines (they are ours)."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"malformed JSONL line {i} in {path}: {e}") from e
    return records


def new_worker_record(
    name: str,
    session: str,
    cmd: str | None = None,
    task: str | None = None,
    started_at: str | None = None,
    adopted_without_meta: bool = False,
) -> dict[str, Any]:
    """Canonical in-state record for one worker."""
    return {
        "name": name,
        "session": session,
        "cmd": cmd,
        "task": task,
        "started_at": started_at,
        "adopted_without_meta": adopted_without_meta,
        "started_event_emitted": False,
        "finished": False,
        "exit_code": None,
        "amplifier_session_id": None,
    }


class SupervisorState:
    """Disk-backed supervisor state. All paths live under the resolved home."""

    def __init__(self, home: str | Path | None = None):
        self.home = Path(home).expanduser() if home is not None else default_home()
        self.state_path = self.home / "state.json"
        self.events_path = self.home / "events.jsonl"
        self.ledger_dir = self.home / "ledger"
        self.workers_dir = self.home / "workers"

        self.seen_pending: set[str] = set()
        self.seen_answered: set[str] = set()
        self.workers: dict[str, dict[str, Any]] = {}  # keyed by tmux session name (am-*)
        self.loaded_from_snapshot: bool = False

    # -- worker paths ---------------------------------------------------------

    def worker_dir(self, session: str) -> Path:
        return self.workers_dir / session

    def worker_log_path(self, session: str) -> Path:
        return self.worker_dir(session) / "worker.log"

    def worker_meta_path(self, session: str) -> Path:
        return self.worker_dir(session) / "meta.json"

    # -- snapshot (state.json) ------------------------------------------------

    def load(self) -> None:
        """Load state.json if present; otherwise start empty (rebuild path).

        Malformed state.json fails loud with a recovery hint — it is our own
        atomically-written file, so corruption means something is genuinely
        wrong and silently rebuilding would hide it.
        """
        if not self.state_path.exists():
            self.loaded_from_snapshot = False
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"corrupt state file {self.state_path}: {e}. Delete it to rebuild tracking from disk (D5)."
            ) from e
        self.seen_pending = set(data.get("seen_pending", []))
        self.seen_answered = set(data.get("seen_answered", []))
        self.workers = dict(data.get("workers", {}))
        self.loaded_from_snapshot = True

    def save(self) -> None:
        """Atomically persist the snapshot (tmp + os.replace, as in queue.py)."""
        self.home.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": STATE_SCHEMA_VERSION,
            "saved_at": utc_now_iso(),
            "seen_pending": sorted(self.seen_pending),
            "seen_answered": sorted(self.seen_answered),
            "workers": self.workers,
        }
        _write_atomic(self.state_path, json.dumps(data, indent=2) + "\n")

    # -- events.jsonl -----------------------------------------------------------

    def append_event(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one event line: {ts, event, ...fields}. Returns the record."""
        self.home.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {"ts": utc_now_iso(), "event": event, **fields}
        _append_jsonl(self.events_path, record)
        return record

    def read_events(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.events_path)

    # -- ledger ------------------------------------------------------------------

    def ledger_path(self, date: str | None = None) -> Path:
        return self.ledger_dir / f"{date or utc_today()}.jsonl"

    def ledger_append(self, kind: str, **details: Any) -> dict[str, Any]:
        """Append one ledger entry: {ts, kind, ...details} to today's file."""
        record: dict[str, Any] = {"ts": utc_now_iso(), "kind": kind, **details}
        _append_jsonl(self.ledger_path(), record)
        return record

    def ledger_read(self, date: str | None = None) -> list[dict[str, Any]]:
        return _read_jsonl(self.ledger_path(date))

    # -- rebuild from disk (D5) ---------------------------------------------------

    def rebuild_seen_from_queue(self, pending_ids: Iterable[str], answered_ids: Iterable[str]) -> dict[str, int]:
        """Rebuild seen-sets from a queue scan when no snapshot exists.

        answered/auto packets are closed history — pre-mark seen so we never
        replay ``packet:answered`` for them. pending packets are NOT pre-marked:
        they still need attention, so announcing them after a total state loss
        is the honest behavior (re-announce beats silent drop).
        """
        answered = set(answered_ids)
        self.seen_answered |= answered
        self.seen_pending |= answered  # an answered packet is past pending
        return {"pending_unseen": len(set(pending_ids) - self.seen_pending), "answered_seeded": len(answered)}

    def adopt_workers(self, tmux_sessions: Iterable[str]) -> list[str]:
        """Merge workers from workers/ dirs + live am-* tmux sessions into state.

        workers/ meta.json is the metadata source of truth; tmux tells liveness.
        Already-tracked sessions keep their flags (started/finished/exit_code).
        Returns the list of newly adopted session names.
        """
        added: list[str] = []

        if self.workers_dir.exists():
            for meta_path in sorted(self.workers_dir.glob(f"{SESSION_PREFIX}*/meta.json")):
                session = meta_path.parent.name
                if session in self.workers:
                    continue
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    # Loud degraded: a broken meta.json must not kill supervision.
                    self.append_event("state:error", error=f"malformed {meta_path}: {e}")
                    continue
                self.workers[session] = new_worker_record(
                    name=meta.get("name") or session.removeprefix(SESSION_PREFIX),
                    session=session,
                    cmd=meta.get("cmd"),
                    task=meta.get("task"),
                    started_at=meta.get("started_at"),
                )
                added.append(session)

        for session in tmux_sessions:
            if not session.startswith(SESSION_PREFIX) or session in self.workers:
                continue
            # am-* session with no workers/ dir: not launched by us; adopt it
            # anyway (the am-* prefix is the design's ownership convention).
            self.workers[session] = new_worker_record(
                name=session.removeprefix(SESSION_PREFIX),
                session=session,
                adopted_without_meta=True,
            )
            added.append(session)

        return added
