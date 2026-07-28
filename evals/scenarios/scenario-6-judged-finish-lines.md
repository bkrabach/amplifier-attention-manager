# Scenario 6 — Judged finish lines (step 4: judge-gated loop closure)

**What this proves:** the step-4 promise — "a loop closes only when the judge
passes; finish lines are honest by construction" — through the real supervisor
runtime: `judge verify` broken-tests a judge (and rejects a decoration judge),
a passing judge yields `loop:closed`, a failing judge yields `loop:failed`
(loud), an unjudged worker yields neither, and the daily ledger renders both
loops by name.

## Why FAKE workers (explicit, so nobody mistakes this for an LLM test)

This scenario uses **fake `--worker-cmd` workers and NO LLM**, deliberately:

- The judge mechanics under test — verify directions, judge execution with
  cwd/env contract, loop:closed/loop:failed gating, judged:true/false fields,
  finish-line notifications, ledger summary — are **deterministic supervisor
  code paths**. An LLM in the loop would add cost, latency, and flake without
  exercising one additional line of the judge machinery.
- Real-LLM supervision (dispatch → packet → answer → unblock → observe) is
  **already proven by scenario 4**. What S6 adds is the judge layer on top of
  the same worker-observation path, and that layer's inputs (an artifact file,
  a worker exit) are identical whether an LLM or an `echo` produced them.

Hard timeout is 300s accordingly (no LLM waits).

References: `docs/designs/attention-manager.md` §The Judge Requirement +
build step 4, `src/attention_manager/judge.py`, the judge paths in
`supervisor.py`/`workers.py`/`cli.py`, `context/judge-contract.md`
($ARTIFACT convention; WORKER_LOG/WORKER_EXIT env),
`scripts/local_finishline_smoke.sh` (the local judge this mirrors).

## Setup

- Scenario-scoped `ATTENTION_QUEUE_DIR=<work-dir>/<run-id>/s6-judged-finish-lines/queue`,
  `ATTENTION_HOME=<work-dir>/<run-id>/s6-judged-finish-lines/home`;
  notify file sink at `<...>/notify.jsonl`.
- Fixed worker names g/b/u → tmux sessions `am-g`/`am-b`/`am-u`
  (pre-cleaned best-effort; requires tmux, as in S4).
- Artifacts for `judge verify`: `good-artifact.txt` (contains `GOOD-MARKER`)
  and `broken-artifact.txt` (missing it), written by the harness.

## Flow

1. **`judge verify` (broken-test protocol):**
   `attention-manager judge verify --cmd 'if grep -q GOOD-MARKER "$ARTIFACT"; …'
   --good <good> --broken <broken>` → exit 0 + `VERDICT: PASS`.
   Then the decoration rejection: `--cmd 'true'` → NONZERO exit
   ("a judge that never fails is decoration").
2. **Supervisor up** (setsid pattern; `--interval 2 --notify file:<sink>
   --batch-window 5 --batch-max 10`; stdout+stderr → supervisor.log).
3. **Dispatch three fake workers** via `--worker-cmd`:
   - **G**: writes `artifact.txt` WITH the marker into its own worker dir,
     exits 0 — `--judge` = grep-judge on relative `artifact.txt` (proves the
     judge-cwd contract: the supervisor runs judges with cwd = worker dir).
     The judge echoes a PASS/FAIL reason either way (incl. `$WORKER_EXIT`),
     per the judge contract's "prints its reason".
   - **B**: writes `artifact.txt` WITHOUT the marker, exits 0 — SAME judge.
   - **U**: `echo unjudged`, exits 0 — NO judge.
4. **Observe** (poll 2s): all three `worker:finished` events (≤120s), then
   grade events, judge.log, notify sink.
5. **Ledger**: kind counts, then `attention-manager --json ledger --summary`
   renders with `am-g` under loops closed and `am-b` under loops failed.
6. **Cleanup (trap-safe / finally):** kill supervisor process group +
   `tmux kill-session` am-g/am-b/am-u; artifacts collected even on early exit.

## Grader assertions (hard — could-not-evaluate = FAIL)

| # | Assertion | Check |
|---|-----------|-------|
| 1 | `judge-verify-pass` | working grep-judge: exit 0 AND stdout contains `VERDICT: PASS` |
| 2 | `judge-verify-rejects-decoration` | `--cmd 'true'`: NONZERO exit |
| 3 | `supervisor-started` | background launch printed a valid PID |
| 4 | `workers-dispatched` | all three `dispatch` commands exit 0 |
| 5 | `three-worker-finished` | 3 `worker:finished` events within 120s |
| 6 | `loop-closed-good` | exactly 1 `loop:closed`; session==am-g; `judge_output` tail non-empty |
| 7 | `judge-log-good` | `workers/am-g/judge.log` exists and is non-empty |
| 8 | `loop-failed-bad` | exactly 1 `loop:failed`; session==am-b; `reason` non-empty (loud; expected `judge exited 1`, verbatim recorded) |
| 9 | `finished-judged-fields` | G: `judged:true` + `judge_result:"closed"`; B: `judged:true` + `judge_result:"failed"` |
| 10 | `unjudged-worker` | U: `judged:false`, no `judge_result`; NO loop:* event for session am-u |
| 11 | `notify-finish-line-items` | sink batch items include kind `finish_line` with id am-g AND kind `finish_line_failed` with id am-b (≤60s) |
| 12 | `ledger-counts` | ledger kinds: `loop_closed`=1, `loop_failed`=1, `worker_finished`=3, `dispatched`=3 |
| 13 | `ledger-summary-renders` | `--json ledger --summary` exits 0, parses, `loops_closed[].session` contains am-g, `loops_failed[].session` contains am-b |

## Pass criteria & budgets

ALL assertions pass; unevaluable = FAIL `could-not-evaluate`. Per-scenario
hard timeout **300s**; worker-finished wait ≤120s; notify wait ≤60s; all polls
2s-interval and bounded by the scenario deadline.

## Artifacts

`supervisor.log`, `events.jsonl` copy, `ledger.jsonl` copy, `notify.jsonl`
copy, `tmux-ls.txt`, `judge-verify.out` + `judge-verify-decoration.out`,
`judge-am-g.log` + `judge-am-b.log`, worker logs for all three sessions,
`ledger-summary.json`, `harness.log` — collected even on early bail.
