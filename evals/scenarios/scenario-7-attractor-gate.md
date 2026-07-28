# Scenario 7 — Attractor hexagon gate through the REAL pipeline engine

**What this proves:** a headless attractor work unit (`attention-manager
workunit run`), driven by the REAL `amplifier-module-loop-pipeline` engine,
blocks on a hexagon gate by publishing a `kind=attractor-gate` packet to the
shared disk queue; a human answer via the CLI routes the pipeline down the
chosen edge; the work unit finishes and the finish is recorded (event +
ledger). This is design build-order step 5 (Tier 2 producer #3, D3) exercised
end to end.

References: `docs/designs/attention-manager.md` §Tier 2 producer #3 / D3,
`context/packet-schema.md` (attractor-gate ≥2 options rule),
`src/attention_manager/attractor_gate.py`, `src/attention_manager/workunit.py`,
`evals/pipelines/gate.dot`, `scripts/local_attractor_smoke.sh` (the local
judge for this step).

## Deterministic — NO LLM (explicit, so nobody mistakes this for an LLM test)

The pipeline (`gate.dot`) contains ONLY hexagon (gate) + parallelogram (tool)
nodes — no box/LLM nodes, no provider, no session (`backend=None`). Every
step is deterministic shell/engine mechanics; hard timeout is 300s
accordingly. Real-LLM worker behavior is proven by scenarios 1/4.

## Setup

- Requires the `[attractor]` extra:
  `pip install 'amplifier-attention-manager[attractor]'`. No provider/session
  is needed — the pipeline is fully standalone (`backend=None`, no box/LLM
  nodes in `gate.dot`).
- Scenario-scoped `ATTENTION_QUEUE_DIR` and `ATTENTION_HOME`; a scratch
  working directory (the tool nodes' `tool_command` writes `A.txt`/`R.txt`
  relative to the **process cwd**, so the harness launches the workunit with
  `cd <workdir> && …`).

## Task

Launch in the background (setsid + pgid-file pattern; stdout+stderr →
`wu.log`, exit code → `wu.exit`) from the scratch working directory:

```
cd <workdir> && attention-manager workunit run \
    <repo-dir>/evals/pipelines/gate.dot --name wu-eval
```

## Missing-extra environment failure (honest, not softened)

If the `[attractor]` extra is not installed in the environment, the workunit
dies immediately with the loud ImportError naming
`amplifier-attention-manager[attractor]` /
`amplifier_module_loop_pipeline`. The harness detects this (workunit exited
before publishing a gate + marker text in `wu.log`) and grades assertion 1
as FAIL `could-not-evaluate` **with the captured error text** — an honest
environment failure, never a soft skip. (The DTU refresh installs the extra;
until then this is the expected outcome.)

## Grader assertions (hard — could-not-evaluate = FAIL)

| # | Assertion | Check |
|---|-----------|-------|
| 1 | `workunit-launched` | pgid captured AND the workunit published a gate before exiting (missing-extra death ⇒ CNE with error text captured) |
| 2 | `gate-packet-shape` | `kind=attractor-gate` packet ≤60s; `question == "Approve the work unit?"`; option ids exactly `[A, R]` with labels containing Approve/Reject; `source.work_unit == "wu-eval"`; `stage: gate` in `context` |
| 3 | `events-gate-created` | `gate:packet_created` event with `work_unit == wu-eval` + `packet_id` (≤15s after the packet) |
| 4 | `answer-accepted` | `attention-manager answer <id> A` exits 0 |
| 5 | `workunit-completed` | workunit process finishes ≤60s after the answer; exit 0 |
| 6 | `route-A-taken` | `A.txt` exists in the workdir with content `A`; `R.txt` does NOT exist (misroute protection: an out-of-options answer would have raised, never silently routed) |
| 7 | `events-answered-finished` | `gate:answered {answer: "A", packet_id}` + `workunit:finished {name: "wu-eval", status: "success"}` |
| 8 | `ledger-workunit-finished` | daily ledger has one `workunit_finished {name: "wu-eval", status: "success"}` entry |

## Budgets & artifacts

Per-scenario hard timeout **300s**; gate-packet wait ≤60s; completion wait
≤60s; all polls 2s-interval, bounded by the scenario deadline. Cleanup kills
the workunit process group (trap-safe / finally).

Artifacts: `workunit.log` (wu stdout+stderr), `workunit.exit`,
`packet-pending.json`, `events.jsonl` copy, `ledger.jsonl` copy,
`queue_snapshots.jsonl`, `pipeline-logs.txt` (concatenated
`$ATTENTION_HOME/workunits/wu-eval/` tree), `workdir-state.txt`
(A.txt/R.txt state), `harness.log` — collected even on early bail.

## Known v1 limits (documented, fail-loud)

- **FREEFORM gates are NOT supported**: the packet contract requires
  enumerated options (cold-reader test). A `mode=freeform` gate raises a loud
  error naming the gate/stage. Hexagon gates with labeled edges are the
  design target.
- **Single-option gates are rejected** (`attractor-gate` packets require ≥2
  options — a one-option gate is not a decision).
- **Judges are NOT wired into `workunit run` itself**: finish-line judging
  stays with the step-4 dispatch path
  (`dispatch <name> --worker-cmd 'attention-manager workunit run …' --judge …`).
- Cancellation (SKIPPED answer → Outcome FAIL) is out of scope for v1.
- Avoid nested manager→child→hexagon gates (loop-pipeline example 11 is a
  documented failing fixture); top-level hexagons are safe. Do not declare
  per-node timeouts on gate nodes — gates may legitimately wait hours.
