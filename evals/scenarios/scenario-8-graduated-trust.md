# Scenario 8 — Graduated trust: Phase-2 auto-answer + calibration demotion (step 6)

**What this proves:** with a rulebook section PRE-PROMOTED to Phase 2, a REAL
LLM triage pass auto-answers a rule-covered packet (canonical copy in
`answered/` with `answered_by: manager-auto`, review record in `queue/auto/`)
while a NOT-rule-covered control packet stays Phase-1 recommend-only — the
conservative bounds hold in both directions. Then `auto reject` records the
human correction and DEMOTES the section back to Phase 1, visibly, in the
rulebook heading annotation.

The streak-walk to promotion (5 consecutive matches → `trust:promoted`) is
deterministically proven by `scripts/local_trust_smoke.sh` with a fake
amplifier; this scenario seeds the promoted state directly and spends its
real-LLM budget on what only the DTU can test: whether a real verdict
actually clears the auto-answer bounds.

References: `docs/designs/attention-manager.md` §Triage (graduated trust),
`src/attention_manager/{trust,autolog}.py`, the auto-answer path in
`triage.py`, `rulebook.py` heading annotations, `context/packet-schema.md`
(auto/ review-record format), `scripts/local_trust_smoke.sh`.

## Setup

- Scenario-scoped `ATTENTION_QUEUE_DIR` + `ATTENTION_HOME`.
- **Rulebook seeded with a PRE-PROMOTED section**: the S5 template, but the
  Auto-answer rules heading carries the trust annotation —
  `## Auto-answer rules <!-- phase:2 streak:5 -->` — and contains the same
  operational-risk seed rule S5 uses. All other sections stay bare (Phase 1).
- **Two packets seeded** via the root queue lib (tier batch, no producer
  recommendation on either):
  - **P1 (rule-covered):** the S5-style rollout question — options A
    "big-bang rollout" (faster but riskier) / B "staged rollout" (slower,
    lower operational risk), bounded context. Clearly covered by the seed
    rule → auto-answer expected (rule-implied option: **B**).
  - **P2 (NOT rule-covered, control):** "Choose a name for the internal test
    fixture" — options fixture-a / fixture-b, bounded context giving a
    packet-local basis to decide (shortest-distinctive-name convention
    stated IN the packet, deliberately not a rulebook rule). Decidable cold,
    but no rulebook rule applies → must stay recommend-only.

## Flow

1. Seed rulebook + packets.
2. `attention-manager triage --once --bundle file://<repo>/bundles/triage.md
   --timeout 180` (real LLM; both packets in one pass).
3. Grade P1 auto-answered / P2 recommend-only / events / ledger.
4. `attention-manager auto reject <P1> --correct-option <the OTHER option>
   --reason eval` (opposite of the actual auto answer; recorded).
5. Grade review record + demotion + events.

## Grader assertions (hard — could-not-evaluate = FAIL)

| # | Assertion | Check |
|---|-----------|-------|
| 1 | `rulebook-seeded-prepromoted` | readback contains `## Auto-answer rules <!-- phase:2 streak:5 -->`, the seed rule, and all five section headings |
| 2 | `packets-seeded` | seeding prints two `pkt-` ids |
| 3 | `triage-pass-ok` | `triage --once` exits 0 |
| 4 | `p1-auto-answered` | `answered/<P1>.json` exists; `resolution.answered_by == "manager-auto"`; `resolution.answer == "B"` (the rule-implied option; actual recorded); `pending/<P1>.json` gone |
| 5 | `p1-auto-record` | `queue/auto/<P1>.json`: `reviewed == false`, `sections` includes `"Auto-answer rules"`, `answer` matches the resolution |
| 6 | `events-ledger-auto` | exactly 1 `triage:auto_answered` (P1) in events; ledger has `triage_auto_answered` |
| 7 | `p2-not-auto-answered` | `pending/<P2>.json` still exists with `triage.handled_by == "manager-recommend"` (NOT auto-answered, NOT bounced) |
| 8 | `auto-reject-accepted` | `auto reject <P1> --correct-option <other> --reason eval` exits 0 |
| 9 | `auto-record-reviewed` | record now `reviewed == true` with `review.action == "rejected"` and the correction recorded |
| 10 | `section-demoted` | rulebook.md contains `## Auto-answer rules <!-- phase:1 streak:0 -->` |
| 11 | `event-trust-demoted` | exactly 1 `trust:demoted` event (section Auto-answer rules, from_phase 2) |

## Honest bound note (assertion 4)

Auto-answer fires only when ALL conservative bounds hold: verdict decision
`recommend`, **confidence == `high`**, `rule_refs` non-empty with every ref
resolving to a Phase-2 section, and tier != `now`. If the real model returns
`medium` confidence for P1 (or cites no resolvable rule), the bound
legitimately blocks the auto-answer and P1 stays pending — the grader FAILs
assertion 4 **with the captured triage fields (incl. the confidence)** in the
detail. That outcome means our seeded rule/packet weren't clear enough for a
high-confidence verdict; the response is to tune the scenario, not the code.

## Budgets & artifacts

Hard timeout **600s** (one real-LLM pass over 2 packets + retries). Artifacts:
`rulebook-before.md` / `rulebook-after.md`, `p1-answered.json`,
`p1-auto-record.json` (before + after review via detail), `p2-pending.json`,
`events.jsonl` + `ledger.jsonl` copies, `triage-pass-1.out`,
`triage-sessions.log`, `queue_snapshots.jsonl`, `harness.log` — collected even
on early bail.
