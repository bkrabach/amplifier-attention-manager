# Scenario 1 — Decision roundtrip (the core promise)

**What this proves:** a real `amplifier run` worker session, with the
`packet-escalation` behavior composed in, blocks on a multi-option decision by
writing a `kind=decision` packet to the shared disk queue; a human-side answer
via the `attention-manager` CLI unblocks it; the worker acts on the answer.
This is design build-order step 1 ("the whole system in miniature") exercised
through the REAL worker runtime instead of the local subprocess judge
(`scripts/local_roundtrip.sh`).

References: `docs/designs/attention-manager.md` §Metrics & proof gate (Tuesday
test item 2), `docs/designs/attention-manager-behavioral-model.md` §8
Scenario 1, `context/packet-schema.md`.

## Setup

- Runs inside an already-provisioned DTU (harness receives the exec prefix).
- Scenario-scoped queue: `ATTENTION_QUEUE_DIR=<work-dir>/<run-id>/s1-decision-roundtrip/queue`
  (isolation from other scenarios sharing the DTU).
- Worker bundle: `<repo-dir>/bundles/test-worker.md` (foundation +
  `behaviors/packet-escalation.yaml`).

## Task

Launch in the background (own process group, stdout+stderr to `worker.log`,
exit code to `worker.exit`):

```
amplifier run -B <repo-dir>/bundles/test-worker.md "<prompt>"
```

The prompt contains a `NEEDS-HUMAN-DECISION` marker with two options —
A ("use JSON") / B ("use YAML") — and instructs the worker to call
`request_decision`, wait, print exactly `DECISION RECEIVED: <answer>`, and
proceed per the answer.

## Steps

1. Launch worker in background; record PID.
2. Poll `attention-manager --json queue list` (2s interval) until a
   `source.kind == "decision"` packet appears (timeout **240s**).
3. Read the packet through the queue lib (`queue show --json <id>`) and grade
   its fields.
4. Answer: `attention-manager answer <id> B --rationale "eval"`.
5. Wait for the worker process to finish (`worker.exit` appears).
6. Grade worker output and final queue state.

## Grader assertions (programmatic — files, strings, exit codes)

| # | Assertion | Check |
|---|-----------|-------|
| 1 | `packet-appeared` | A `kind=decision` packet is listed within 240s |
| 2 | `packet-schema-valid` | `queue show --json <id>` exits 0 (queue lib parse) AND: `id` starts `pkt-`, `schema_version == 1`, `question` non-empty, exactly 2 options with ids `{A, B}` |
| 3 | `answer-accepted` | `attention-manager answer <id> B --rationale eval` exits 0 |
| 4 | `worker-finished` | worker process exits before the scenario deadline |
| 5 | `worker-exit-zero` | `worker.exit` content is `0` |
| 6 | `worker-received-answer` | `worker.log` contains `DECISION RECEIVED: B` |
| 7 | `packet-answered-authoritative` | `answered/<id>.json` exists, `resolution.answer == "B"`, `resolution.answered_by == "human"`, `resolution.answered_at` present |
| 8 | `pending-removed` | `pending/<id>.json` no longer exists (normal resolution flow, packet-schema.md §Resolution flow) |

## Pass criteria

ALL assertions pass. Any assertion that cannot be evaluated (e.g. no packet id
to grade against) is recorded as FAIL with reason `could-not-evaluate` — never
silently skipped.

## Failure diagnostics captured

`worker.log` (full), `worker.exit`, every queue-list snapshot
(`queue_snapshots.jsonl`), the packet JSON at each graded stage, and the
harness command log (`harness.log`).

Per-scenario hard timeout: 300s. On timeout the worker process group is
SIGKILLed (cleanup) and all outstanding assertions fail loudly.
