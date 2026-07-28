# Judge Contract

> "An agent without an exit condition stops when it feels done, which is not a
> condition, it is a mood."

Consumers of this contract: the **manager** (finish-line evaluation, build step
4+) and the **dispatch instruction** for autonomous work units. It is never
always-on worker context.

## Requirements

1. **Every autonomously dispatched work unit ships a judge**: a command that
   exits `0` (pass) or `1` (fail) **and prints a reason** either way. A silent
   exit code is not a judge — the reason is what makes failures diagnosable and
   passes auditable.
2. Judges wrap whatever verification fits: test suites, schema checks, file
   assertions, or an LLM yes/no where mechanical checks can't reach. The
   wrapper still exits 0/1 + reason.
3. **No judge → no autonomous dispatch.** The work runs as an interactive
   session the human hops into via muxplex instead. Exploratory work is
   loop-shaped; don't fake a judge for it.
4. A loop **closes** only when the judge passes. Finish lines are honest by
   construction — the judge, not the worker's mood, closes them.

## The broken-test protocol

**Judges are broken-tested before batch dispatch.** A judge that never fails is
decoration, and every green light after it is meaningless.

Before trusting a judge:

1. Run it against a **known-good** artifact → it MUST pass (exit 0, reason printed).
2. Run it against a **deliberately broken** artifact (sabotage the exact
   failure mode it guards against) → it MUST fail (exit 1, reason printed).
3. Only after both directions are verified may the judge gate autonomous
   dispatch or loop closure.

Judges that ship in this repo encode the protocol as a `--self-test` flag that
runs both directions and asserts both (see `scripts/local_roundtrip.sh`).

### `judge verify` and the `$ARTIFACT` convention

The CLI mechanizes the protocol:

```
attention-manager judge verify --cmd CMD --good PATH --broken PATH
```

It runs `CMD` (via `bash -c`) twice — once against the known-good artifact
(must exit 0) and once against the deliberately broken one (must exit
nonzero) — prints both results, and exits 0 ONLY when both directions
behave. A judge that passes both directions is decoration and the verify
FAILs.

**The `$ARTIFACT` convention:** `judge verify` exports the artifact path
under test as the `ARTIFACT` environment variable. Judges under this tool
read `$ARTIFACT` when verifying artifacts (`grep -q MARKER "$ARTIFACT"`), or
ignore it and check world-state instead (test suites, deployed services).
World-state judges are still broken-tested by sabotaging the world, not the
artifact path.

## How the supervisor runs judges

When a dispatched worker (`dispatch --judge "<command>"`) finishes — exit
sentinel seen or its tmux session died — the supervisor runs the judge:

- via `bash -c`, with **cwd = the worker's dir** (`workers/<session>/`)
- environment: `ATTENTION_HOME`, `ATTENTION_QUEUE_DIR`, **`WORKER_LOG`**
  (absolute path to the worker's `worker.log`) and **`WORKER_EXIT`** (the
  worker's exit code; empty string when the session died without a
  sentinel) — so judges can assert on worker output and exit status
- combined stdout+stderr is captured to `workers/<session>/judge.log`
- timeout: `supervise --judge-timeout` (default 60s)

Judge exit 0 → `loop:closed` (+ ledger `loop_closed` + a `finish_line`
notification item). Judge nonzero, timeout, or spawn failure →
`loop:failed`, loud, with the reason and output tail (+ ledger
`loop_failed` + `finish_line_failed` notification item). A configured judge
is **never** silently skipped — any inability to run it IS `loop:failed`.

## Output convention

```
PASS: <one-line reason>     # exit 0
FAIL: <one-line reason>     # exit 1
```
