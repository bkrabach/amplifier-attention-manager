# Behavioral Model: attention-manager

> Generated from the mechanism spec extraction. This document describes expected runtime behavior derived from the spec ONLY — it does not describe implementation, and it flags where the spec is silent or ambiguous.

---

## 1. Overview

### Bundle Identity

| Field | Value |
|-------|-------|
| **Name** | `attention-manager` (working name; `attention-director` noted as possible rename — open question 4) |
| **Purpose** | Manage human attention instead of the human managing agent attention: workers escalate via well-formed re-entry packets to a disk-backed queue; a manager (persistent Python supervisor — a loop, not a graph) batches packets to the human's schedule, cold-triages them against a self-growing rulebook, closes loops only when judges pass, and compounds every human answer into a rule edit so the same class of escalation is never asked again. "A queue that learns is a firewall; a queue that doesn't is just deferred interrupts." |
| **Form** | uv-tool installable CLI app (`attention-manager/pyproject.toml`) + bundle assets (modules, behavior, agent, context files) |

### Dependencies (as stated in spec)

| Dependency | Role |
|------------|------|
| amplifier-foundation (embedded) | `load_bundle -> prepare() -> create_session()/spawn()` per `foundation:docs/APPLICATION_INTEGRATION_GUIDE.md`; powers the manager's own LLM work only |
| hooks-approval | Forked into `hooks-packet-approval` |
| hooks-logging | Required in every worker bundle for `events.jsonl` tailing; foundation provides it |
| bkrabach/muxplex + muxplex-deck / director-deck | Tier 3 hop-in and Stream Deck surfaces; agent API, `am-*` input allowlist, bells, `sort=attention` |
| notify bundle (desktop + ntfy) | Batch and finish-line notifications (already configured) |
| recipes bundle | `approvals` operation polled by an adapter in the manager; the recipes tool is not patched |
| attractor engine | Work-unit format; hexagon gates; `Interviewer.async_ask`; file-state self-skip resume |
| payneio/amplifier-bundle-foreman | Closest prior art; study before writing the supervisor — possible starting fork |
| amplifier-bundle-orchestration / observers | Spawn/trigger/observer primitives |
| bkrabach/amplifier-bundle-attention-firewall | Triage + graduated trust + self-updating rules pattern; fork the calibration loop |

### Component Inventory

| Type | Count | Names |
|------|-------|-------|
| Modes | 0 | — |
| Agents | 1 | `triage` |
| Skills | 0 | — |
| Recipes | 0 | — |
| Hooks | 1 | `hooks-packet-approval` |
| Tools | 1 | `tool-request-decision` |
| Behaviors | 1 | `behaviors/packet-escalation.yaml` |
| Context files | 3 | `context/packet-schema.md`, `context/judge-contract.md`, `rulebook.md` (runtime, user data) |
| App components | 1 | `attention-manager` CLI (supervisor loop, queue lib, triage runner, muxplex client, ledger, attractor `async_ask` impl, recipe-gate poller) |
| External reused systems | 8 | muxplex agent API (+ decks), notify, hooks-logging / events.jsonl, recipes approvals operation, attractor engine + examples, foreman/orchestration/observers bundles, attention-firewall bundle, AMCC concept doc (Team Pulse) |

---

## 2. Tool Governance

**The spec defines no modes, and therefore no per-mode tool availability matrix (safe/warn/confirm/block) and no `default_action`.** Governance in this bundle is expressed through different mechanisms:

| Governance surface | Mechanism | Source |
|--------------------|-----------|--------|
| Permission gates in workers | `hooks-packet-approval` (kernel ApprovalProvider protocol; binary allow/deny only) — gates **fail closed** | Hook definition; triage agent directive "gates fail closed" |
| Decision escalations in workers | `tool-request-decision` writes packet to disk queue, awaits resolution | Tool definition |
| Write permissions | "Permission discipline: narrow write permissions first; expand as judgment is validated" | Triage agent behavioral directive |
| Autonomy expansion | Earned per rule section (Phase 3), not granted globally | Triage agent operating modes |
| Timeout behavior | Packet's declared `on_timeout` is explicitly mapped onto `approval_timeout` / `approval_default`; the kernel default (300s -> deny) is never left unmapped because it would silently violate fail-loud | `hooks-packet-approval` behavior |
| muxplex input | POST input only under the `am-*` allowlist; avoids `/connect` and `PATCH /api/state`, which move the human's view | App component (muxplex client) |

**Limitation (flagged per instructions):** Delegation necessity and composition loopholes cannot be derived from this spec. They require the resolved transitive composition of every worker bundle that composes `behaviors/packet-escalation.yaml` (which tools workers inherit, what foundation contributes, whether spawned children can bypass packet escalation). The spec states direct dependencies only.

---

## 3. Mode Behaviors

**None.** The extraction contains an empty `modes` list — this bundle defines no runtime behavior overlays with tool policy tiers.

Note to avoid confusion: the spec's "Phase 1 / Phase 2 / Phase 3" are **operating modes of the `triage` agent's trust progression** (see Section 4), not Amplifier modes. They are behavioral phases of the system's autonomy calibration, not togglable session overlays.

---

## 4. Agent Behaviors

### `triage`

| Attribute | Value |
|-----------|-------|
| **Purpose** | Cold triage of escalation packets: reads packet + rulebook ONLY (never the worker's context); recommends (Phase 1) or auto-answers rule-covered packets (Phase 2+); forwards genuine escalations to the human queue with a "why"; logs why + `rule_refs` on every decision; proposes the `rule_delta` after each human answer; bounces malformed packets that cannot be decided cold. |
| **Model role** | `[reasoning, general]` — gets the big model: "a bad rule propagates into every downstream output, which is exactly where capability is worth paying for." |
| **Tool requirements** | Not stated in spec (empty in extraction). |
| **Exit conditions** | Not stated in spec (empty in extraction). |

**Trigger conditions:**

1. A packet lands in / is pending in the disk queue (triage pass, run by the manager).
2. After every human answer/correction — derive the rule edit (`rule_delta`) that would have prevented the escalation.

**Operating modes (trust progression):**

| Phase | Behavior |
|-------|----------|
| **Phase 1 (recommend-only)** | Manager recommends on every packet; human answers everything; every manager decision carries a logged "why". |
| **Phase 2 (auto-answer)** | Manager auto-answers rule-covered packets; human reviews the auto log (`queue/auto/`) at their convenience; rejected/auto-handled items stay visible for calibration. |
| **Phase 3 (spot-check)** | Human spot-checks; autonomy is earned per rule section, not granted globally. |

**Behavioral directives:**

- **Cold read**: packet + rulebook only, never the worker's context — "a reviewer sharing the writer's context always agrees with the writer."
- **Cold triage doubles as packet validation**: can't decide cold = malformed packet → bounce back to the producing worker to enrich.
- **Fail loud, no silent fallbacks**: if triage can't decide, the packet surfaces to the human with why; undeclared timeout = pending + loud, never a quiet default; no synthetic answers.
- Every decision logs a one-line "why" and `rule_refs` (always logged).
- Post-answer step always asks "what rule change does this answer imply?" — even "none, genuinely one-off" is logged as such.
- Answer routine gates itself (cold, from packet + rulebook only); forward genuine ones to the human queue with a why.
- **Permission discipline**: narrow write permissions first; expand as judgment is validated; gates fail closed.

**Context loading (context-sink pattern):**

- `@mentions rulebook.md` — system-prompt factory re-reads from disk each turn, so freshness is free.
- `@mentions context/packet-schema.md`.

---

## 5. Skill Behaviors

**None.** The extraction contains an empty `skills` list — this bundle defines no on-demand knowledge packages.

---

## 6. Context & Cross-Cutting Concerns

### Context Files

| File | Always loaded? | Role |
|------|---------------|------|
| `context/packet-schema.md` | No | Packet conventions: one decision per packet, enumerated options with consequences, recommendation with confidence, bounded inline context, resume links, urgency tier with EXPLICIT declared `on_timeout`, triage and resolution fields including `rule_delta`. Includes the **cold-reader test**: a cold reader with nothing but packet + rulebook must be able to decide, else the packet is malformed and bounced. Full schema @mentioned by the `triage` agent; a **thin packet-conventions pointer** is composed into worker bundles via the `packet-escalation` behavior. |
| `context/judge-contract.md` | No | Judge requirements: every autonomously dispatched work unit ships a judge (command with exit 0/1 + printed reason; wraps tests, schema checks, or an LLM yes/no where mechanical checks can't reach). **No judge → no autonomous dispatch** (runs as interactive session via muxplex instead). Includes the **broken-test protocol**: verify the judge FAILs on a deliberately broken artifact and PASSes on a known-good one before batch dispatch. A loop closes only when the judge passes. |
| `rulebook.md` (runtime, user data) | Yes (for triage + workers) | Self-growing triage rules in one file, structured in sections: attention priorities, auto-answer rules (with bounds), escalation thresholds, edge cases, "when you cannot proceed." Read by every triage pass and every worker at start. Grows via `rule_delta` from every human answer/correction; **nothing bypasses it** (correction without a rule edit = two sources of truth); triage/human disagreement or 3+ citations of one rule = one badly written rule, rewrite the sentence. |

### Token Budget

| Item | Estimate | Notes |
|------|----------|-------|
| `rulebook.md` | ~2,000 tokens (starting cap) | Token-capped; **hitting the cap forces consolidation**. |
| `context/packet-schema.md` | Not estimated in spec | Loaded only in triage agent's context (sink) + thin pointer in workers. |
| `context/judge-contract.md` | Not estimated in spec | Loading location not fully specified (see Section 9). |

**Rulebook placement (deliberate context architecture):**

- @mentioned in the `triage` agent's `.md` (context sink; per-turn disk re-read gives freshness for free).
- Injected into workers **at spawn via instruction** — not via always-on context.
- Explicitly **NOT** in behavior `context.include` (>1,000 tokens there is a policy error).

### Delegation Chains and Routing Patterns

The spec defines a hub-and-spoke topology centered on the **disk queue** (`queue/pending/`, `queue/answered/`, `queue/auto/` — rebuilt from filesystem every scan, no queue state in any context window, "kill at 60% → resume at 60%"):

| Producer | Path into queue | Answer path back |
|----------|----------------|------------------|
| #1 `tool-request-decision` (workers) | Tool writes packet, awaits | Chosen option + rationale returned as ToolResult (tool results carry arbitrary text; approval responses don't) |
| #2 `hooks-packet-approval` (workers) | Hook `ask_user` permission gate serializes packet | Binary allow/deny via kernel `ApprovalResponse` (permission-shaped, not decision-shaped) |
| #3 Attractor hexagon gates | Manager's `Interviewer.async_ask` implementation publishes to the same queue ("a hexagon gate already IS a re-entry packet") | Answers flow back natively |
| #4 Recipe approval gates | Manager's recipe-gate poller polls the recipes `approvals` operation | `approve`/`deny` + `{{_approval_message}}`; no patching the recipes tool |

**Consumer:** the manager's triage runner spawns the `triage` agent (embedded-foundation session) per pass. The human is the **build daemon**: workers never page the human directly; the manager batches and presents on the human's schedule — "the costliest check runs least often."

**Observation channel:** tailing per-session `events.jsonl` (`session:start/end`, `tool:post`, packet events) is the PRIMARY mechanism; `parent_id` linking reconstructs the session tree; tolerance for partial lines. `packet:created` is emitted by our own code — "blocked-on-human is owned, not inferred" (D6).

**Durability caveat (from hook spec):** a parked await inside a worker turn is not durable — the queue survives; blocked turns are re-driven via session resume from the packet's `links.resume`.

**Finish lines:** a loop closes only when the work unit's judge passes → emit `loop:closed`, append daily ledger (loops closed by name, packets answered, rules added, escalations auto-handled), push notify; Stream Deck key goes green ("the tactile finish line").

---

## 7. Recipe Workflows

**None defined by this bundle.** The extraction contains an empty `recipes` list.

The bundle does, however, **consume** external recipe workflows as an escalation producer: the manager's recipe-gate poller (producer #4) polls the recipes bundle's `approvals` operation and bridges pending staged-recipe approval gates into the packet queue. Answers return via the standard `approve`/`deny` path with `{{_approval_message}}`. The spec is explicit that the recipes tool is **not patched** — the bridge is an adapter in the manager.

---

## 8. Behavioral Scenarios

> Coverage note: the required scenario mix asks for one mode-driven interactive flow, but **this spec defines no modes** — a mode-driven scenario cannot be honestly generated. In its place, Scenario 1 covers the primary interactive flow (human-in-the-loop decision escalation), Scenario 3 covers the recipe-driven automated flow, and Scenarios 1/2/5 exercise the agent delegation chain (manager → triage agent).

### Scenario 1: Worker hits a decision point (Phase 1, recommend-only)

- **Trigger:** A worker (interactive CLI session in a muxplex `am-*` tmux session, with `packet-escalation` composed in) reaches a multi-option decision it cannot resolve from the rulebook injected at spawn.
- **Mechanism chain:**
  1. Worker calls `tool-request-decision` (producer #1) → packet written to `queue/pending/`, tool awaits.
  2. Manager's supervisor loop observes the packet (queue scan + `events.jsonl` tail).
  3. Manager runs a triage pass → spawns the `triage` agent (embedded-foundation session, `[reasoning, general]`).
  4. Triage cold-reads packet + rulebook only. Packet is decidable but not rule-covered → forwarded to the human queue with a recommendation + confidence + logged "why" and `rule_refs` (Phase 1: recommend on every packet).
  5. notify announces the **batch** (not the individual packet); Stream Deck key shows the ask.
  6. Human answers (batch review, on their schedule; optionally hop-in via muxplex).
  7. Answer flows back: tool returns chosen option + rationale as its ToolResult; worker resumes.
  8. Post-answer step: triage proposes the `rule_delta` that would have prevented the escalation; manager applies the rulebook edit.
- **Context flow:** Full packet schema + rulebook load only in the triage agent (context sink). Worker carries only the thin packet-conventions pointer + spawn-time rulebook injection. No queue state lives in any context window.
- **Expected outcome:** Decision resolved, worker unblocked, "why" logged, rulebook grew by one rule — this class of escalation should not be asked again.

### Scenario 2: Permission gate fires in a worker

- **Trigger:** A hook in a worker session raises `ask_user` for a permission gate (binary allow/deny).
- **Mechanism chain:**
  1. `hooks-packet-approval` (producer #2) receives `request_approval(ApprovalRequest)` via the kernel ApprovalProvider protocol.
  2. Instead of blocking a console, it serializes a packet to the shared disk queue and awaits resolution.
  3. The packet's declared `on_timeout` is explicitly mapped onto `approval_timeout` / `approval_default` — the kernel default (300s → deny) is never silently inherited (fail-loud).
  4. Manager triage: routine, rule-covered gates are answered cold (Phase 2+); genuine ones forwarded to the human with a why. Gates fail closed.
  5. Resolution maps back to a kernel `ApprovalResponse` (allow/deny — permission-shaped, multi-option answers cannot flow through this channel).
- **Context flow:** Cold triage sees packet + rulebook only. `packet:created` is emitted by our code (owned, not inferred).
- **Expected outcome:** Gate resolved allow/deny. **Durability caveat:** if the worker process dies while awaiting, the queue survives but the parked await does not — the blocked turn is re-driven via session resume from the packet's `links.resume`.

### Scenario 3: Staged recipe reaches an approval gate (recipe-driven automated flow)

- **Trigger:** An externally running staged recipe pauses at an approval gate.
- **Mechanism chain:**
  1. Manager's recipe-gate poller (producer #4) polls the recipes `approvals` operation and finds the pending gate.
  2. Poller adapter creates a packet in the shared queue — no patching of the recipes tool.
  3. Triage pass: rule-covered → auto-answer (Phase 2+, logged to `queue/auto/` for human calibration review); otherwise → human queue with a why.
  4. Resolution returns via `approve`/`deny`; any message rides `{{_approval_message}}` into subsequent recipe stages.
- **Context flow:** The recipe's own context is untouched; only the gate surfaces through the adapter. Triage remains cold (packet + rulebook).
- **Expected outcome:** Recipe resumes (or stops on deny) without the human being interrupted at gate-fire time; the answer is batched to the human's schedule.

### Scenario 4: Autonomous work unit reaches its finish line

- **Trigger:** An autonomously dispatched attractor work unit completes its artifact.
- **Mechanism chain:**
  1. Per `context/judge-contract.md`, the unit was only dispatched autonomously because it shipped a judge (exit 0/1 + printed reason), pre-verified by the broken-test protocol (FAILs on a deliberately broken artifact, PASSes on a known-good one). No judge → it would have run as an interactive muxplex session instead.
  2. Any hexagon gates hit mid-run published packets via the manager's `Interviewer.async_ask` implementation (producer #3) — answered through the same queue, flowing back natively. (Nested manager→child→hexagon gates are avoided; example 11 is a documented failing fixture.)
  3. Manager runs the judge. Pass → `loop:closed` emitted, daily ledger appended, notify pushed, Stream Deck key goes green. Fail → the loop does not close; failure surfaces loud (no silent fallbacks).
- **Context flow:** Judge is a command, not context; the manager's supervisor loop owns finish-line evaluation.
- **Expected outcome:** A loop closes only on judge pass — "closed" means verified, not merely finished.

### Scenario 5: Malformed packet bounce + rulebook consolidation

- **Trigger:** A triage pass encounters a packet that cannot be decided from packet + rulebook alone (fails the cold-reader test).
- **Mechanism chain:**
  1. Cold triage doubles as packet validation: undecidable-cold = malformed → bounced back to the producing worker to enrich (options, consequences, bounded inline context, resume links).
  2. Separately, calibration signals accumulate: triage/human disagreement, or 3+ citations of one rule, flags one badly written rule → the sentence is rewritten.
  3. If `rulebook.md` hits its ~2,000-token cap, consolidation is forced.
- **Context flow:** The bounce carries the "why" back to the worker; the rulebook edit happens in the manager's embedded-foundation session; freshness propagates automatically (per-turn disk re-read in triage; spawn-time injection for new workers).
- **Expected outcome:** Packet quality and rulebook quality are both self-correcting loops; the rulebook stays small, current, and singular (no second source of truth).

---

## 9. Spec-Derived Limitations

What CANNOT be determined from the spec alone:

1. **Transitive dependency tree.** The spec states direct dependencies only. What foundation, the recipes bundle, muxplex, and each worker bundle transitively pull in is unresolved.
2. **Composition loopholes.** With no modes and no resolved tool policies, it cannot be verified whether workers composed with `packet-escalation` retain tools that bypass the packet queue (e.g., direct writes, spawning children without the behavior). Requires resolved transitive composition of actual worker bundles.
3. **Delegation necessity map.** The `triage` agent's `tool_requirements` and `exit_conditions` are empty in the extraction; the full tool matrix needed to determine what triage can/cannot do without delegating is absent.
4. **Actual token counts.** Only `rulebook.md` carries an estimate (~2,000-token cap, a target not a measurement). `packet-schema.md` and `judge-contract.md` have no estimates; the per-turn floor for triage and workers cannot be computed.

Additional ambiguities and gaps flagged during synthesis (present in, or implied by, the extraction):

- **Bundle name unsettled** — `attention-manager` vs `attention-director` (open question 4).
- **`judge-contract.md` loading location** is not specified (packet-schema is explicitly @mentioned by triage; judge-contract's consumer is stated only implicitly via the manager's finish-line role).
- **No modes, no skills, no recipes** — the required behavioral-model sections for these are structurally empty; governance relies on hooks, permission discipline, and the phased-trust progression instead of mode tool policies.
- **Phase transition criteria** (when Phase 1 → 2 → 3 flips, and per which rule sections) are described directionally ("autonomy is earned per rule section") but not operationally defined.
- **Kernel `ApprovalResponse` constraint** is load-bearing: multi-option decisions MUST route through `tool-request-decision`, not the approval hook — the spec is explicit that approval responses are permission-shaped only.
