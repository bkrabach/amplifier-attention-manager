# Scenario 2 — Permission gate through the REAL hook wiring

**What this proves:** the one thing unit tests cannot — that at real session
runtime, `hooks-packet-approval` registers itself with the standard
`hooks-approval` module via the `approval.register_provider` capability in
`on_session_ready()`, so a gated `bash` call flows:

```
tool:pre → hooks-approval (gate) → registered provider
        → hooks-packet-approval → kind=permission packet on disk
        → human answers "allow" via CLI → ApprovalResponse(approved=True)
        → bash executes
```

References: `docs/designs/attention-manager.md` Tier 2 producer #2 / D1,
`docs/designs/attention-manager-behavioral-model.md` §8 Scenario 2,
`modules/hooks-packet-approval/.../__init__.py` (`on_session_ready`),
`context/packet-schema.md` (permission packets: exactly `allow`/`deny`).

## Setup

- Scenario-scoped queue: `ATTENTION_QUEUE_DIR=<work-dir>/<run-id>/s2-permission-gate/queue`.
- Worker bundle: `<repo-dir>/evals/bundles/test-worker-gated.md` — composes:
  - foundation,
  - `behaviors/packet-escalation.yaml` (provides `hooks-packet-approval`),
  - the standard `hooks-approval` module configured to gate `bash`
    (`tools.bash.require_approval: true`) with **`rules: []`** — hooks-approval's
    DEFAULT_RULES auto-approve `echo*` bash commands, which would silently
    bypass the provider and invalidate the scenario.

## Task

Background-launch:

```
amplifier run -B <repo-dir>/evals/bundles/test-worker-gated.md \
  "Run \`echo eval-gate-ok\` using the bash tool and report its output verbatim."
```

## Steps

1. Launch worker in background; record PID.
2. Poll `attention-manager --json queue list` until a `source.kind == "permission"`
   packet appears (timeout 240s).
3. Grade the packet's options.
4. Answer: `attention-manager answer <id> allow --rationale "eval"`.
5. Wait for worker completion; grade output.

## Grader assertions

| # | Assertion | Check |
|---|-----------|-------|
| 1 | `permission-packet-appeared` | A `kind=permission` packet is listed within 240s |
| 2 | `options-exactly-allow-deny` | Packet has exactly 2 options with ids `{allow, deny}` |
| 3 | `answer-accepted` | `answer <id> allow --rationale eval` exits 0 |
| 4 | `worker-finished` | worker process exits before scenario deadline |
| 5 | `worker-exit-zero` | `worker.exit` content is `0` |
| 6 | `bash-output-present` | `worker.log` contains `eval-gate-ok` (the gated command actually executed after allow) |
| 7 | `packet-answered-allow` | `answered/<id>.json` has `resolution.answer == "allow"`, `answered_by == "human"` |

## The failure mode this scenario exists to catch — do not soften it

If the permission packet **never appears** (assertion 1 fails), the most likely
causes are exactly the wiring defects this eval hunts:

- `on_session_ready()` never ran, or ran before/without `approval.register_provider`
  being available → hooks-approval auto-denies ("No approval provider available")
  or falls back to console/default behavior;
- the session used some other approval path entirely;
- an auto-approval rule swallowed the gate (why the bundle sets `rules: []`).

In ALL such cases the scenario **FAILS** with diagnostics captured (worker.log,
queue snapshots, harness command log). A worker that completes the bash task
without a packet ever appearing is a FAIL, not a pass — the command ran without
the gate flowing through our provider.

## Pass criteria

ALL assertions pass. Unevaluable assertions are FAIL with reason
`could-not-evaluate`. Per-scenario hard timeout: 300s; worker process group is
SIGKILLed on cleanup.
