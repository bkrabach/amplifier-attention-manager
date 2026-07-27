"""attention-manager CLI — queue, dispatch, supervision, triage, and rulebook.

Commands:
    attention-manager queue list          # pending packets (table or --json)
    attention-manager queue show <id>     # full packet
    attention-manager queue path          # queue root path
    attention-manager answer <id> <option> [--rationale TEXT]
    attention-manager dispatch <name> --task TEXT [--bundle URI] [--worker-cmd CMD]
    attention-manager supervise [--interval N] [--once] [--notify SINK]
                                [--batch-window N] [--batch-max N]
                                [--triage] [--triage-every N]
                                [--triage-bundle URI] [--triage-timeout N]
    attention-manager triage --once [--bundle URI] [--timeout N]
    attention-manager rulebook show
    attention-manager rulebook proposals [--json]
    attention-manager rulebook apply <id>
    attention-manager rulebook reject <id> --reason TEXT
    attention-manager status              # workers + pending packet count
    attention-manager ledger [--date YYYY-MM-DD]

Exit codes: 0 = ok, 1 = error (message on stderr).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from datetime import timezone

from . import workers
from .packet import Packet
from .queue import PacketQueue
from .rulebook import Rulebook
from .state import SupervisorState
from .supervisor import DEFAULT_TRIAGE_EVERY_TICKS
from .supervisor import Supervisor
from .triage import DEFAULT_TIMEOUT_S
from .triage import TriageRunner


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age(created_at: str) -> str:
    created = _parse_iso(created_at)
    if created is None:
        return "?"
    seconds = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
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
    meta = workers.launch(args.name, cmd, state.home, task=args.task)
    state.append_event("worker:dispatched", session=meta["session"], name=args.name, cmd=cmd, task=args.task)
    state.ledger_append("dispatched", session=meta["session"], name=args.name, cmd=cmd, task=args.task)
    if as_json:
        print(json.dumps({**meta, "log": str(state.worker_log_path(meta["session"]))}, indent=2))
    else:
        print(f"dispatched {meta['session']} (log: {state.worker_log_path(meta['session'])})")
    return 0


def _cmd_supervise(args: argparse.Namespace) -> int:
    notify_spec = args.notify or os.environ.get("ATTENTION_NOTIFY") or None
    triage_every = args.triage_every if args.triage else None
    supervisor = Supervisor(
        notify_spec=notify_spec,
        interval_s=args.interval,
        batch_window_s=args.batch_window,
        batch_max=args.batch_max,
        triage_every=triage_every,
    )
    if supervisor.triage_runner is not None:
        if args.triage_bundle:
            supervisor.triage_runner.bundle_uri = args.triage_bundle
        if args.triage_timeout is not None:
            supervisor.triage_runner.timeout_s = args.triage_timeout
    return supervisor.run(once=args.once)


def _cmd_triage(args: argparse.Namespace, as_json: bool) -> int:
    runner = TriageRunner(bundle_uri=args.bundle, timeout_s=args.timeout)
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


def _cmd_ledger(args: argparse.Namespace, as_json: bool) -> int:
    state = SupervisorState()
    entries = state.ledger_read(args.date)
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

    triage_p = sub.add_parser("triage", help="run one cold-triage pass (Phase 1: recommend + bounce + rule deltas)")
    triage_p.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="required: run exactly one pass (continuous triage runs inside 'supervise --triage')",
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

    rulebook_p = sub.add_parser("rulebook", help="show the rulebook and manage rule proposals")
    rulebook_sub = rulebook_p.add_subparsers(dest="rulebook_command", required=True)
    rulebook_sub.add_parser("show", help="print the rulebook (+ approx token count)")
    rulebook_sub.add_parser("proposals", help="list rule-delta proposals")
    apply_p = rulebook_sub.add_parser("apply", help="apply a proposed rule to the rulebook (cap-checked)")
    apply_p.add_argument("proposal_id")
    reject_p = rulebook_sub.add_parser("reject", help="reject a proposed rule (reason required)")
    reject_p.add_argument("proposal_id")
    reject_p.add_argument("--reason", required=True, help="why the proposal is wrong (calibration data)")

    sub.add_parser("status", help="workers with state + pending packet count")

    ledger_p = sub.add_parser("ledger", help="show the daily ledger")
    ledger_p.add_argument("--date", default=None, help="YYYY-MM-DD (default: today UTC)")

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
        if args.command == "rulebook":
            return _cmd_rulebook(args, args.json)
        if args.command == "status":
            return _cmd_status(queue, args.json)
        if args.command == "ledger":
            return _cmd_ledger(args, args.json)
        raise ValueError(f"unhandled command {args.command!r}")  # pragma: no cover
    except (ValueError, KeyError, OSError, RuntimeError) as e:
        message = e.args[0] if (isinstance(e, KeyError) and e.args) else str(e)
        print(f"error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
