"""Headless attractor work-unit runner (``attention-manager workunit run``).

Runs a pipeline ``.dot`` file through the loop-pipeline engine with a
:class:`~attention_manager.attractor_gate.PacketInterviewer` wired in, fully
standalone: ``backend=None``, no session/provider needed as long as the graph
has no box(LLM) nodes. Hexagon gates publish ``kind="attractor-gate"``
packets to the shared queue and block until answered (design doc §Tier 2
producer #3, D3).

Verified invocation (against amplifier-module-loop-pipeline):
    parse_dot → PipelineContext() → apply_transforms → validate_or_raise
    → HandlerRegistry(HandlerContext(backend=None, hooks=None, interviewer=X))
    → await PipelineEngine(graph, context, registry, logs_root).run()

Judges / loop closure: DELIBERATELY NOT WIRED HERE. Finish-line judging for
work units stays with the step-4 dispatch path — a work unit is dispatched as
a worker and judged by the supervisor:

    attention-manager dispatch <name> \\
        --worker-cmd 'attention-manager workunit run pipeline.dot --name <name>' \\
        --judge '<judge cmd>'

Nothing in this module prevents that composition: it never writes state.json,
never takes the supervisor lock, and exits 0 iff the pipeline Outcome is
success (so the tmux sentinel / judge flow works unchanged).

All loop-pipeline imports are lazy — the core package stays stdlib-only.
Missing dependency → loud ImportError naming the ``[attractor]`` extra.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .attractor_gate import PacketInterviewer, import_loop_pipeline
from .queue import PacketQueue
from .state import SupervisorState


async def run_pipeline(
    dot_path: Path,
    name: str,
    logs_root: str,
    state: SupervisorState,
    queue: PacketQueue | None = None,
) -> Any:
    """Parse, transform, validate, and run the pipeline. Returns the engine Outcome."""
    dot_parser = import_loop_pipeline("dot_parser")
    context_mod = import_loop_pipeline("context")
    transforms_mod = import_loop_pipeline("transforms")
    validation_mod = import_loop_pipeline("validation")
    handlers_pkg = import_loop_pipeline("handlers")
    handlers_ctx_mod = import_loop_pipeline("handlers.context")
    engine_mod = import_loop_pipeline("engine")

    graph = dot_parser.parse_dot(dot_path.read_text(encoding="utf-8"))
    pipeline_context = context_mod.PipelineContext()
    transforms_mod.apply_transforms(graph, pipeline_context)
    validation_mod.validate_or_raise(graph)

    interviewer = PacketInterviewer(
        queue or PacketQueue(),
        work_unit_name=name,
        events_emitter=state.append_event,
    )
    handler_ctx = handlers_ctx_mod.HandlerContext(backend=None, hooks=None, interviewer=interviewer)
    registry = handlers_pkg.HandlerRegistry(handler_ctx)
    engine = engine_mod.PipelineEngine(graph, pipeline_context, registry, logs_root)
    return await engine.run()


def cmd_run(args: argparse.Namespace, as_json: bool) -> int:
    """CLI entry for ``workunit run``. Exit 0 iff the pipeline Outcome is success."""
    dot_path = Path(args.pipeline).expanduser()
    if not dot_path.is_file():
        raise ValueError(f"pipeline file not found: {dot_path}")
    name = args.name or dot_path.stem

    state = SupervisorState()
    logs_root = Path(args.logs_dir).expanduser() if args.logs_dir else state.home / "workunits" / name
    logs_root.mkdir(parents=True, exist_ok=True)

    outcome = asyncio.run(run_pipeline(dot_path, name, str(logs_root), state))

    status = getattr(outcome.status, "value", None) or str(outcome.status)
    state.append_event("workunit:finished", name=name, status=status)
    state.ledger_append("workunit_finished", name=name, status=status)

    success = status == "success"
    if as_json:
        print(
            json.dumps(
                {"name": name, "status": status, "success": success, "logs_root": str(logs_root)},
                indent=2,
            )
        )
    else:
        print(f"work unit {name!r} finished: {status} (logs: {logs_root})")
    if not success:
        reason = (
            getattr(outcome, "failure_reason", None)
            or getattr(outcome, "notes", None)
            or f"pipeline outcome status={status!r}"
        )
        print(f"error: work unit {name!r} did not succeed: {reason}", file=sys.stderr)
        return 1
    return 0
