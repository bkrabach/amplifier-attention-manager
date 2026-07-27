# Scenario 4 — Supervised fleet roundtrip (step 2: the supervisor loop)

**What this proves:** the full step-2 story with REAL LLM workers — the unique
in-DTU value over `scripts/local_supervisor_smoke.sh` (which uses fake
queue-lib workers). Two `amplifier run` workers dispatched into `am-*` tmux
sessions block on decisions; the supervisor observes the queue and the fleet,
emits events, batches notifications, survives a mid-run SIGKILL+restart with
no duplicate events (D5), and after CLI answers both workers unblock and the
events + ledger tell the complete story.

References: `docs/designs/attention-manager.md` §Tier 1 + build step 2,
`src/attention_manager/{supervisor,workers,notify,state}.py`,
`scripts/local_supervisor_smoke.sh` (the local judge this mirrors).

## Setup

- Scenario-scoped state (isolation on the shared DTU):
  - `ATTENTION_QUEUE_DIR=<work-dir>/<run-id>/s4-supervised-fleet/queue`
  - `ATTENTION_HOME=<work-dir>/<run-id>/s4-supervised-fleet/home`
  - notify file sink: `<work-dir>/<run-id>/s4-supervised-fleet/notify.jsonl`
- Requires **tmux** in the DTU (`dispatch`/`supervise` fail loud without it).
- Worker bundle: `file://<repo-dir>/bundles/test-worker.md`.
- Worker tmux sessions have FIXED names (`am-w1`, `am-w2`) per the dispatch
  contract — leftovers from prior runs are best-effort killed first
  (`dispatch` fails loud on an existing session).

## Flow

1. **Pre-clean:** `tmux kill-session` for `am-w1`/`am-w2` (best-effort).
2. **Supervisor up:** background-launch (setsid, own process group; stdout+
   stderr appended to `supervisor.log`; prints PID):
   `attention-manager supervise --interval 2 --notify file:<sink> --batch-window 30 --batch-max 10`
   (ATTENTION_QUEUE_DIR + ATTENTION_HOME exported in the exec'd command —
   `dispatch`/`supervise` both need them).
3. **Dispatch fleet:** `attention-manager dispatch w1 --task '<JSON-vs-YAML
   NEEDS-HUMAN-DECISION task, tag [w1]>' --bundle file://<repo>/bundles/test-worker.md`
   and `w2` (LRU-vs-FIFO, tag `[w2]`). Each task instructs the worker to
   include its literal tag in the `request_decision` question (packet→worker
   mapping), print `DECISION RECEIVED: <answer>`, and finish.
4. **Observe (poll 2s, generous budgets — two parallel LLM workers):**
   sessions exist → 2 `kind=decision` packets (≤240s) → exactly 2
   `packet:created` in `$ATTENTION_HOME/events.jsonl` → notifications.
5. **Durability mid-run:** SIGKILL the supervisor process group; restart with
   the same flags; wait ~2 ticks; `packet:created` count must STILL be
   exactly 2 (state.json is authoritative on restart — no re-announce, D5).
6. **Answer:** w1→A, w2→B via `attention-manager answer` (mapping via tag,
   domain-keyword fallback; unresolvable mapping = FAIL could-not-evaluate).
7. **Unblock + full story:** both worker.logs
   (`$ATTENTION_HOME/workers/am-*/worker.log`, ANSI-stripped before matching)
   contain `DECISION RECEIVED: A` / `DECISION RECEIVED: B` (≤180s); events
   gain 2 `packet:answered` (each with a non-null `latency_s`) and 2
   `worker:finished` (each `judged: false`, `exit_code: 0`); today's ledger
   contains `dispatched`×2, `packet_created`×2, `packet_answered`×2,
   `worker_finished`×2, `notified_batch`≥1.
8. **Cleanup (trap-safe / finally):** kill supervisor process group(s) +
   `tmux kill-session` both workers; artifacts collected even on early exit.

## Grader assertions (hard — could-not-evaluate = FAIL)

| # | Assertion | Check |
|---|-----------|-------|
| 1 | `supervisor-started` | background launch printed a valid PID |
| 2 | `workers-dispatched` | both `dispatch` commands exit 0 |
| 3 | `tmux-sessions-exist` | `am-w1` AND `am-w2` exist (`tmux has-session`) |
| 4 | `two-decision-packets` | exactly 2 `kind=decision` packets pending within 240s |
| 5 | `events-packet-created-x2` | events.jsonl contains exactly 2 `packet:created` |
| 6 | `all-created-packets-notified` | every non-empty sink line parses as a batch record (`count` + `packets[]`), and BOTH created packet ids appear across batch records (≤120s after both packets seen) |
| 7 | `restart-no-duplicate-events` | after SIGKILL + restart + ~2 ticks, `packet:created` count is STILL exactly 2 |
| 8 | `packet-worker-mapping` | each packet maps to exactly one worker (tag `[w1]`/`[w2]`, keyword fallback) |
| 9 | `answers-accepted` | `answer <w1-pkt> A` and `answer <w2-pkt> B` both exit 0 |
| 10 | `w1-received-A` | am-w1 worker.log contains `DECISION RECEIVED: A` (≤180s, ANSI-stripped) |
| 11 | `w2-received-B` | am-w2 worker.log contains `DECISION RECEIVED: B` (≤180s, ANSI-stripped) |
| 12 | `events-packet-answered-x2-latency` | exactly 2 `packet:answered`, each with non-null `latency_s` |
| 13 | `events-worker-finished-x2-judged-false` | exactly 2 `worker:finished`, each `judged == false` and `exit_code == 0` |
| 14 | `ledger-full-story` | ledger kinds: dispatched=2, packet_created=2, packet_answered=2, worker_finished=2, notified_batch>=1 |

## The ONE-batch check (soft, honest)

The local smoke gets a deterministic single batch by dispatching both fake
workers before the supervisor's first tick. With real LLM workers the two
packets can arrive more than the 30s batch window apart and legitimately
produce two batches. So:

- **Hard assertion (#6):** every created packet was notified in some batch,
  and every notification line is a well-formed batch record.
- **Soft check (recorded in assertion #6's detail):** whether ONE batch
  covered both packets (`single-batch=yes/no (N batches)`). Two batches is a
  PASS with the honest detail recorded; non-batch-shaped sink lines are a
  hard FAIL.

## Pass criteria & budgets

ALL hard assertions pass. Per-scenario hard timeout: **600s** (two parallel
LLM workers, a batch-window wait, and a restart). All poll loops are 2s-interval
and bounded by both their own budget and the scenario deadline.

## Artifacts

`supervisor.log`, `worker-am-w1.log` + `worker-am-w2.log`, `events.jsonl`
copy, `ledger.jsonl` copy (all ledger files concatenated), `notify.jsonl`
copy, `tmux-ls.txt`, `queue_snapshots.jsonl`, both packet JSONs, and
`harness.log` — collected even when the scenario bails early.
