"""The supervisor tick loop — design §Tier 1, "the clock" (mechanical only).

Each tick (default 2s):

0. (once, at startup) Acquire the single-instance flock on
   ``<home>/supervisor.lock`` — at most ONE supervise loop per home, ever.
   A second supervisor fails loud instead of silently duplicating events.
1. Re-scan the packet queue (pending/ + answered/) and diff against
   the seen-sets → emit ``packet:created`` / ``packet:answered`` events,
   enqueue created packets for notification batching, write ledger entries.
   (``auto/`` holds Phase-2 REVIEW RECORDS, not packets — the canonical copy
   of an auto-answered packet lives in ``answered/``, so scanning answered/
   already covers it; see context/packet-schema.md.)
2. Observe workers (tmux liveness + log sentinel) → emit ``worker:started``
   (once) / ``worker:finished`` (sentinel or dead session). When the worker's
   meta carries a ``judge_cmd`` (design §The Judge Requirement, step 4), the
   judge runs on finish and gates loop closure: exit 0 → ``loop:closed``;
   nonzero / timeout / spawn failure → ``loop:failed`` (loud). A configured
   judge is NEVER silently skipped — any inability to run it IS loop:failed.
3. Flush notification batches per policy (window / max / retry-on-failure).
4. Persist state atomically.

Step-2 scope deviation from the design's build-order wording — DELIBERATE,
for honesty:

(a) NO embedded-foundation LLM work. The design's build order mentions
    "embed foundation" at step 2, but the manager's own LLM work (triage,
    rulebook) is step 3; this supervisor is a plain Python loop and adding
    foundation embedding now would be ceremony around mechanics.

(Resolved former deviation (b): ``loop:closed`` was deferred at step 2
because emitting it without a judge would have been a fake finish line (D7).
As of step 4 it EXISTS and is judge-gated: ``worker:finished`` carries
``judged: true`` + ``judge_result: "closed"|"failed"`` when a judge ran, and
``judged: false`` unchanged when the worker shipped no judge.)
"""

from __future__ import annotations

import fcntl
import os
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from . import bells as bells_mod
from . import workers as workers_mod
from .judge import DEFAULT_JUDGE_TIMEOUT_S, JudgeResult, run_judge
from .notify import NotificationBatcher, parse_sink
from .packet import Packet
from .queue import PacketQueue
from .recipe_gates import RecipeGatePoller
from .state import SupervisorState
from .triage import TriageRunner
from .workers import Observation

DEFAULT_INTERVAL_S = 2.0
DEFAULT_TRIAGE_EVERY_TICKS = 15
DEFAULT_RECIPES_EVERY_TICKS = 5
LOCK_FILENAME = "supervisor.lock"


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)  # 3.11+ accepts the 'Z' suffix natively
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
        triage_every: int | None = None,
        triage_runner: TriageRunner | None = None,
        recipes_every: int | None = None,
        recipe_poller: RecipeGatePoller | None = None,
        judge_timeout_s: float = DEFAULT_JUDGE_TIMEOUT_S,
        bells: bool = True,
        ring: Callable[[str], None] | None = None,
        err: TextIO | None = None,
    ):
        self.state = SupervisorState(home)
        self.state.load()
        self.queue = queue or PacketQueue()
        self.interval_s = interval_s
        self.judge_timeout_s = judge_timeout_s
        self._list_sessions = list_sessions or workers_mod.list_am_sessions
        self._observe = observe or workers_mod.observe
        # Tier-3 muxplex bells: ON by default (the supervise path already
        # requires tmux, so "when tmux is present" holds by construction);
        # `supervise --no-bells` disables. `ring` is an injectable seam like
        # list_sessions/observe so the join/idempotency logic is unit-testable
        # without tmux (real ringing is covered by test_bells.py + the smoke).
        self.bells = bells
        self._ring = ring or bells_mod.ring_bell
        self._bell_error_sessions: set[str] = set()
        self._err = err or sys.stderr
        self._stop = False
        self._warned_no_sink = False
        self._reported_bad_files: set[str] = set()
        self._lock_fd: int | None = None
        # Triage integration (design step 3, Phase 1 recommend-only). OFF by
        # default: triage_every = run one pass every N ticks when set.
        self.triage_every = triage_every
        self.triage_runner = triage_runner
        if self.triage_every is not None and self.triage_runner is None:
            self.triage_runner = TriageRunner(queue=self.queue, state=self.state, err=self._err)
        # Recipe-gate poller (design producer #4, D9). OFF by default:
        # recipes_every = run one poll every N ticks when set.
        self.recipes_every = recipes_every
        self.recipe_poller = recipe_poller
        if self.recipes_every is not None and self.recipe_poller is None:
            self.recipe_poller = RecipeGatePoller(queue=self.queue, state=self.state, err=self._err)
        self._tick_count = 0

        self.batcher: NotificationBatcher | None = None
        if notify_spec:
            kwargs: dict[str, Any] = {}
            if batch_window_s is not None:
                kwargs["batch_window_s"] = batch_window_s
            if batch_max is not None:
                kwargs["batch_max"] = batch_max
            self.batcher = NotificationBatcher(sink=parse_sink(notify_spec), **kwargs)

        if not self.state.loaded_from_snapshot:
            # D5 rebuild path: no snapshot — reseed seen-sets from the queue
            # dirs. auto/ holds review records (not packets) since Phase 2;
            # every auto-answered packet's canonical copy is in answered/.
            pending = [p.id for p in self._scan_subdir("pending")]
            answered = [p.id for p in self._scan_subdir("answered")]
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
        answered = self._scan_subdir("answered")  # auto-answered packets land here too (canonical)

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
            if self.bells and pkt.source.session_id:
                # Bell join is deferred to _ring_bells (after _observe_workers)
                # because the worker's amplifier session id may appear in its
                # log on this tick — or a LATER one (late binding). Packets
                # without a source session_id (recipe gates, seeded packets,
                # standalone workunits) never become candidates: no bell, no
                # error, no event spam.
                self.state.ring_candidates[pkt.id] = pkt.source.session_id
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
            # An answered packet no longer needs a bell — retire any unrung
            # candidate (the human already reached it without the surface).
            self.state.ring_candidates.pop(pkt.id, None)
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

                # Judge-gated finish lines (design §The Judge Requirement).
                # A worker with a judge_cmd is judged on finish — ALWAYS. Any
                # inability to run the judge is loop:failed, never a skip.
                judge_cmd = record.get("judge_cmd")
                judge_result: JudgeResult | None = None
                if judge_cmd:
                    judge_result = self._run_worker_judge(session, judge_cmd, obs.exit_code)

                fields: dict[str, Any] = {
                    "session": session,
                    "name": record.get("name"),
                    "exit_code": obs.exit_code,
                    "judged": judge_result is not None,
                }
                if judge_result is not None:
                    fields["judge_result"] = "closed" if judge_result.passed else "failed"
                if not obs.sentinel_seen:
                    fields["sentinel_missing"] = True  # dead session, no exit line — loud
                self.state.append_event("worker:finished", **fields)
                self.state.ledger_append(
                    "worker_finished",
                    session=session,
                    exit_code=obs.exit_code,
                    judged=judge_result is not None,
                    judge_result=fields.get("judge_result"),
                    sentinel_missing=not obs.sentinel_seen,
                )
                if judge_result is not None:
                    self._report_judge_outcome(session, record, obs.exit_code, judge_result)

    # -- judge-gated finish lines (design §The Judge Requirement, step 4) -----------

    def _run_worker_judge(self, session: str, judge_cmd: str, worker_exit: int | None) -> JudgeResult:
        """Run the worker's judge and persist its output to workers/<session>/judge.log.

        cwd = the worker's dir; env carries ATTENTION_HOME, ATTENTION_QUEUE_DIR,
        WORKER_LOG (abs path) and WORKER_EXIT (empty string when the session
        died without a sentinel). Timeout / spawn failure come back as a
        failed JudgeResult — never an exception, never a skip.
        """
        worker_dir = self.state.worker_dir(session)
        worker_dir.mkdir(parents=True, exist_ok=True)  # cwd must exist even for adopted workers
        result = run_judge(
            judge_cmd,
            cwd=worker_dir,
            home=self.state.home,
            queue_root=self.queue.root,
            worker_log=self.state.worker_log_path(session).resolve(),
            worker_exit=worker_exit,
            timeout_s=self.judge_timeout_s,
        )
        log_text = result.output
        if result.reason:
            log_text = (log_text + "\n" if log_text and not log_text.endswith("\n") else log_text) + (
                f"[attention-manager] {result.reason}\n"
            )
        (worker_dir / "judge.log").write_text(log_text, encoding="utf-8")
        return result

    def _report_judge_outcome(
        self, session: str, record: dict[str, Any], worker_exit: int | None, result: JudgeResult
    ) -> None:
        """Emit loop:closed / loop:failed + ledger + notification for a judged worker."""
        name = record.get("name")
        if result.passed:
            self.state.append_event(
                "loop:closed", session=session, name=name, worker_exit=worker_exit, judge_output=result.output_tail
            )
            self.state.ledger_append(
                "loop_closed", session=session, name=name, worker_exit=worker_exit, judge_output=result.output_tail
            )
            if self.batcher is not None:
                self.batcher.enqueue(session, f"loop closed: {name} (worker exit {worker_exit})", kind="finish_line")
            else:
                self._warn_notifications_disabled()
            return

        # Loud failure: nonzero exit, timeout, or spawn failure — a configured
        # judge that could not deliver a pass is ALWAYS loop:failed (D7).
        self.state.append_event(
            "loop:failed",
            session=session,
            name=name,
            worker_exit=worker_exit,
            reason=result.reason,
            judge_output=result.output_tail,
        )
        self.state.ledger_append(
            "loop_failed",
            session=session,
            name=name,
            worker_exit=worker_exit,
            reason=result.reason,
            judge_output=result.output_tail,
        )
        print(
            f"ERROR: loop:failed for {session}: {result.reason}"
            + (f" — judge output tail: {result.output_tail}" if result.output_tail else ""),
            file=self._err,
        )
        if self.batcher is not None:
            self.batcher.enqueue(session, f"LOOP FAILED: {name} — {result.reason}", kind="finish_line_failed")
        else:
            self._warn_notifications_disabled()
        # A failed loop needs the human — surface it on the muxplex bell. Rings
        # at most once per loop_failed by construction: loop:failed itself is
        # emitted exactly once per worker (the persisted `finished` flag gates
        # re-observation, across restarts too).
        if self.bells:
            self._try_ring(session, trigger="loop_failed")

    # -- bells (Tier-3 muxplex surface: needs-attention = tmux bell) -----------------

    def _try_ring(self, session: str, trigger: str, packet_id: str | None = None) -> bool:
        """Ring one session's bell; emit bell:rung on success.

        Failure policy (never crash the loop, don't retry-spam): ONE loud
        bell:error event + stderr line per session, then quiet for that
        session. Returns True iff the bell actually rang.
        """
        fields: dict[str, Any] = {"session": session, "trigger": trigger}
        if packet_id is not None:
            fields["packet_id"] = packet_id
        try:
            self._ring(session)
        except Exception as e:  # noqa: BLE001 — boundary: bell trouble must not kill supervision
            if session not in self._bell_error_sessions:
                self._bell_error_sessions.add(session)
                self.state.append_event("bell:error", error=str(e), **fields)
                print(f"ERROR: bell ring failed for {session}: {e}", file=self._err)
            return False
        self.state.append_event("bell:rung", **fields)
        return True

    def _ring_bells(self) -> None:
        """Ring worker bells for created packets (runs AFTER _observe_workers).

        Join: packet.source.session_id ↔ the amplifier session id observe()
        extracted from the worker's log. The id may appear in worker.log
        AFTER the packet is created (late binding), so unjoined candidates
        are kept and retried every tick until rung or answered. Candidates
        and the rung set live in state.json (D5): a restart neither re-rings
        nor drops a not-yet-joined packet.

        No matching worker (recipe gates, standalone workunits, seeded
        packets, id never observed): the candidate just waits — no bell, no
        error, no event spam. A failed ring retires the candidate (one loud
        bell:error per session — see _try_ring — never per-tick retries).
        """
        if not self.bells or not self.state.ring_candidates:
            return
        by_amplifier_id: dict[str, str] = {
            record["amplifier_session_id"]: session
            for session, record in self.state.workers.items()
            if record.get("amplifier_session_id")
        }
        for packet_id in sorted(self.state.ring_candidates):
            if packet_id in self.state.rung_packets:  # already rung (e.g. pre-restart)
                del self.state.ring_candidates[packet_id]
                continue
            session = by_amplifier_id.get(self.state.ring_candidates[packet_id])
            if session is None:
                continue  # late binding: no worker claims this id yet; retry next tick
            del self.state.ring_candidates[packet_id]
            if self._try_ring(session, trigger="packet", packet_id=packet_id):
                self.state.rung_packets.add(packet_id)

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

    # -- single-instance lock ---------------------------------------------------------

    def _acquire_instance_lock(self) -> None:
        """Enforce the single-writer invariant (state.py): at most ONE supervise
        loop per home, ever.

        Rationale (S4 DTU incident): a supervisor that survives a botched kill
        plus a restarted one both tick against the same home — each emits its
        own ``packet:answered``/``worker:finished`` events and both write
        state.json, silently corrupting the record. flock is kernel-owned and
        dies with the process (even SIGKILL), so the D5 kill/restart path needs
        no stale-lock handling, while a *surviving* supervisor keeps the lock
        held — the second instance must fail loud, never double-write.
        """
        self.state.home.mkdir(parents=True, exist_ok=True)
        lock_path = self.state.home / LOCK_FILENAME
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            holder = ""
            try:
                holder = os.read(fd, 64).decode("utf-8", "replace").strip()
            except OSError:
                pass
            os.close(fd)
            raise RuntimeError(
                f"another supervisor is already running for {self.state.home}"
                + (f" (lock {lock_path} held by pid {holder})" if holder else f" (lock {lock_path} held)")
                + ". Two supervisors against one home would duplicate events and corrupt "
                "state.json (single-writer invariant) — stop the other process first."
            ) from e
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._lock_fd = fd

    def _release_instance_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_fd)
            self._lock_fd = None

    # -- triage (design step 3, Phase 1 recommend-only) --------------------------------

    def _maybe_triage(self) -> None:
        """Run one triage pass every ``triage_every`` ticks (first tick counts).

        Per-packet failures are handled (loudly) inside the runner; anything
        that escapes here is a system failure — reported loud, loop keeps
        running (non-interference: triage trouble must not kill supervision).
        """
        if self.triage_every is None or self.triage_runner is None:
            return
        if self._tick_count % self.triage_every != 0:
            return
        try:
            self.triage_runner.triage_pass()
        except Exception as e:  # noqa: BLE001 — boundary: keep the loop alive, loudly
            self.state.append_event("triage:error", error=f"triage pass crashed: {e}")
            print(f"ERROR: triage pass crashed: {e}", file=self._err)

    # -- recipe-gate polling (design producer #4, D9) -----------------------------------

    def _maybe_recipes(self) -> None:
        """Run one recipe-gate poll every ``recipes_every`` ticks (first tick counts).

        Per-gate failures are handled (loudly) inside the poller; anything
        that escapes here is a system failure — reported loud, loop keeps
        running (non-interference, same discipline as triage).
        """
        if self.recipes_every is None or self.recipe_poller is None:
            return
        if self._tick_count % self.recipes_every != 0:
            return
        try:
            self.recipe_poller.poll_once()
        except Exception as e:  # noqa: BLE001 — boundary: keep the loop alive, loudly
            self.state.append_event("recipe_gates:error", error=f"recipe-gate poll crashed: {e}")
            print(f"ERROR: recipe-gate poll crashed: {e}", file=self._err)

    # -- the loop --------------------------------------------------------------------

    def tick(self) -> None:
        self._scan_packets()
        self._observe_workers()
        self._ring_bells()  # after observation: the packet↔worker join needs fresh session ids
        self._maybe_triage()
        self._maybe_recipes()
        self._tick_count += 1
        self._flush_notifications()
        self.state.save()

    def _handle_signal(self, signum: int, frame: Any) -> None:
        self._stop = True

    def run(self, once: bool = False) -> int:
        """Run the loop until SIGINT/SIGTERM (or a single tick with once=True)."""
        # Lock FIRST — before tmux checks and before ANY event/state write. A
        # second supervisor must leave zero traces in the home it doesn't own.
        self._acquire_instance_lock()
        try:
            workers_mod.require_tmux()  # fail loud upfront — no tmux, no supervision
            if self.triage_runner is not None and self.triage_every is not None:
                self.triage_runner.preflight()  # fail loud upfront — no amplifier bin, no triage
            if self.recipe_poller is not None and self.recipes_every is not None:
                self.recipe_poller.preflight()  # fail loud upfront — no amplifier bin, no gate polling
            if self.batcher is None:
                self._warn_notifications_disabled()
            self.state.append_event(
                "supervisor:started",
                interval_s=self.interval_s,
                once=once,
                queue_root=str(self.queue.root),
                notify=self.batcher.sink.name if self.batcher is not None else None,
                triage_every=self.triage_every,
                recipes_every=self.recipes_every,
                bells=self.bells,
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
        finally:
            self._release_instance_lock()
