# Attention Manager — Design

**Status:** Draft v1.1 (bundle-design-expert review applied; ready for behavioral modeling)
**Date:** 2026-07-26
**Owner:** bkrabach
**Workspace:** `better-attention`

---

## Problem

Source: Matt Whetton, *"The coding was the recovery. We just never called it that."*

Agent orchestration removed the zone-2 work (mechanical coding = hidden recovery) and left
an all-intervals workday. Three specific failures:

1. **All peaks, no valleys** — what remains is judgment calls, reviews, briefings, corrections;
   every one is a hill sprint.
2. **Expensive switches** — every agent interrupt evicts held context (~20 min full re-entry
   cost, Gloria Mark / UC Irvine); the day becomes made of interruptions.
3. **No finish lines** — everything stays in flight; zero loops close by your own hand; the
   missing completion is where the irritability comes from.

Today the human manages agent attention. The goal is a system that manages human attention
instead (this is also the stated goal of the Resolvers workstream).

## The core insight

Two methods attack this from opposite ends, and we need both:

- **Whetton's end (manage what reaches you):** escalations arrive as well-formed re-entry
  packets, batched to your schedule, with loops that visibly close.
- **Sumner's end (prevent escalations from existing):** fix the *process* that produced the
  escalation, not the instance. "Fix the sentence in the rules that let it through, and the
  next hundred files come back right."

The fusion is the killer feature: **a packet queue that shrinks over time.** A queue that
learns is a firewall; a queue that doesn't is just deferred interrupts.

## Design goals

1. A workday with 3+ sessions in flight where interrupts arrive only at genuine
   human-decision points, batched on the human's schedule.
2. Every escalation is answerable cold — from the packet alone — in minutes, not via a
   20-minute context rebuild.
3. Loops close visibly (mechanical finish lines), including a tactile/at-a-glance surface.
4. Every human answer compounds: answered once → rule added → same class never asked again.
5. The human can hop directly into any session at any time; the manager never fights them
   for control.

## Non-goals (v1)

- No new UI framework or canvas app (AMCC canvas is v2+; "generate UI after the data exists").
- No custom orchestrator module (see Decisions).
- No cross-machine federation, no multi-user.
- Not a general workflow engine — work-unit graphs are attractor pipelines, already built.

---

## Architecture — three tiers

```
┌─────────────────────────────────────────────────────────────┐
│ TIER 3: SURFACES                                             │
│  muxplex (hop-in browser/PWA) · Stream Deck (director-deck)  │
│  ntfy/desktop notify · daily ledger · [v2: AMCC canvas]      │
└──────────────────────────┬──────────────────────────────────┘
                           │ packets, finish lines, bells
┌──────────────────────────┴──────────────────────────────────┐
│ TIER 2: ESCALATION BUS                                       │
│  Packet queue (files on disk) · Rulebook · Cold triage       │
│  Producers: tool-request-decision (decisions) ·              │
│  ApprovalProvider (permission gates) · attractor             │
│  Interviewer.async_ask · recipe approval gates               │
└──────────────────────────┬──────────────────────────────────┘
                           │ spawn, observe, answer, resume
┌──────────────────────────┴──────────────────────────────────┐
│ TIER 1: MANAGER (the clock)                                  │
│  Embedded-foundation Python supervisor (loop, not graph)     │
│  spawns workers into muxplex sessions · tails events.jsonl   │
│  judges finish lines · edits rulebook · batches escalations  │
└─────────────────────────────────────────────────────────────┘
```

### Tier 1 — The Manager (the clock)

A persistent Python supervisor. **Two distinct process models — don't conflate them:**

- **Workers are separate interactive CLI processes** launched into muxplex-managed tmux
  sessions (naming convention `am-*`; also the muxplex input-allowlist boundary). This is
  what makes hop-in possible. Because they're separate processes, the manager cannot hand
  them an in-process ApprovalProvider — the packet modules ship as a **behavior composed
  into the worker bundle** (`behaviors/packet-escalation.yaml`), writing to the shared
  disk queue.
- **Embedded foundation** (`load_bundle → prepare() → create_session()/spawn()` per
  `foundation:docs/APPLICATION_INTEGRATION_GUIDE.md`) is for the **manager's own LLM
  work**: triage passes, rulebook edits, packet-quality bounces.

It owns:

- **Worker lifecycle** — launches worker CLI sessions into `am-*` tmux sessions with the
  packet-escalation behavior composed in.
- **Observation** — tails per-session `events.jsonl` (`session:start/end`, `tool:post`,
  packet events the bus modules emit); `parent_id` linking reconstructs the session tree.
  Tailing is the **primary** observation mechanism (log-viewer precedent), not a fallback;
  requires hooks-logging in every worker bundle (foundation provides it) and tolerance for
  partial lines.
- **Triage** — answers routine gates itself (cold, from packet + rulebook only), forwards
  genuine ones to the human queue with a "why".
- **Finish lines** — closes a loop only when the work unit's judge passes; emits
  `loop:closed`; appends to the daily ledger; pushes notify.
- **Rulebook maintenance** — after every human answer/correction, derives the rule edit
  that would have prevented the escalation (see Rulebook contract).

The manager is a **loop, not a graph** — supervision is open-ended; graphs need known
shape. Work *units* are graphs (attractor); the manager holds the clock.

**The human is the build daemon.** Sumner serialized cargo behind a single daemon because
it was the expensive operation; here the expensive operation is the human. Workers never
page the human directly — they write packets; the manager batches and presents on the
human's schedule. Match check frequency to check cost: the human is the costliest check,
so it runs least often, in batches.

### Tier 2 — The Escalation Bus

**One packet standard, four producers, one queue.**

Two escalation types with different return channels — the review caught that the kernel's
`ApprovalResponse` is `{approved: bool, reason, remember}`: **permission-shaped, not
decision-shaped**. A multi-option answer ("B") cannot flow back through it. So:

Producers:
1. **`tool-request-decision`** (new tool module) — the primary producer for **decision
   requests** (multi-option, the packet schema's central case). Workers call it; it writes
   the packet, awaits resolution, and returns the chosen option + rationale as its
   ToolResult (tool results carry arbitrary text; approval responses don't).
2. **Custom `ApprovalProvider`** (kernel protocol: `request_approval(ApprovalRequest) →
   ApprovalResponse`) — fork of `hooks-approval`, scoped to **permission gates only**
   (binary allow/deny from hook `ask_user`). Serializes a packet instead of blocking a
   console; maps the packet's `on_timeout` declaration onto `approval_timeout` /
   `approval_default` explicitly (kernel default is 300s → deny, which would silently
   violate fail-loud if left unmapped).
3. **Attractor `Interviewer.async_ask`** — hexagon gates publish to the same queue. A
   hexagon gate already *is* a re-entry packet: node prompt = question, outgoing edge
   labels = enumerated options, `[A]`/`[R]` accelerators = affordances. The engine already
   prefers `async_ask` when present; answers flow back natively.
4. **Recipe approval gates** — bridged by an adapter in the manager that polls the recipes
   `approvals` operation (don't patch the recipes tool); answers return via
   `approve`/`deny` + `{{_approval_message}}`.

**Durability caveat (from review):** a parked await inside a worker turn is not durable —
kill the worker mid-block and that turn is lost. The *queue* survives anything; blocked
turns are recovered by `session resume` + re-driving from the packet's `links.resume`.

Queue = files on disk (`queue/pending/`, `queue/answered/`, `queue/auto/`). Rebuilt from
the filesystem every scan; nothing about the queue lives in a conversation. Kill the
manager at 60%, restart, it resumes at 60% because the disk remembers.

### Tier 3 — Surfaces

- **muxplex** (hop-in): dual human/agent access is its design center. Human uses
  browser/PWA; manager uses the agent API. Bells + `sort=attention` are the existing
  needs-attention primitives; packets set bells.
- **Stream Deck** (`director-deck`, already a local bundle in settings): live session
  previews on keys; a packet lands = key shows the ask; a loop closes = **key goes green**
  (the tactile finish line — the article's missing dopamine, on a button).
- **notify** (desktop + ntfy, already enabled in settings): packet-batch arrival and
  finish lines. Notifications announce *batches*, not individual packets.
- **Daily ledger**: end-of-day artifact — loops closed (by name), packets answered, rules
  added, escalations auto-handled. This is the closure ritual.

---

## The Packet Schema (the load-bearing artifact)

A packet must pass the **cold-reader test**: an agent (or the human) with *nothing but the
packet and the rulebook* must be able to make the decision. If a cold reader can't decide,
the packet is malformed — bounce it back to the producing worker to enrich. This is
Whetton's "re-entry requirements" made mechanical, and it is enforced structurally because
triage itself reads cold (see Triage).

```yaml
packet:
  id: pkt-2026-07-26-0042          # sortable, unique
  created_at: 2026-07-26T16:20:00Z
  source:
    session_id: <amplifier session id>
    kind: hook-ask_user | attractor-hexagon | recipe-gate | a2a
    work_unit: <name, if part of a dispatched unit>
    muxplex_session: am-portfix-3   # hop-in target
  question: >                       # ONE decision, one sentence
    Migrate the config parser to the new schema now, or keep compat shim?
  options:                          # enumerated; from edge labels where applicable
    - id: A
      label: Migrate now
      consequence: breaks two downstream repos until they update
    - id: B
      label: Keep shim one more release
      consequence: carries known-buggy path for ~2 weeks
  recommendation:
    option: B
    rationale: downstream repos have no owner available this week
    confidence: medium
  context: |                        # BOUNDED decision material, inline
    <the minimal facts needed to decide — not a transcript>
  links:
    resume: amplifier session resume <id>
    files: [path1, path2]
  urgency:
    tier: batch | today | now       # "now" is rare and must justify itself
    deadline: 2026-07-27T00:00:00Z  # optional
    on_timeout:                     # EXPLICIT, declared — never a silent default
      action: apply-option | fail-loud
      option: B
  triage:                           # filled by manager
    handled_by: manager | human
    rule_refs: [rulebook §3.2]
    why: <one-line reasoning, always logged>
  resolution:                       # filled at answer time
    answer: B
    answered_by: human | manager
    answered_at: ...
    rule_delta: >                   # THE COMPOUNDING FIELD — the rulebook edit
      Added §3.4: "Prefer compat shims when downstream owners are unavailable
      within the release window."
```

**Fail-loud principle:** no silent fallbacks anywhere in the bus. If triage can't decide,
the packet surfaces to the human with why. Timeouts execute only *declared* defaults;
absent a declaration, the work unit fails loudly and the packet stays pending. A worker
proceeding in a "lesser" state is a bug.

## The Rulebook Contract

One file (`rulebook.md`), structured in sections, read by every triage pass and every
worker at start. Modeled on the attention firewall's self-updating notes — generalized
from notifications to decisions.

- **Sections:** attention priorities · auto-answer rules (what the manager may decide
  itself, with bounds) · escalation thresholds · edge cases · "when you cannot proceed."
- **It grows:** every human answer/correction produces a `rule_delta` — the sentence that
  would have prevented the escalation. The manager proposes it; early on the human
  approves rulebook edits (see Trust).
- **Nothing bypasses it:** correcting a triage decision without a rulebook edit = two
  sources of truth, one of them in your head. The manager's post-answer step always asks
  "what rule change does this answer imply?" — even if the answer is "none, genuinely
  one-off" (logged as such).
- **Disagreement = ambiguity:** if triage and human disagree, or a rule is cited in 3+
  packets, that's one badly written rule, not N problems. Rewrite the sentence.
- **Placement (from review):** @mention the rulebook in the **triage agent's** `.md`
  (context sink; the system-prompt factory re-reads from disk each turn, so freshness is
  free). Inject into workers at spawn via instruction. Do **not** put it in a behavior
  `context.include` — it grows, and >1,000 tokens there is a policy error.
- **Token budget:** the rulebook has an explicit cap (start: ~2,000 tokens). Hitting the
  cap forces consolidation — which is the "3+ citations = one badly written rule" pass
  anyway. A rulebook that only grows becomes context poisoning.

## The Judge Requirement

"An agent without an exit condition stops when it feels done, which is not a condition,
it is a mood."

- Every **autonomously dispatched** work unit ships a judge: a command with exit 0/1 +
  printed reason (wraps tests, schema checks, or an LLM yes/no where mechanical checks
  can't reach).
- **No judge → no autonomous dispatch.** It runs as an interactive session the human hops
  into via muxplex instead. (Per the graph-engineering caveat: exploratory work is
  loop-shaped; don't fake a judge.)
- **Judges are broken-tested before batch dispatch:** verify the judge FAILs on a
  deliberately broken artifact and PASSes on a known-good one. A judge that never fails is
  decoration, and every green light after it is meaningless.
- A loop **closes** only when the judge passes. Finish lines are honest by construction.

## Triage — cold, graduated trust

- **Cold:** the triage agent reads **packet + rulebook only** — never the worker's
  context. "A reviewer sharing the writer's context always agrees with the writer."
  Cold triage doubles as packet validation: can't decide cold = malformed packet, bounce.
- **Graduated trust** (the attention-firewall pattern, promoted from notifications to
  decisions):
  - Phase 1: manager *recommends* on every packet; human answers everything; every
    manager decision carries a logged "why."
  - Phase 2: manager auto-answers rule-covered packets; human reviews the auto log
    (`queue/auto/`) at their convenience; rejected/auto-handled items stay visible for
    calibration.
  - Phase 3: human spot-checks. Autonomy is earned per rule section, not granted globally.
- **Phase transitions are operational, not vibes** (gap flagged by behavioral model): a
  rule *section* graduates Phase 1 → 2 after **5 consecutive** human answers matching the
  triage recommendation with zero overrides in that section; any human override demotes
  the section back one phase immediately. Promotion state lives in the rulebook section
  header (visible, auditable), never in memory.
- **Triage agent tool surface** (gap flagged by behavioral model): file read/write scoped
  to the queue directory + rulebook — nothing else (no bash, no web, no delegation). Exit
  condition per pass: every pending packet has triage fields filled, or is bounced with a
  why. Anything it can't do with that surface is, by definition, a human escalation.
- **Permission discipline:** narrow write permissions first; expand as judgment is
  validated. Gates fail closed.

## Worker isolation

Parallel workers get separate ground to stand on: one muxplex tmux session each (`am-*`),
one git worktree (or DTU) each for code work. Sumner's agents collided until split across
worktrees; ours don't get the chance.

## Model routing

Per Sumner/Krieger and the existing routing matrix: workers on cheap/fast models where the
work allows; **the big model goes to triage and rulebook writing** — "a bad rule
propagates into every downstream output, which is exactly where capability is worth
paying for."

---

## Decisions (with reasons)

| # | Decision | Reason |
|---|---|---|
| D1 | **No custom orchestrator.** | Human-input handling is not the orchestrator's job — it flows hook `ask_user` → `ApprovalProvider` (kernel protocol) → app layer. A packet-writing provider works with every existing orchestrator unchanged. Fails the two-implementation rule today. Revisit only for mid-loop pause/checkpoint semantics hooks can't express. |
| D2 | **Workers = separate CLI processes in muxplex tmux; embedded foundation = the manager's own LLM work only.** | Hop-in requires workers to be interactive processes in tmux panes — so the manager cannot register an in-process provider for them; the packet modules ship as a behavior composed into worker bundles, and events.jsonl tailing is the primary observation channel. Embedded foundation (`PreparedBundle`) powers the manager's triage/rulebook sessions. amplifierd (REST+SSE) is the attach point for a web layer later; amplifier-agent envelopes for disposable turns. |
| D3 | **Attractor = work-unit format, not the brain.** | Five shipped-engine blockers for a supervisor: no resume-from-checkpoint, step bound (nodes × 50), no event ingress, single cursor, blocking gates. As work units they're excellent: hexagon gates force *declared* escalation points; `Interviewer.async_ask` is the escalation bus hook; file-state self-skip (`12-graph-resume.dot`) gives disk-resume. Avoid nested manager→child→hexagon gates (example 11 is a documented failing fixture). |
| D4 | **Muxplex = hop-in layer; manager avoids `/connect` and `PATCH /api/state`.** | Those move the *human's* view (server-global active session, last-writer-wins). Manager reads `/api/view` (cheap, built for polling) not full snapshots at high frequency; input via `POST /api/sessions/{name}/input` under the `am-*` allowlist (default-closed, audit-logged, read-back verify). |
| D5 | **State on disk everywhere.** | Queue, packets, rulebook, ledger are files. Resumable by construction; kill at 60% → resume at 60%. No queue state in any context window. |
| D6 | **Blocked-on-human is owned, not inferred.** | We control the ApprovalProvider — the moment of blockage is our code; we emit `packet:created` ourselves. Tailing events.jsonl is the fallback for sessions we didn't launch. |
| D7 | **Fail loud, no fallbacks.** | Undecidable triage surfaces with why. Undeclared timeout = pending + loud, never a quiet default. No synthetic "answers." |

## Reuse map (don't rebuild)

| Asset | Role here |
|---|---|
| `hooks-approval` | Fork → packet-writing ApprovalProvider |
| `payneio/amplifier-bundle-foreman` | Closest prior art (assistant managing a fleet); study before writing the supervisor — possible starting fork |
| `amplifier-bundle-orchestration` / `observers` | Spawn/trigger/observer primitives |
| `bkrabach/amplifier-bundle-attention-firewall` | Triage + graduated trust + self-updating rules pattern (fork the calibration loop) |
| `notify` bundle (desktop + ntfy, already configured) | Delivery channel |
| recipes approval queue | Precedent + producer #3 |
| `bkrabach/muxplex` + `muxplex-deck` / `director-deck` | Tier 3 surfaces, proven end-to-end |
| Attractor examples | `08-human-gate.dot`, `conversational-gate.dot`, `12-graph-resume.dot`, `09-manager-supervisor.dot` |
| AMCC concept doc (Team Pulse) | v2 canvas design vocabulary — do not rewrite |

## Packaging (from review — BUNDLE_GUIDE app + bundle hybrid, one repo)

```
attention-manager/
├── pyproject.toml                  # uv-tool installable CLI
├── src/attention_manager/         # APP: supervisor loop, queue lib, triage runner,
│                                   #   muxplex client, ledger, attractor async_ask impl,
│                                   #   recipe-gate poller (polls `approvals`; no patching)
├── modules/
│   ├── hooks-packet-approval/      # MODULE: ApprovalProvider fork (permission gates)
│   └── tool-request-decision/      # MODULE: decision-request tool (producer #1)
├── behaviors/
│   └── packet-escalation.yaml      # BEHAVIOR: both modules + thin packet-conventions
│                                   #   pointer — composed into every worker bundle
├── agents/
│   └── triage.md                   # AGENT: model_role: [reasoning, general];
│                                   #   @mentions rulebook + packet schema
└── context/
    ├── packet-schema.md            # conventions (the schema above)
    └── judge-contract.md           # judge requirements + broken-test protocol
```

## Parked (named, with reasons)

1. **Manager authoring its own workflows** (the true "dynamic workflows" analog — noticing
   a repeating work shape and generating the graph + rulebook for it). The compounding
   endgame; onboard via the middle path (interactive first, corrections become the first
   rulebook, graph the second run). Needs the queue-that-learns to exist first. **v2.**
2. **AMCC canvas UI.** "Generate UI after the data exists" — the queue generates the data. **v2.**
3. **muxplex SSE push.** `/api/view` polling is cheap and purpose-built; add push only if
   2s latency actually hurts. **Park.**
4. **Two cold reviewers + disagreement resolution per work unit.** Adopt per-unit when
   stakes justify; not day-one machinery. **Park.**
5. **A2A ingress** (peers/other machines message the manager; `defer` is the "not now"
   primitive). Natural fit, not on the critical path. **v1.5.**
6. **Senior triage-agent hierarchy** (Whetton's experiment d). The rulebook + cold triage
   IS the triage layer; a hierarchy of them is premature. **Park.**

## Metrics & proof gate

**Metrics (all from queue/ledger files — the queue generates its own telemetry):**
- **Escalations per work unit — must FALL week over week.** The measurable difference
  between "attention firewall for sessions" and "snooze button."
- Time-to-re-entry per packet (created → answered), split by tier.
- Loops closed per day (ledger), visible to the human.
- Auto-triage precision (spot-check of `queue/auto/` why-log).

**V1 proof gate — "the Tuesday test":** one real workday, not a demo:
1. 3+ workers in flight in muxplex; human hops into one mid-run via browser; manager
   doesn't fight for the view.
2. A worker blocks → packet lands in queue (+ bell + deck + batched notify); human answers
   it an hour later cold, from the packet alone; worker resumes.
3. Kill the manager at 60% of the day; restart; the queue, rulebook, and ledger are
   intact and pending packets still answerable. (Scope honestly: a worker turn that was
   mid-block when *the worker* died is not durable — it is re-driven via `session resume`
   from the packet's `links.resume`. The queue survives; blocked turns are re-driven.)
4. One human answer produces a `rule_delta`; the same class of escalation is auto-handled
   next occurrence (with logged why).
5. End of day: ledger shows loops closed by name — and the judge, not the worker's mood,
   closed them.

## Build order (plumbing first — every step ends runnable)

1. **Packet pipe in miniature:** packet schema + file queue + packet-writing
   ApprovalProvider (fork hooks-approval). One worker blocks → packet lands → answered via
   CLI → worker resumes. *(This is the whole system in miniature; prove it before anything
   else.)*
2. **Supervisor loop:** embed foundation; spawn 2 workers into muxplex `am-*` sessions;
   tail events.jsonl; emit `packet:created` / `loop:closed`; ntfy batches.
3. **Rulebook + cold triage:** triage pass (recommend-only, Phase 1), why-log,
   `rule_delta` capture after each human answer.
4. **Finish lines:** judge contract + broken-test step + judge-gated loop closure + daily
   ledger; deck key goes green.
5. **Attractor producer:** `Interviewer.async_ask` implementation publishing to the queue;
   dispatch first graph-shaped work unit with declared hexagon gates.
6. Graduated trust Phase 2 (auto-answer + auto-log), then parked/v1.5 items as earned.

## Open questions

1. Manager state home: `~/.amplifier/attention/` vs project-local? (Leaning user-global —
   sessions span projects.)
2. Packet schema versioning; exact `on_timeout` → `approval_timeout`/`approval_default`
   mapping for the permission-gate path (resolve during build step 1 against the real
   protocol — named as Issue 3 in review).
3. Rulebook edit approval UX in Phase 1 — inline in packet resolution, or batched review?
4. Naming. "Attention Manager" is the working name; the firewall precedent suggests
   "attention-director" / relationship to `director-deck`.

## Appendix: Mechanisms (input for spec-to-behavioral-model)

| Kind | Name | Purpose |
|---|---|---|
| Module (tool) | `tool-request-decision` | Worker-callable decision escalation: writes packet, awaits resolution, returns chosen option + rationale as ToolResult. Producer #1. |
| Module (hook) | `hooks-packet-approval` | ApprovalProvider fork of `hooks-approval`; permission gates only (binary); writes packet instead of console-blocking; explicit timeout mapping. Producer #2. |
| Behavior | `behaviors/packet-escalation.yaml` | Composes both modules + thin packet-conventions pointer into worker bundles. |
| Agent | `agents/triage.md` (`model_role: [reasoning, general]`) | Cold triage: reads packet + rulebook only; recommends or (Phase 2+) auto-answers; logs why + rule_refs; proposes rule_delta. @mentions rulebook + packet schema. |
| Context | `context/packet-schema.md` | Packet conventions + cold-reader test. |
| Context | `context/judge-contract.md` | Judge requirements (exit 0/1 + why) + broken-test protocol. Consumers: the **manager** (finish-line evaluation) and the **dispatch instruction** for autonomous work units; never always-on in worker context. |
| Context (runtime, user data) | `rulebook.md` | Self-growing triage rules; token-capped; @mentioned by triage agent, injected into workers at spawn. Not in behavior `context.include`. |
| App | `attention-manager` CLI (uv tool) | Supervisor loop, queue lib, ledger, muxplex client, attractor `Interviewer.async_ask` impl (producer #3), recipe-gate poller (producer #4), embedded-foundation triage/rulebook sessions. |
| External (reused) | muxplex agent API, `notify`, hooks-logging/events.jsonl, recipes `approvals`, attractor engine, foreman/orchestration/observers (study/fork) | See Reuse map. |

## Sources synthesized

- Whetton, *The coding was the recovery* (problem framing; re-entry requirements; tasks
  not interrupts; triage layer instinct)
- Sumner Bun port + Yarchi *Graph Engineering* guide + Anthropic *dynamic workflows*
  (rulebook; judge-first; cold reviewers; state on disk; check-cost placement; serialize
  the expensive op; models by role)
- Team Pulse: *Attention-Managed Command Center* concept (design vocabulary, v2 canvas);
  attention-firewall live instantiation (graduated trust, why-logs, self-updating rules)
- git-ops recon of `bkrabach/muxplex` + `muxplex-deck` (agent API, input allowlist,
  bells, polling economics, server-global view gotcha)
- amplifier-expert mechanism map (ApprovalProvider flow, D1/D2/D6; reuse map)
- attractor-expert fit assessment (D3; Interviewer bus; resume pattern; example 11 caveat)
