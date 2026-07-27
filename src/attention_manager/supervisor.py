"""The supervisor tick loop — design §Tier 1, "the clock" (mechanical only).

Each tick (default 2s):

1. Re-scan the packet queue (pending/ + answered/ + auto/) and diff against
   the seen-sets → emit ``packet:created`` / ``packet:answered`` events,
   enqueue created packets for notification batching, write ledger entries.
2. Observe workers (tmux liveness + log sentinel) → emit ``worker:started``
   (once) / ``worker:finished`` (sentinel or dead session).
3. Flush notification batches per policy (window / max / retry-on-failure).
4. Persist state atomically.

Step-2 scope deviations from the design's build-order wording — DELIBERATE,
both for honesty:

(a) NO embedded-foundation LLM work. The design's build order mentions
    "embed foundation" at step 2, but the manager's own LLM work (triage,
    rulebook) is step 3; this supervisor is a plain Python loop and adding
    foundation embedding now would be ceremony around mechanics.
(b) NO ``loop:closed`` events. Loop closure is judge-gated (design §The
    Judge Requirement, build step 4). Emitting ``loop:closed`` without a
    judge would be a fake finish line — exactly what the design's own
    fail-loud rule (D7) forbids. Instead the supervisor emits
    ``worker:finished`` with the exit code and an explicit
    ``"judged": false`` field. Step 4 adds the judge and ``loop:closed``.
"""

from __future__ import annotations

import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import TextIO

from . import workers as workers_mod
from .notify import NotificationBatcher
from .notify import parse_sink
from .packet import Packet
from .queue import PacketQueue
from .state import SupervisorState
from .workers import Observation

DEFAULT_INTERVAL_S = 2.0


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _latency_seconds(created_at: str, answered_at: str) -> float | None:
    created = _parse_iso(created_at)
    answered = _parse_iso(answered_at)
    if created is None or answered is None:
        return None
    return max(0.0, (answered - created).total_seconds())


class Supervisor:
    """Mechanical supervisor: queue diffing, worker observation, batched notify.

    ``list_sessions`` / ``observe`` are injectable seams (default: real tmux
    via the workers module) so the pure diff/batch logic is unit-testable
    without tmux. The real CLI paths always use tmux and fail loud without it.
    """

    def __init__(
        self,
        home: str | Path | None = None,
        queue: PacketQueue | None = None,
        notify_spec: str | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        batch_window_s: float | None = None,
        batch_max: int | None = None,
        list_sessions: Callable[[], list[str]] | None = None,
        observe: Callable[[str, Path], Observation] | None = None,
        err: TextIO | None = None,
    ):
        self.state = SupervisorState(home)
        self.state.load()
        self.queue = queue or PacketQueue()
        self.interval_s = interval_s
        self._list_sessions = list_sessions or workers_mod.list_am_sessions
        self._observe = observe or workers_mod.observe
        self._err = err or sys.stderr
        self._stop = False
        self._warned_no_sink = False
        self._reported_bad_files: set[str] = set()

        self.batcher: NotificationBatcher | None = None
        if notify_spec:
            kwargs: dict[str, Any] = {}
            if batch_window_s is not None:
                kwargs["batch_window_s"] = batch_window_s
            if batch_max is not None:
                kwargs["batch_max"] = batch_max
            self.batcher = NotificationBatcher(sink=parse_sink(notify_spec), **kwargs)

        if not self.state.loaded_from_snapshot:
            # D5 rebuild path: no snapshot — reseed seen-sets from the queue dirs.
            pending = [p.id for p in self._scan_subdir("pending")]
            answered = [p.id for p in self._scan_subdir("answered")] + [p.id for p in self._scan_subdir("auto")]
            counts = self.state.rebuild_seen_from_queue(pending, answered)
            self.state.append_event("state:rebuilt", **counts)

    # -- queue scanning ---------------------------------------------------------

    def _scan_subdir(self, subdir: str) -> list[Packet]:
        """Load all packets in a queue subdir; a malformed file is reported
        loudly (event + stderr, once per path) and skipped — one bad file must
        not halt supervision, but it is never silently ignored."""
        packets: list[Packet] = []
        for path in sorted(self.queue.dir(subdir).glob("pkt-*.json")):
            try:
                packets.append(Packet.from_json(path.read_text(encoding="utf-8")))
            except ValueError as e:
                if str(path) not in self._reported_bad_files:
                    self._reported_bad_files.add(str(path))
                    self.state.append_event("queue:error", path=str(path), error=str(e))
                    print(f"ERROR: skipping malformed packet file {path}: {e}", file=self._err)
        return sorted(packets, key=lambda p: p.id)

    def _scan_packets(self) -> None:
        pending = self._scan_subdir("pending")
        answered = self._scan_subdir("answered") + self._scan_subdir("auto")

        for pkt in pending:
            if pkt.id in self.state.seen_pending:
                continue
            self.state.seen_pending.add(pkt.id)
            self.state.append_event(
                "packet:created",
                packet_id=pkt.id,
                question=pkt.question,
                kind=pkt.source.kind,
                tier=pkt.urgency.tier,
                muxplex_session=pkt.source.muxplex_session,
            )
            self.state.ledger_append("packet_created", packet_id=pkt.id, question=pkt.question, tier=pkt.urgency.tier)
            if self.batcher is not None:
                self.batcher.enqueue(pkt.id, pkt.question)
            else:
                self._warn_notifications_disabled()

        for pkt in sorted(answered, key=lambda p: p.id):
            if pkt.id in self.state.seen_answered:
                continue
            self.state.seen_answered.add(pkt.id)
            # A packet first seen already-answered never fires packet:created —
            # it no longer needs attention. Mark it past-pending too.
            self.state.seen_pending.add(pkt.id)
            resolution = pkt.resolution.to_dict() if pkt.resolution is not None else None
            latency = (
                _latency_seconds(pkt.created_at, pkt.resolution.answered_at) if pkt.resolution is not None else None
            )
            self.state.append_event("packet:answered", packet_id=pkt.id, resolution=resolution, latency_s=latency)
            self.state.ledger_append("packet_answered", packet_id=pkt.id, resolution=resolution, latency_s=latency)

    # -- worker observation -------------------------------------------------------

    def _observe_workers(self) -> None:
        self.state.adopt_workers(self._list_sessions())
        for session, record in sorted(self.state.workers.items()):
            if record.get("finished"):
                continue
            obs = self._observe(session, self.state.worker_log_path(session))

            if not record.get("started_event_emitted"):
                record["started_event_emitted"] = True
                self.state.append_event(
                    "worker:started",
                    session=session,
                    name=record.get("name"),
                    task=record.get("task"),
                    adopted_without_meta=record.get("adopted_without_meta", False),
                )

            if obs.session_id and not record.get("amplifier_session_id"):
                record["amplifier_session_id"] = obs.session_id

            if obs.sentinel_seen or not obs.alive:
                record["finished"] = True
                record["exit_code"] = obs.exit_code
                # judged: false — honest finish reporting. The judge-gated
                # loop:closed event is step 4; faking it here would violate D7.
                fields: dict[str, Any] = {
                    "session": session,
                    "name": record.get("name"),
                    "exit_code": obs.exit_code,
                    "judged": False,
                }
                if not obs.sentinel_seen:
                    fields["sentinel_missing"] = True  # dead session, no exit line — loud
                self.state.append_event("worker:finished", **fields)
                self.state.ledger_append(
                    "worker_finished",
                    session=session,
                    exit_code=obs.exit_code,
                    judged=False,
                    sentinel_missing=not obs.sentinel_seen,
                )

    # -- notifications --------------------------------------------------------------

    def _warn_notifications_disabled(self) -> None:
        if self._warned_no_sink:
            return
        self._warned_no_sink = True
        message = (
            "notifications are DISABLED: no sink configured (--notify or $ATTENTION_NOTIFY). "
            "Packets will only be visible via 'attention-manager queue list' / status."
        )
        self.state.append_event("notify:disabled", message=message)
        print(f"WARNING: {message}", file=self._err)

    def _flush_notifications(self) -> None:
        if self.batcher is None:
            return
        outcome = self.batcher.flush_if_due()
        if outcome is None:
            return
        if outcome.delivered:
            self.state.append_event(
                "notify:batch_sent", count=outcome.count, packet_ids=outcome.packet_ids, sink=outcome.sink
            )
            self.state.ledger_append(
                "notified_batch", count=outcome.count, packet_ids=outcome.packet_ids, sink=outcome.sink
            )
        else:
            # Loud degraded operation: report and keep the items queued for the
            # next flush attempt. Never crash the loop, never drop silently.
            self.state.append_event(
                "notify:error", error=outcome.error, count=outcome.count, packet_ids=outcome.packet_ids
            )
            print(
                f"ERROR: notification delivery failed via {outcome.sink}: {outcome.error} "
                f"({outcome.count} item(s) kept queued for retry)",
                file=self._err,
            )

    # -- the loop --------------------------------------------------------------------

    def tick(self) -> None:
        self._scan_packets()
        self._observe_workers()
        self._flush_notifications()
        self.state.save()

    def _handle_signal(self, signum: int, frame: Any) -> None:  # noqa: ARG002
        self._stop = True

    def run(self, once: bool = False) -> int:
        """Run the loop until SIGINT/SIGTERM (or a single tick with once=True)."""
        workers_mod.require_tmux()  # fail loud upfront — no tmux, no supervision
        if self.batcher is None:
            self._warn_notifications_disabled()
        self.state.append_event(
            "supervisor:started",
            interval_s=self.interval_s,
            once=once,
            queue_root=str(self.queue.root),
            notify=self.batcher.sink.name if self.batcher is not None else None,
        )
        if once:
            self.tick()
            self.state.append_event("supervisor:stopped", reason="once")
            self.state.save()
            return 0

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        while not self._stop:
            self.tick()
            # Sleep in small slices so a signal stops us within ~0.2s.
            deadline = time.monotonic() + self.interval_s
            while not self._stop and time.monotonic() < deadline:
                time.sleep(0.2)
        self.state.append_event("supervisor:stopped", reason="signal")
        self.state.save()  # clean flush on shutdown
        return 0
