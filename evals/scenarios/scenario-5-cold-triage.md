# Scenario 5 — Cold triage pass (step 3: rulebook + cold triage)

**What this proves:** the full step-3 story with a REAL LLM triage agent — the
unique in-DTU value over `scripts/local_triage_smoke.sh` (which uses a fake
amplifier binary writing canned verdicts). A real `amplifier run` session with
the restricted triage bundle must: recommend on a cold-decidable packet
(atomically, still pending), bounce a cold-undecidable one with a reason,
propose exactly ONE rule_delta after a human answer (idempotent across
passes), and land an applied rule under the right rulebook section.

References: `docs/designs/attention-manager.md` §Triage + §Rulebook Contract +
build step 3, `src/attention_manager/{triage,rulebook}.py`,
`bundles/triage.md`, `scripts/local_triage_smoke.sh` (the local judge this
mirrors).

## Setup

- Scenario-scoped state: `ATTENTION_QUEUE_DIR=<work-dir>/<run-id>/s5-cold-triage/queue`,
  `ATTENTION_HOME=<work-dir>/<run-id>/s5-cold-triage/home`.
- **Rulebook seeded directly by the harness** (user-data seeding is
  legitimate): the standard 5-section template
  (Attention priorities / Auto-answer rules / Escalation thresholds /
  Edge cases / When you cannot proceed) plus ONE seed rule under
  `## Auto-answer rules`:
  *"Prefer option ids whose consequence mentions lower operational risk when
  confidence is otherwise equal."*
- **Two packets seeded directly** via a python one-liner using the ROOT queue
  lib inside the DTU (`PYTHONPATH=<repo>/src` — provenance is not under test;
  S1 already proves the worker path):
  - **P1 (cold-decidable):** kind=decision, question "Choose rollout strategy
    for the config change", options A "big-bang rollout" (consequence "faster
    but riskier") / B "staged rollout" (consequence "slower, lower operational
    risk"), a bounded context paragraph with the facts needed to decide,
    **NO producer recommendation**.
  - **P2 (cold-undecidable by construction):** question "Proceed with the
    approach we discussed earlier?", options yes/no **without consequences**,
    context empty — fails the cold-reader test.

## Flow

1. Seed rulebook + packets; capture ids (seeding order fixes P1/P2 — no
   mapping heuristics needed).
2. **Pass 1:** `attention-manager triage --once
   --bundle file://<repo>/bundles/triage.md --timeout 180`
   (synchronous, poll-free; exec budgeted ~500s — up to 2 packets × 2
   attempts of real LLM sessions; stdout captured to `triage-pass-1.out`).
3. Grade P1/P2 outcomes + events + ledger.
4. Answer P1 via CLI with the option **OPPOSITE** to triage's recommendation
   (forces an interesting rule_delta; the grader records which way).
5. **Pass 2:** same command → exactly ONE rule_delta record for P1 in
   `$ATTENTION_HOME/rulebook-proposals.jsonl`.
6. **Pass 3:** same command → record count for P1 unchanged (idempotency).
7. Branch: if the record is a proposal → `rulebook apply <id>` and assert the
   sentence landed under the target section in rulebook.md; if an explicit
   none-record → assert reason non-empty, PASS with branch noted.

## Grader assertions (hard — could-not-evaluate = FAIL)

| # | Assertion | Check |
|---|-----------|-------|
| 1 | `rulebook-seeded` | readback of rulebook.md contains the seed rule AND all five `## <section>` headings |
| 2 | `packets-seeded` | seeding one-liner exits 0 and prints two `pkt-` ids |
| 3 | `triage-pass-1-ok` | `triage --once` exits 0 |
| 4 | `p1-recommended-pending` | `pending/<P1>.json` exists; `triage.handled_by == "manager-recommend"`; `triage.why` non-empty; `triage.rule_refs` is a list; `recommendation.option` in {A, B} (producer gave none, so this is triage's) |
| 5 | `p2-bounced` | `bounced/<P2>.json` exists; `triage.why` contains a non-empty `bounce:` reason; `pending/<P2>.json` gone |
| 6 | `events-triage` | events.jsonl has exactly 1 `triage:recommended` (packet_id==P1) and exactly 1 `triage:bounced` (packet_id==P2); `triage:error` count recorded in detail |
| 7 | `ledger-triage` | ledger kinds include `triage_recommended` ×1 and `triage_bounced` ×1 |
| 8 | `answer-opposite-accepted` | `answer <P1> <opposite>` exits 0; detail records "recommended X → answered Y" |
| 9 | `triage-pass-2-ok` | second pass exits 0 |
| 10 | `one-rule-delta-record` | exactly ONE record for P1 in rulebook-proposals.jsonl — either a proposal {status=="proposed", sentence non-empty, section one of the five} or an explicit none-record {status=="none", reason non-empty}; detail records which branch |
| 11 | `third-pass-idempotent` | third pass exits 0 AND P1's record count still exactly 1 |
| 12 | `rulebook-apply-branch` | proposal branch: `rulebook apply <id>` exits 0 and the sentence appears under the target `## <section>` in rulebook.md (file read + section-scoped match); none branch: reason non-empty, PASS with `branch=none` noted |

Real-LLM caveat, graded honestly: if the LLM bounces P1 or recommends P2,
assertions 4/5 FAIL — that is a genuine cold-triage quality finding on
packets constructed to be clearly decidable/undecidable, not a grader gap.

## Budgets

Per-scenario hard timeout: **600s**. `triage --once` execs get a ~500s local
budget each (bounded by the scenario deadline); per-session LLM timeout 180s
via `--timeout` (runner retries once per packet, logged as `triage:error`).
Pass 3 runs no LLM sessions when idempotency holds.

## Artifacts

`rulebook-before.md`, `rulebook-after.md`, P1/P2 packet JSONs at each graded
stage (`p1-pending.json`, `p1-answered.json`, `p2-bounced.json`),
`rulebook-proposals.jsonl` copy, `events.jsonl` copy, `ledger.jsonl` copy,
`triage-pass-{1,2,3}.out` (CLI stdout), `triage-sessions.log` (concatenated
per-packet session logs + verdict JSONs from `$ATTENTION_HOME/triage/`),
`queue_snapshots.jsonl`, `harness.log` — collected even on early bail.
