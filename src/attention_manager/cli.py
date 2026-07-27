"""attention-manager CLI — queue, dispatch, and supervision.

Commands:
    attention-manager queue list          # pending packets (table or --json)
    attention-manager queue show <id>     # full packet
    attention-manager queue path          # queue root path
    attention-manager answer <id> <option> [--rationale TEXT]
    attention-manager dispatch <name> --task TEXT [--bundle URI] [--worker-cmd CMD]
    attention-manager supervise [--interval N] [--once] [--notify SINK]
                                [--batch-window N] [--batch-max N]
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
from .state import SupervisorState
from .supervisor import Supervisor


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
    supervisor = Supervisor(
        notify_spec=notify_spec,
        interval_s=args.interval,
        batch_window_s=args.batch_window,
        batch_max=args.batch_max,
    )
    return supervisor.run(once=args.once)


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
