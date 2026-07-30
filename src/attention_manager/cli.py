"""attention-manager CLI — queue, dispatch, supervision, triage, and rulebook.

Commands:
    attention-manager queue list          # pending packets (table or --json)
    attention-manager queue show <id>     # full packet
    attention-manager queue path          # queue root path
    attention-manager answer <id> <option> [--rationale TEXT]
    attention-manager dispatch <name> --task TEXT [--bundle URI] [--worker-cmd CMD]
                               [--judge CMD]
    attention-manager supervise [--interval N] [--once] [--notify SINK]
                                [--batch-window N] [--batch-max N]
                                [--triage] [--triage-every N]
                                [--triage-bundle URI] [--triage-timeout N]
                                [--judge-timeout N] [--no-bells]
    attention-manager judge verify --cmd CMD --good PATH --broken PATH [--timeout N]
    attention-manager triage --once [--bundle URI] [--timeout N]
    attention-manager triage --retry <packet_id>   # clear abandon/failure markers
    attention-manager auto list [--json]
    attention-manager auto confirm <packet_id>
    attention-manager auto reject <packet_id> --correct-option X --reason TEXT
    attention-manager recipes poll --once [--bundle NAME] [--timeout N]
    attention-manager rulebook show
    attention-manager rulebook proposals [--json]
    attention-manager rulebook apply <id>
    attention-manager rulebook reject <id> --reason TEXT
    attention-manager workunit run <pipeline.dot> [--name NAME] [--logs-dir DIR]
    attention-manager status              # workers + pending packet count
    attention-manager ledger [--date YYYY-MM-DD] [--summary]

Exit codes: 0 = ok, 1 = error (message on stderr).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from . import trust, workers, workunit
from .autolog import AutoLog
from .judge import DEFAULT_JUDGE_TIMEOUT_S
from .judge import verify as judge_verify
from .packet import Packet
from .queue import PacketQueue
from .recipe_gates import DEFAULT_TIMEOUT_S as RECIPES_DEFAULT_TIMEOUT_S
from .recipe_gates import RecipeGatePoller
from .rulebook import Rulebook
from .state import SupervisorState
from .supervisor import DEFAULT_RECIPES_EVERY_TICKS, DEFAULT_TRIAGE_EVERY_TICKS, Supervisor
from .triage import DEFAULT_TIMEOUT_S, TriageRunner


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)  # 3.11+ accepts the 'Z' suffix natively
    except ValueError:
        return None


def _age(created_at: str) -> str:
    created = _parse_iso(created_at)
    if created is None:
        return "?"
    seconds = max(0, int((datetime.now(UTC) - created).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _truncate(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def _cmd_queue_list(queue: PacketQueue, as_json: bool) -> int:
    pending = queue.list_pending()
    if as_json:
        print(json.dumps([p.to_dict() for p in pending], indent=2))
        return 0
    if not pending:
        print("queue empty (no pending packets)")
        return 0
    header = f"{'ID':<26} {'KIND':<14} {'TIER':<6} {'AGE':<5} QUESTION"
    print(header)
    print("-" * len(header))
    for p in pending:
        print(f"{p.id:<26} {p.source.kind:<14} {p.urgency.tier:<6} {_age(p.created_at):<5} {_truncate(p.question, 60)}")
    return 0


def _cmd_queue_show(queue: PacketQueue, packet_id: str, as_json: bool) -> int:
    packet = queue.get(packet_id)
    if as_json:
        print(json.dumps(packet.to_dict(), indent=2))
        return 0
    subdir, path = queue.locate(packet_id)
    print(f"# {packet.id}  [{subdir}]  {path}")
    print(json.dumps(packet.to_dict(), indent=2))
    return 0


def _cmd_queue_path(queue: PacketQueue, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"path": str(queue.root)}))
    else:
        print(queue.root)
    return 0


def _cmd_answer(queue: PacketQueue, packet_id: str, option: str, rationale: str | None, as_json: bool) -> int:
    packet: Packet = queue.answer(packet_id, option, rationale=rationale)
    assert packet.resolution is not None  # answer() always fills resolution
    if as_json:
        print(json.dumps({"id": packet.id, "resolution": packet.resolution.to_dict()}, indent=2))
    else:
        print(f"answered {packet.id}: {packet.resolution.answer} (by {packet.resolution.answered_by})")
    return 0


def _cmd_dispatch(args: argparse.Namespace, as_json: bool) -> int:
    # dispatch never writes state.json (the supervise loop owns it); it writes
    # workers/<session>/meta.json + append-only event/ledger lines, and the
    # supervisor adopts the new worker on its next tick.
    state = SupervisorState()
    cmd = args.worker_cmd or workers.default_worker_cmd(args.task, args.bundle)
    meta = workers.launch(args.name, cmd, state.home, task=args.task, judge_cmd=args.judge)
    state.append_event(
        "worker:dispatched", session=meta["session"], name=args.name, cmd=cmd, task=args.task, judge_cmd=args.judge
    )
    state.ledger_append(
        "dispatched", session=meta["session"], name=args.name, cmd=cmd, task=args.task, judge_cmd=args.judge
    )
    if as_json:
        print(json.dumps({**meta, "log": str(state.worker_log_path(meta["session"]))}, indent=2))
    else:
        print(f"dispatched {meta['session']} (log: {state.worker_log_path(meta['session'])})")
    return 0


def _cmd_supervise(args: argparse.Namespace) -> int:
    notify_spec = args.notify or os.environ.get("ATTENTION_NOTIFY") or None
    triage_every = args.triage_every if args.triage else None
    recipes_every = args.recipes_every if args.recipes else None
    supervisor = Supervisor(
        notify_spec=notify_spec,
        interval_s=args.interval,
        batch_window_s=args.batch_window,
        batch_max=args.batch_max,
        triage_every=triage_every,
        recipes_every=recipes_every,
        judge_timeout_s=args.judge_timeout,
        bells=not args.no_bells,
    )
    if supervisor.triage_runner is not None:
        if args.triage_bundle:
            supervisor.triage_runner.bundle_uri = args.triage_bundle
        if args.triage_timeout is not None:
            supervisor.triage_runner.timeout_s = args.triage_timeout
    return supervisor.run(once=args.once)


def _cmd_triage(args: argparse.Namespace, as_json: bool) -> int:
    runner = TriageRunner(bundle_uri=args.bundle, timeout_s=args.timeout)
    if args.retry:
        # Escape hatch for abandoned packets: clear the cross-pass failure
        # markers so the next pass re-attempts triage/rule_delta.
        cleared = runner.clear_abandon_markers(args.retry)
        if not cleared:
            print(f"no failure markers found for {args.retry} (nothing to clear)", file=sys.stderr)
            return 1
        print(f"cleared {', '.join(cleared)} failure marker(s) for {args.retry} — next pass will re-attempt")
        return 0
    runner.preflight()  # fail loud upfront if the amplifier binary is missing
    outcomes = runner.triage_pass()
    if as_json:
        print(json.dumps([o.to_dict() for o in outcomes], indent=2))
        return 0
    if not outcomes:
        print("triage pass: nothing to do (no untriaged pending packets, no unproposed answered packets)")
        return 0
    for o in outcomes:
        print(f"{o.packet_id}  {o.phase:<10} {o.outcome:<12} {_truncate(o.detail, 80)}")
    errors = sum(1 for o in outcomes if o.outcome == "error")
    print(f"triage pass: {len(outcomes)} packet(s) processed, {errors} error(s)")
    return 0


def _cmd_auto(args: argparse.Namespace, queue: PacketQueue, as_json: bool) -> int:
    """The Phase-2 calibration loop over queue/auto/ review records.

    HONESTY NOTE (also in the record format doc): reviewing an auto-answer is
    CALIBRATION ONLY. The producing worker already unblocked the moment the
    auto answer landed in answered/ — `auto reject` cannot un-answer it. A
    rejection records the correction and DEMOTES the cited rulebook sections
    to Phase 1 (streak 0) so the same class stops being auto-answered.
    """
    autolog = AutoLog(queue.root)
    if args.auto_command == "list":
        records = autolog.list_records(include_reviewed=False)
        if as_json:
            print(json.dumps(records, indent=2))
            return 0
        if not records:
            print(f"no unreviewed auto-answer records ({autolog.dir()})")
            return 0
        header = f"{'PACKET':<26} {'ANSWER':<8} {'SECTIONS':<28} WHY / RULES"
        print(header)
        print("-" * len(header))
        for r in records:
            detail = f"{r.get('why', '')} | rules: {', '.join(r.get('rule_refs') or [])}"
            print(
                f"{r['packet_id']:<26} {r.get('answer', '?'):<8} "
                f"{_truncate(', '.join(r.get('sections') or []), 28):<28} {_truncate(detail, 60)}"
            )
        return 0

    state = SupervisorState()
    rulebook = Rulebook()
    if args.auto_command == "confirm":
        record = autolog.mark_confirmed(args.packet_id)
        outcomes = trust.record_match(
            rulebook, state, args.packet_id, list(record.get("sections") or []), source="auto-confirm"
        )
        state.append_event("auto:confirmed", packet_id=args.packet_id, sections=record.get("sections"))
        state.ledger_append("auto_confirmed", packet_id=args.packet_id, sections=record.get("sections"))
        if as_json:
            print(json.dumps({"record": record, "trust": outcomes}, indent=2))
            return 0
        print(f"confirmed {args.packet_id}: auto answer {record.get('answer')!r} was right")
        for o in outcomes:
            note = " — PROMOTED to phase 2" if o["promoted"] else ""
            print(f"  trust: {o['section']} phase {o['phase']} streak {o['streak']}{note}")
        return 0

    if args.auto_command == "reject":
        # Validate the correction against the packet's declared options — a
        # correction naming a non-existent option is calibration garbage.
        packet = queue.get(args.packet_id)
        if args.correct_option not in packet.option_ids():
            raise ValueError(
                f"--correct-option {args.correct_option!r} is not one of packet "
                f"{args.packet_id!r} options {packet.option_ids()}"
            )
        record = autolog.mark_rejected(args.packet_id, args.correct_option, args.reason)
        outcomes = trust.record_override(
            rulebook, state, args.packet_id, list(record.get("sections") or []), source="auto-reject"
        )
        state.append_event(
            "auto:rejected",
            packet_id=args.packet_id,
            correct_option=args.correct_option,
            reason=args.reason,
            sections=record.get("sections"),
        )
        state.ledger_append(
            "auto_rejected", packet_id=args.packet_id, correct_option=args.correct_option, reason=args.reason
        )
        if as_json:
            print(json.dumps({"record": record, "trust": outcomes}, indent=2))
            return 0
        print(
            f"rejected {args.packet_id}: correction recorded (correct option {args.correct_option!r}). "
            f"NOTE: calibration only — the worker already unblocked on the auto answer; this cannot un-answer it."
        )
        for o in outcomes:
            print(f"  trust: {o['section']} DEMOTED to phase {o['phase']}, streak {o['streak']}")
        return 0
    raise ValueError(f"unhandled auto command {args.auto_command!r}")  # pragma: no cover


def _cmd_recipes(args: argparse.Namespace, queue: PacketQueue, as_json: bool) -> int:
    poller = RecipeGatePoller(queue=queue, bundle=args.bundle, timeout_s=args.timeout)
    poller.preflight()  # fail loud upfront if the amplifier binary is missing
    results = poller.poll_once()
    if as_json:
        print(json.dumps(results, indent=2))
        return 0
    if not results:
        print("recipes poll: nothing to do (no new pending gates, no answered gate packets)")
        return 0
    for r in results:
        if r["action"] == "packetized":
            print(f"packetized {r['gate']} -> {r['packet_id']}")
        elif r["action"] == "resolved":
            print(f"resolved {r['gate']}: {r['answer']} forwarded to the recipes tool ({r['packet_id']})")
        else:
            print(f"ERROR {r.get('gate', r.get('phase', '?'))}: {r['error']}")
    errors = sum(1 for r in results if r["action"] == "error")
    print(f"recipes poll: {len(results)} action(s), {errors} error(s)")
    return 0


def _cmd_rulebook(args: argparse.Namespace, as_json: bool) -> int:
    rulebook = Rulebook()
    if args.rulebook_command == "show":
        content, tokens = rulebook.read()
        if as_json:
            print(json.dumps({"path": str(rulebook.path), "approx_tokens": tokens, "content": content}, indent=2))
        else:
            print(f"# {rulebook.path}  (~{tokens} tokens, cap {rulebook.token_cap})\n")
            print(content)
        return 0
    if args.rulebook_command == "proposals":
        proposals = rulebook.list_proposals()
        if as_json:
            print(json.dumps(proposals, indent=2))
            return 0
        if not proposals:
            print(f"no rulebook proposals ({rulebook.proposals_path})")
            return 0
        header = f"{'ID':<25} {'STATUS':<9} {'PACKET':<26} {'SECTION':<24} SENTENCE/REASON"
        print(header)
        print("-" * len(header))
        for p in proposals:
            text = p.get("sentence") or p.get("reason") or ""
            print(
                f"{p.get('id', '?'):<25} {p.get('status', '?'):<9} {p.get('packet_id', '?'):<26} "
                f"{p.get('section') or '-':<24} {_truncate(text, 50)}"
            )
        return 0
    state = SupervisorState()
    if args.rulebook_command == "apply":
        record = rulebook.apply(args.proposal_id)
        state.append_event(
            "rulebook:applied", proposal_id=record["id"], section=record["section"], sentence=record["sentence"]
        )
        state.ledger_append(
            "rule_applied", proposal_id=record["id"], section=record["section"], sentence=record["sentence"]
        )
        if as_json:
            print(json.dumps(record, indent=2))
        else:
            print(f"applied {record['id']}: [{record['section']}] - {record['sentence']}")
        return 0
    if args.rulebook_command == "reject":
        record = rulebook.reject(args.proposal_id, args.reason)
        state.append_event("rulebook:rejected", proposal_id=record["id"], reason=args.reason)
        state.ledger_append("rule_rejected", proposal_id=record["id"], reason=args.reason)
        if as_json:
            print(json.dumps(record, indent=2))
        else:
            print(f"rejected {record['id']}: {args.reason}")
        return 0
    raise ValueError(f"unhandled rulebook command {args.rulebook_command!r}")  # pragma: no cover


def _worker_state(record: dict, obs: workers.Observation) -> str:
    if obs.sentinel_seen:
        return f"finished(rc={obs.exit_code})"
    if obs.alive:
        return "running"
    if record.get("finished"):
        return f"finished(rc={record.get('exit_code')})"
    return "dead(no sentinel)"


def _cmd_status(queue: PacketQueue, as_json: bool) -> int:
    state = SupervisorState()
    state.load()
    state.adopt_workers(workers.list_am_sessions())  # read-only merge; not saved
    rows = []
    for session, record in sorted(state.workers.items()):
        obs = workers.observe(session, state.worker_log_path(session))
        rows.append(
            {
                "session": session,
                "state": _worker_state(record, obs),
                "exit_code": obs.exit_code if obs.sentinel_seen else record.get("exit_code"),
                "task": record.get("task"),
            }
        )
    pending_count = len(queue.list_pending())
    if as_json:
        print(json.dumps({"workers": rows, "pending_packets": pending_count}, indent=2))
        return 0
    if not rows:
        print("no workers")
    else:
        header = f"{'SESSION':<24} {'STATE':<20} TASK"
        print(header)
        print("-" * len(header))
        for row in rows:
            print(f"{row['session']:<24} {row['state']:<20} {_truncate(row['task'] or '-', 50)}")
    print(f"pending packets: {pending_count}")
    return 0


def _cmd_judge_verify(args: argparse.Namespace, as_json: bool) -> int:
    """The broken-test protocol (context/judge-contract.md): a judge must PASS
    the known-good artifact AND FAIL the deliberately broken one. A judge that
    never fails is decoration — exit 0 ONLY when both directions behave."""
    result = judge_verify(args.cmd, args.good, args.broken, timeout_s=args.timeout)
    if as_json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.passed else 1
    for direction, expectation in ((result.good, "expect exit 0"), (result.broken, "expect nonzero")):
        code = "none (timeout/spawn failure)" if direction.exit_code is None else direction.exit_code
        status = "ok" if direction.ok else "WRONG"
        print(f"[{direction.direction}] {direction.artifact}")
        print(f"  exit: {code} ({expectation}) -> {status}")
        output = _truncate(direction.output, 200)
        print(f"  output: {output or '(none)'}")
    if result.passed:
        print("VERDICT: PASS — judge passes the good artifact and fails the broken one")
        return 0
    problems = []
    if not result.good.ok:
        problems.append("judge did NOT pass the known-good artifact")
    if not result.broken.ok:
        problems.append("judge did NOT fail the broken artifact (a judge that never fails is decoration)")
    print(f"VERDICT: FAIL — {'; '.join(problems)}")
    return 1


# -- ledger summary (the "what landed today" closure ritual) -----------------------


def summarize_ledger(entries: list[dict]) -> dict:
    """Reduce one day's ledger entries to the closure-ritual summary dict."""
    import statistics

    loops_closed = [e for e in entries if e.get("kind") == "loop_closed"]
    loops_failed = [e for e in entries if e.get("kind") == "loop_failed"]
    finished = [e for e in entries if e.get("kind") == "worker_finished"]
    unjudged = [e for e in finished if not e.get("judged")]
    created = [e for e in entries if e.get("kind") == "packet_created"]
    answered = [e for e in entries if e.get("kind") == "packet_answered"]
    latencies = [e["latency_s"] for e in answered if e.get("latency_s") is not None]
    rules_applied = [e for e in entries if e.get("kind") == "rule_applied"]
    batches = [e for e in entries if e.get("kind") == "notified_batch"]
    return {
        "loops_closed": [
            {"session": e.get("session"), "name": e.get("name"), "worker_exit": e.get("worker_exit")}
            for e in loops_closed
        ],
        "loops_failed": [
            {"session": e.get("session"), "name": e.get("name"), "reason": e.get("reason")} for e in loops_failed
        ],
        "workers_finished_unjudged": [{"session": e.get("session"), "exit_code": e.get("exit_code")} for e in unjudged],
        "packets_created": len(created),
        "packets_answered": len(answered),
        "answer_latency_median_s": round(statistics.median(latencies), 1) if latencies else None,
        "rules_applied": [
            {"proposal_id": e.get("proposal_id"), "section": e.get("section"), "sentence": e.get("sentence")}
            for e in rules_applied
        ],
        "notification_batches": len(batches),
    }


def format_ledger_summary(summary: dict, date: str, path: str) -> str:
    """Human-readable 'what landed today' rendering."""
    lines = [f"What landed: {date}  ({path})", ""]

    closed = summary["loops_closed"]
    lines.append(f"Loops closed ({len(closed)}):")
    for e in closed:
        lines.append(f"  {e['session']}  (worker exit {e['worker_exit']})")
    if not closed:
        lines.append("  (none)")

    failed = summary["loops_failed"]
    lines.append(f"Loops failed ({len(failed)}):")
    for e in failed:
        lines.append(f"  {e['session']}  — {_truncate(e.get('reason') or '?', 70)}")
    if not failed:
        lines.append("  (none)")

    unjudged = summary["workers_finished_unjudged"]
    lines.append(f"Workers finished unjudged ({len(unjudged)}):")
    for e in unjudged:
        lines.append(f"  {e['session']}  (exit {e['exit_code']})")
    if not unjudged:
        lines.append("  (none)")

    packets = f"Packets: {summary['packets_created']} created, {summary['packets_answered']} answered"
    if summary["answer_latency_median_s"] is not None:
        packets += f" (median latency {summary['answer_latency_median_s']}s)"
    lines.append(packets)

    rules = summary["rules_applied"]
    lines.append(f"Rules applied ({len(rules)}):")
    for e in rules:
        lines.append(f"  [{e.get('section') or '?'}] {_truncate(e.get('sentence') or '?', 70)}")
    if not rules:
        lines.append("  (none)")

    lines.append(f"Notification batches: {summary['notification_batches']}")
    return "\n".join(lines)


def _cmd_ledger(args: argparse.Namespace, as_json: bool) -> int:
    state = SupervisorState()
    entries = state.ledger_read(args.date)
    if args.summary:
        summary = summarize_ledger(entries)
        if as_json:
            print(json.dumps(summary, indent=2))
            return 0
        date = args.date or datetime.now(UTC).strftime("%Y-%m-%d")
        print(format_ledger_summary(summary, date, str(state.ledger_path(args.date))))
        return 0
    if as_json:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print(f"ledger empty for {args.date or 'today'} ({state.ledger_path(args.date)})")
        return 0
    for entry in entries:
        details = {k: v for k, v in entry.items() if k not in ("ts", "kind")}
        print(f"{entry.get('ts', '?'):<21} {entry.get('kind', '?'):<16} {_truncate(json.dumps(details), 80)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="attention-manager", description="Attention manager packet queue")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    queue_p = sub.add_parser("queue", help="inspect the packet queue")
    queue_sub = queue_p.add_subparsers(dest="queue_command", required=True)
    queue_sub.add_parser("list", help="list pending packets")
    show_p = queue_sub.add_parser("show", help="show a packet in full")
    show_p.add_argument("packet_id")
    queue_sub.add_parser("path", help="print the queue root path")

    answer_p = sub.add_parser("answer", help="answer a pending packet")
    answer_p.add_argument("packet_id")
    answer_p.add_argument("option")
    answer_p.add_argument("--rationale", default=None)

    dispatch_p = sub.add_parser("dispatch", help="launch a worker into an am-* tmux session")
    dispatch_p.add_argument("name", help="worker name (tmux session becomes am-<name>)")
    dispatch_p.add_argument("--task", required=True, help="the task text for the worker")
    dispatch_p.add_argument("--bundle", default=None, help="bundle URI passed to 'amplifier run -B'")
    dispatch_p.add_argument("--worker-cmd", dest="worker_cmd", default=None, help="full command override")
    dispatch_p.add_argument(
        "--judge",
        dest="judge",
        default=None,
        help=(
            "judge command gating loop closure (context/judge-contract.md): run by the supervisor "
            "when the worker finishes; exit 0 = loop:closed, nonzero = loop:failed"
        ),
    )

    supervise_p = sub.add_parser("supervise", help="run the supervisor tick loop (foreground)")
    supervise_p.add_argument("--interval", type=float, default=2.0, help="tick interval seconds (default 2)")
    supervise_p.add_argument("--once", action="store_true", help="run a single tick and exit")
    supervise_p.add_argument(
        "--notify", default=None, help="sink: file:<path> | ntfy:<url> | console ($ATTENTION_NOTIFY)"
    )
    supervise_p.add_argument("--batch-window", dest="batch_window", type=float, default=20.0)
    supervise_p.add_argument("--batch-max", dest="batch_max", type=int, default=10)
    supervise_p.add_argument(
        "--triage", action="store_true", help="run a Phase-1 triage pass every N ticks (default off)"
    )
    supervise_p.add_argument(
        "--triage-every",
        dest="triage_every",
        type=int,
        default=DEFAULT_TRIAGE_EVERY_TICKS,
        help=f"ticks between triage passes when --triage is set (default {DEFAULT_TRIAGE_EVERY_TICKS})",
    )
    supervise_p.add_argument(
        "--triage-bundle",
        dest="triage_bundle",
        default=None,
        help="triage bundle URI ($ATTENTION_TRIAGE_BUNDLE, default: repo bundles/triage.md via git)",
    )
    supervise_p.add_argument(
        "--triage-timeout", dest="triage_timeout", type=float, default=None, help="per-session timeout seconds"
    )
    supervise_p.add_argument(
        "--recipes",
        action="store_true",
        help="poll recipes approval gates into packets every N ticks (producer #4; default off)",
    )
    supervise_p.add_argument(
        "--recipes-every",
        dest="recipes_every",
        type=int,
        default=DEFAULT_RECIPES_EVERY_TICKS,
        help=f"ticks between recipe-gate polls when --recipes is set (default {DEFAULT_RECIPES_EVERY_TICKS})",
    )
    supervise_p.add_argument(
        "--no-bells",
        dest="no_bells",
        action="store_true",
        help=(
            "disable muxplex bells (default: on — packet:created with a resolvable worker "
            "session and loop:failed ring the worker's tmux bell so muxplex/deck surface it)"
        ),
    )
    supervise_p.add_argument(
        "--judge-timeout",
        dest="judge_timeout",
        type=float,
        default=DEFAULT_JUDGE_TIMEOUT_S,
        help=f"judge command timeout seconds (default {DEFAULT_JUDGE_TIMEOUT_S:.0f}); a timed-out judge is loop:failed",
    )

    judge_p = sub.add_parser("judge", help="judge utilities (context/judge-contract.md)")
    judge_sub = judge_p.add_subparsers(dest="judge_command", required=True)
    verify_p = judge_sub.add_parser(
        "verify",
        help="broken-test protocol: judge must PASS a good artifact AND FAIL a broken one",
    )
    verify_p.add_argument("--cmd", required=True, help="the judge command (run via bash -c with $ARTIFACT set)")
    verify_p.add_argument("--good", required=True, help="path to a known-good artifact (judge must exit 0)")
    verify_p.add_argument("--broken", required=True, help="path to a deliberately broken artifact (must exit nonzero)")
    verify_p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_JUDGE_TIMEOUT_S,
        help=f"per-direction timeout seconds (default {DEFAULT_JUDGE_TIMEOUT_S:.0f})",
    )

    triage_p = sub.add_parser("triage", help="run one cold-triage pass (Phase 1: recommend + bounce + rule deltas)")
    triage_mode = triage_p.add_mutually_exclusive_group(required=True)
    triage_mode.add_argument(
        "--once",
        action="store_true",
        help="run exactly one pass (continuous triage runs inside 'supervise --triage')",
    )
    triage_mode.add_argument(
        "--retry",
        metavar="PACKET_ID",
        default=None,
        help="clear the abandon/failure markers for a packet so the next pass re-attempts it",
    )
    triage_p.add_argument(
        "--bundle", default=None, help="triage bundle URI ($ATTENTION_TRIAGE_BUNDLE, default: repo bundles/triage.md)"
    )
    triage_p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"per-session timeout seconds (default {DEFAULT_TIMEOUT_S:.0f})",
    )

    auto_p = sub.add_parser(
        "auto",
        help=(
            "review Phase-2 auto-answers (queue/auto/). Calibration only: a rejection records the "
            "correction and demotes trust — it CANNOT un-answer the packet (the worker already unblocked)."
        ),
    )
    auto_sub = auto_p.add_subparsers(dest="auto_command", required=True)
    auto_sub.add_parser("list", help="list unreviewed auto-answer records (why + cited rules)")
    auto_confirm_p = auto_sub.add_parser(
        "confirm", help="confirm an auto-answer was right (counts as a match; may promote sections)"
    )
    auto_confirm_p.add_argument("packet_id")
    auto_reject_p = auto_sub.add_parser(
        "reject",
        help=(
            "reject an auto-answer: records the correction and DEMOTES the cited sections to phase 1 "
            "(streak 0). Calibration only — cannot un-answer the already-unblocked worker."
        ),
    )
    auto_reject_p.add_argument("packet_id")
    auto_reject_p.add_argument(
        "--correct-option", dest="correct_option", required=True, help="the option the human would have chosen"
    )
    auto_reject_p.add_argument("--reason", required=True, help="why the auto answer was wrong (calibration data)")

    recipes_p = sub.add_parser(
        "recipes", help="recipe approval-gate bridge (producer #4): polls `amplifier tool invoke recipes`"
    )
    recipes_sub = recipes_p.add_subparsers(dest="recipes_command", required=True)
    recipes_poll_p = recipes_sub.add_parser(
        "poll", help="one poll: packetize new pending gates, forward answered gate packets"
    )
    recipes_poll_p.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="required: run exactly one poll (continuous polling runs inside 'supervise --recipes')",
    )
    recipes_poll_p.add_argument("--bundle", default=None, help="bundle passed to 'amplifier tool invoke -b'")
    recipes_poll_p.add_argument(
        "--timeout",
        type=float,
        default=RECIPES_DEFAULT_TIMEOUT_S,
        help=f"per-invoke timeout seconds (default {RECIPES_DEFAULT_TIMEOUT_S:.0f})",
    )

    rulebook_p = sub.add_parser("rulebook", help="show the rulebook and manage rule proposals")
    rulebook_sub = rulebook_p.add_subparsers(dest="rulebook_command", required=True)
    rulebook_sub.add_parser("show", help="print the rulebook (+ approx token count)")
    rulebook_sub.add_parser("proposals", help="list rule-delta proposals")
    apply_p = rulebook_sub.add_parser("apply", help="apply a proposed rule to the rulebook (cap-checked)")
    apply_p.add_argument("proposal_id")
    reject_p = rulebook_sub.add_parser("reject", help="reject a proposed rule (reason required)")
    reject_p.add_argument("proposal_id")
    reject_p.add_argument("--reason", required=True, help="why the proposal is wrong (calibration data)")

    workunit_p = sub.add_parser(
        "workunit",
        help="run an attractor pipeline as a headless work unit (requires the [attractor] extra)",
    )
    workunit_sub = workunit_p.add_subparsers(dest="workunit_command", required=True)
    wu_run_p = workunit_sub.add_parser(
        "run",
        help="run a pipeline .dot headless; hexagon gates publish attractor-gate packets to the queue",
    )
    wu_run_p.add_argument("pipeline", help="path to the pipeline .dot file")
    wu_run_p.add_argument("--name", default=None, help="work-unit name (default: the .dot file stem)")
    wu_run_p.add_argument(
        "--logs-dir",
        dest="logs_dir",
        default=None,
        help="engine logs root (default: $ATTENTION_HOME/workunits/<name>/)",
    )

    sub.add_parser("status", help="workers with state + pending packet count")

    ledger_p = sub.add_parser("ledger", help="show the daily ledger")
    ledger_p.add_argument("--date", default=None, help="YYYY-MM-DD (default: today UTC)")
    ledger_p.add_argument(
        "--summary",
        action="store_true",
        help="'what landed today' closure ritual: loops closed/failed, packets, rules, batches",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    queue = PacketQueue()
    try:
        if args.command == "queue":
            if args.queue_command == "list":
                return _cmd_queue_list(queue, args.json)
            if args.queue_command == "show":
                return _cmd_queue_show(queue, args.packet_id, args.json)
            if args.queue_command == "path":
                return _cmd_queue_path(queue, args.json)
        if args.command == "answer":
            return _cmd_answer(queue, args.packet_id, args.option, args.rationale, args.json)
        if args.command == "dispatch":
            return _cmd_dispatch(args, args.json)
        if args.command == "supervise":
            return _cmd_supervise(args)
        if args.command == "triage":
            return _cmd_triage(args, args.json)
        if args.command == "auto":
            return _cmd_auto(args, queue, args.json)
        if args.command == "recipes":
            if args.recipes_command == "poll":
                return _cmd_recipes(args, queue, args.json)
            raise ValueError(f"unhandled recipes command {args.recipes_command!r}")  # pragma: no cover
        if args.command == "rulebook":
            return _cmd_rulebook(args, args.json)
        if args.command == "judge":
            if args.judge_command == "verify":
                return _cmd_judge_verify(args, args.json)
            raise ValueError(f"unhandled judge command {args.judge_command!r}")  # pragma: no cover
        if args.command == "workunit":
            if args.workunit_command == "run":
                return workunit.cmd_run(args, args.json)
            raise ValueError(f"unhandled workunit command {args.workunit_command!r}")  # pragma: no cover
        if args.command == "status":
            return _cmd_status(queue, args.json)
        if args.command == "ledger":
            return _cmd_ledger(args, args.json)
        raise ValueError(f"unhandled command {args.command!r}")  # pragma: no cover
    except (ValueError, KeyError, OSError, RuntimeError, ImportError) as e:
        message = e.args[0] if (isinstance(e, KeyError) and e.args) else str(e)
        print(f"error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
