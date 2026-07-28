# Scenario 9 — Recipe-gate bridge (step 6: producer #4, with auto-resume)

**What this proves:** the recipe-gate poller bridges a REAL staged recipe's
approval gate into the packet queue and closes the loop WITHOUT the human —
end to end against the real recipes tool: `execute` pauses at the gate,
`recipes poll --once` packetizes it (kind=recipe-gate, options exactly
approve/deny), the CLI answer is forwarded via `operation=approve` with the
rationale as the approval message, the poller then AUTO-LAUNCHES the resume
(fire-and-forget background subprocess), the recipe completes, and dedupe
holds across polls — for both the packet AND the resume launch.

References: `docs/designs/attention-manager.md` §Tier 2 producer #4 / D9,
`src/attention_manager/recipe_gates.py` (incl. `_launch_resume`),
`evals/recipes/gate-recipe.yaml` (minimal purpose-built staged recipe — the
recipes bundle's staged examples all drive LLM agents; this one is two bash
echoes with one gate; validated with the real recipes tool:
`{'status': 'valid', 'recipe': 'eval-gate-recipe', 'version': '1.0.0',
'warnings': []}`), `scripts/local_trust_smoke.sh` part 4 (the local judge
with a fake amplifier).

## Verified real mechanics (probed against the real recipes tool locally)

1. `amplifier tool invoke recipes operation=execute recipe_path=… -o json`
   is SYNCHRONOUS: it returns `{'status': 'paused_for_approval',
   'session_id': …, 'stage_name': 'before-gate', …}` — no long-running
   process blocks at the gate.
2. `operation=approvals` lists the pending gate.
3. `operation=approve session_id=… stage_name=… message=…` marks the stage
   approved but does **NOT** resume: the real response says *"Use 'resume'
   operation to continue execution."*
4. **The poller now owns the resume** (`_launch_resume`, step-6 decision):
   after a forwarded approve it launches `operation=resume session_id=…` as
   a DETACHED background subprocess (`start_new_session=True` — survives the
   poller exiting), stdout+stderr to
   `<home>/recipe-gates/<session_id>.resume.log`, emits
   `recipe_gates:resume_launched` (event) + `recipe_gate_resume_launched`
   (ledger), and records `resume_launched_at` on the gate in
   `recipe-gates.json` — **idempotent: never launched twice per gate**.
   Deny needs no resume. v1 honesty: the poller does NOT track resume
   completion — the log + event are its only observability.
5. **Completion surface (verified locally):** `operation=list` returns
   `{'sessions': [{'session_id': …, 'recipe_name': …, 'started': …,
   'current_step_index': …, 'completed_steps': ['step-one', 'step-two']}],
   'count': N}` — there is NO status field; for THIS recipe, completion ==
   `"step-two"` (the after-gate step) present in the session's
   `completed_steps`. That is the surface the harness polls.

**cwd matters:** recipe sessions are project-scoped by working directory.
`execute`, every `recipes poll`, and the completion-polling `list` all run
from the SAME scenario workdir (`cd <workdir> && …`), matching
`RecipeGatePoller`'s cwd-based invoke (the auto-resume inherits the
poller's cwd).

## Deterministic — NO LLM

The recipe's steps are two bash echoes; the poller/CLI mechanics are code.
Each `amplifier tool invoke` (incl. the detached resume) carries bundle-prep
latency (~30-90s in a fresh DTU), hence the 600s hard timeout and the 120s
completion-poll budget.

## Honest env gating (assertion 1)

`amplifier tool invoke recipes` requires the recipes tool composed in the
DTU's default bundle. If the invoke fails because the tool is unavailable,
assertion 1 is graded FAIL `could-not-evaluate` **with the captured
stdout/stderr** — an honest environment failure, never softened (same
pattern as S7's missing-extra handling).

## Flow

1. `execute` the recipe (synchronous) from the workdir; parse the envelope;
   capture `session_id`.
2. `attention-manager recipes poll --once` (poll #1: packetize).
3. Grade the packet + `recipe_gates:packetized` event.
4. `attention-manager answer <pkt> approve --rationale eval`.
5. `recipes poll --once` (poll #2: forward + AUTO-RESUME launch) →
   `recipe_gates:resolved` + `recipe_gates:resume_launched` + resume log
   file exists.
6. Poll `operation=list` (≤120s) until the session's `completed_steps`
   includes `step-two`; capture the resume log tail.
7. `recipes poll --once` (poll #3: dedupe) → no new recipe-gate packet AND
   the `recipe_gates:resume_launched` event count is STILL exactly 1
   (`resume_launched_at` idempotency).
8. Grade ledger.

## Grader assertions (hard — could-not-evaluate = FAIL)

| # | Assertion | Check |
|---|-----------|-------|
| 1 | `recipe-executed-paused` | execute invoke exits 0; parsed result `status == "paused_for_approval"` with a `session_id` (tool unavailable ⇒ CNE with output captured) |
| 2 | `gate-packetized` | after poll #1: exactly 1 `kind=recipe-gate` packet pending; options exactly `[approve, deny]`; `source.work_unit == session_id`; `stage: before-gate` in context |
| 3 | `events-packetized` | `recipe_gates:packetized` event with the session_id + stage_name |
| 4 | `answer-approve-accepted` | `answer <pkt> approve --rationale eval` exits 0 |
| 5 | `forward-approve` | after poll #2: `recipe_gates:resolved` event with `answer == "approve"` |
| 6 | `resume-launched` | after poll #2: exactly 1 `recipe_gates:resume_launched` event (session_id match); ledger has `recipe_gate_resume_launched`; `<home>/recipe-gates/<session_id>.resume.log` exists |
| 7 | `recipe-completes` | polling `operation=list` (≤120s): the session's `completed_steps` includes `step-two` (verified surface — list has no status field); resume log tail captured in artifacts |
| 8 | `dedupe-no-second-packet-or-resume` | after poll #3: zero pending `kind=recipe-gate` packets AND `recipe_gates:resume_launched` count still exactly 1 |
| 9 | `ledger-recipe-gates` | ledger has `recipe_gate_packetized` ×1 + `recipe_gate_resolved` ×1 + `recipe_gate_resume_launched` ×1 |

## Budgets & artifacts

Hard timeout **600s**; each `amplifier tool invoke` / `recipes poll` exec
gets a generous local budget (≤240s) bounded by the scenario deadline;
completion polling ≤120s (each list invoke is itself expensive — the loop is
bounded by both the budget and the deadline).

Artifacts: `execute.out`, `poll-{1,2,3}.out`, `list-final.out`,
`resume.log` (copy of the poller-launched resume log),
`packet-pending.json`, `recipe-gates.json` copy (poller dedupe state incl.
`resume_launched_at`), `events.jsonl` + `ledger.jsonl` copies,
`queue_snapshots.jsonl`, `harness.log` — collected even on early bail.
