"""attention-manager CLI — inspect and answer the packet queue.

Commands:
    attention-manager queue list          # pending packets (table or --json)
    attention-manager queue show <id>     # full packet
    attention-manager queue path          # queue root path
    attention-manager answer <id> <option> [--rationale TEXT]

Exit codes: 0 = ok, 1 = error (message on stderr).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from datetime import timezone

from .packet import Packet
from .queue import PacketQueue


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
        raise ValueError(f"unhandled command {args.command!r}")  # pragma: no cover
    except (ValueError, KeyError, OSError) as e:
        message = e.args[0] if (isinstance(e, KeyError) and e.args) else str(e)
        print(f"error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
