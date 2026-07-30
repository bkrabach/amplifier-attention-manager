# Dogfood Quickstart — the Tuesday Test

Run a real workday through the attention manager. Not a demo: real work units,
real escalations, real answers. The proof gate is §Metrics & proof gate in
[`docs/designs/attention-manager.md`](designs/attention-manager.md) — one day
where the system manages your attention instead of the other way around.

**V1 scope: one machine per person.** One `$ATTENTION_HOME` per human. Each
home only adopts and reports the workers it dispatched (they have a
`workers/<session>/` dir under that home) — other homes' `am-*` tmux sessions
on the same server are ignored. Multiple humans sharing one machine/tmux
server is out of scope for v1.

## Terms (used throughout; one line each)

- **work unit** — one dispatched worker or one `workunit run` pipeline; one
  `dispatched`/`workunit_finished` ledger entry = one unit.
- **packet** — a JSON file (question + enumerated options + bounded context)
  a blocked worker writes to the queue; the unit of escalation. Contract:
  `context/packet-schema.md`.
- **triage** — the manager's cold pass over packets (packet + rulebook only):
  it pre-fills a recommendation, bounces malformed packets, and derives rule
  proposals from your answers.
- **judge** — a command gating a work unit's finish line: exit 0 = the work
  actually finished, nonzero = it didn't. Contract: `context/judge-contract.md`.
- **loop** — one work unit's full cycle dispatch → work → (escalations) →
  finish; it *closes* only when the judge passes (`loop:closed` / `loop:failed`).
- **rulebook** — `$ATTENTION_HOME/rulebook.md`; every human answer proposes
  the one-sentence rule that would have prevented that escalation.
- **bell** — the tmux window bell the manager rings on a worker session that
  needs you (escalation or failure); muxplex surfaces it, you clear it.

## Install

```bash
uv tool install 'amplifier-attention-manager[attractor] @ git+https://github.com/bkrabach/amplifier-attention-manager@main'
```

Requires tmux (fail-loud without it) and a working `amplifier` CLI on PATH
(triage and the recipe-gate bridge shell out to it). Verify with
`which attention-manager` (there is no `--version` flag yet).

## Start the manager (one terminal, leave it running)

```bash
attention-manager supervise --triage --recipes --notify ntfy:<your-ntfy-url> --interval 2
```

- The ntfy URL is yours to supply (e.g. `ntfy:https://ntfy.sh/<your-topic>`).
  No ntfy? Use `--notify file:/tmp/attention.jsonl` or `--notify console`.
- **Success looks like silence.** `supervise` prints NOTHING while healthy —
  errors go to stderr; everything else goes to disk. To confirm it's alive:
  run `attention-manager status` from another terminal (workers + pending
  packet count), or tail `$ATTENTION_HOME/events.jsonl` (every tick's
  observations land there; `supervisor:started` appears immediately).
- Bells are ON by default (`--no-bells` to disable): escalations and failed
  loops/workers ring the worker's tmux bell so muxplex surfaces them.
- State lives under `~/.amplifier/attention/` (override: `$ATTENTION_HOME`);
  the packet queue under `~/.amplifier/attention/queue` (override:
  `$ATTENTION_QUEUE_DIR`).

## Dispatch real work

Concrete, working example (this repo's test-worker bundle, fetched via git):

```bash
attention-manager dispatch portfix \
  --task "migrate the config parser to the new schema" \
  --bundle 'git+https://github.com/bkrabach/amplifier-attention-manager@main#subdirectory=bundles/test-worker.md' \
  --judge 'cd ~/repos/thing && python -m pytest -q'
```

- `--bundle` takes anything `amplifier run -B` accepts: a git URI like the
  one above, or a **registered bundle name** (if your amplifier environment
  already has the bundle installed, e.g. `--bundle attention-test-worker`).
  A local file path only works if your amplifier environment resolves it —
  when in doubt, use the git URI form.
- A worker that dies within ~3s of dispatch on a bundle/module load failure
  (or a nonzero exit) is reported LOUDLY by `dispatch` itself (nonzero exit +
  log path) — no more silent instant deaths.
- `--judge` gates the finish line: exit 0 → `loop:closed`, nonzero →
  `loop:failed` (loud + bell). No judge → the worker finishes unjudged —
  but an unjudged worker that dies with a nonzero exit is still loud
  (`WORKER FAILED` notification + bell + `worker_failed` ledger entry).
- Verify a judge before trusting it (the broken-test protocol):

  ```bash
  attention-manager judge verify --cmd 'grep -q DONE "$ARTIFACT"' --good good.txt --broken broken.txt
  ```

  `--cmd` runs via `bash -c` with `$ARTIFACT` set to the artifact path under
  test; it must PASS the known-good artifact AND FAIL the deliberately broken
  one. (The dispatch-time `--judge` command instead runs in the worker's dir
  with `WORKER_LOG`/`WORKER_EXIT` in its environment — a pytest judge like
  the example above needs no `$ARTIFACT`.)

## Making workers escalate (the NEEDS-HUMAN-DECISION protocol)

Workers don't escalate by themselves — two pieces make it happen:

1. **The worker bundle composes the packet-escalation behavior**
   (`behaviors/packet-escalation.yaml`), which provides the
   `request_decision` tool and the permission-gate provider. A plain bundle
   will just block or improvise instead of writing a packet.
   `bundles/test-worker.md` in this repo is the minimal working example.
2. **The worker's instructions tell it when to call `request_decision`.**
   The test-worker pattern: when a task contains a `NEEDS-HUMAN-DECISION`
   marker with enumerated options, the worker MUST call `request_decision`
   with those exact options plus its recommendation, block until the packet
   is answered, then proceed per the answer — never inventing an answer.

Task-prompt pattern (from `bundles/test-worker.md`):

```text
Create the status file. NEEDS-HUMAN-DECISION: should it be named
status-a.txt (option A) or status-b.txt (option B)? Escalate with your
recommendation and proceed per the answer.
```

## The muxplex view

Workers appear in muxplex automatically as `am-*` tmux sessions. When a worker
escalates (packet lands), a loop fails, or an unjudged worker dies, its
session bell rings — `sort=attention` floats it to the top, muxplex-deck shows
amber. You (or the muxplex UI) clear bells; the manager never does. Hop into
any session from the browser at any time — the manager won't fight you for
the view.

NOTE: muxplex views filter what surfaces. If your active view is a curated one
(not "all"), belled `am-*` sessions won't appear in it — add them to the view
you dogfood from (or use "all"). The bell state itself is always there
(host-verified: `GET /api/sessions` shows `bell.unseen_count`/`last_fired_at`
for `am-*` sessions the moment the manager rings).

**No muxplex? The CLI flow IS the flow.** The notify sink
(`--notify file:...` / `console` / `ntfy:`) announces packet batches and
finish lines, and `queue list` + `status` are the polling surfaces. Nothing
about answering packets requires muxplex.

Answer escalations on your schedule, cold, from the packet alone:

```bash
attention-manager queue list             # pending + bounced, with SOURCE column
attention-manager queue show <pkt-id>
attention-manager answer <pkt-id> B --rationale "downstream has no owner this week"
```

`queue list` also shows packets triage bounced (marked `BOUNCED`). Disagree
with a bounce? `answer <pkt-id> <option>` works on bounced packets too — the
human override reclaims the decision in one command.

## Watching progress

```bash
attention-manager status    # every worker this home dispatched + state + pending packet count
```

`status` covers only workers dispatched by THIS home. The full event stream
is `$ATTENTION_HOME/events.jsonl` (append-only JSONL: packet/worker/triage/
bell/loop events); the daily ledger is `$ATTENTION_HOME/ledger/<date>.jsonl`.

## Make answers compound (the queue that learns)

Every human answer gets a rule_delta pass — even answers given before triage
ever saw the packet ("what rule would have prevented this escalation?"):

```bash
attention-manager rulebook proposals   # rule deltas derived from your answers
attention-manager rulebook apply <id>  # the sentence that prevents the next escalation
attention-manager auto list            # Phase-2 auto-answers awaiting calibration review
attention-manager auto confirm <pkt-id>
attention-manager auto reject <pkt-id> --correct-option A --reason "..."
```

## End of day

```bash
attention-manager ledger --summary   # loops closed by name, packets, rules, latency
```

## The one metric that matters

**Escalations per work unit must FALL week over week.** That is the difference
between an attention firewall and a snooze button. `ledger --summary` computes
it: the `escalations/work-unit (healthy)` line shows this ISO week vs last,
counting only HEALTHY units — failed units (`loop_failed`/`worker_failed`) are
shown separately and excluded from the denominator, so failures can never
improve the number.

## Honest v1 rough edges

- **Workers don't escalate by themselves.** See "Making workers escalate"
  above — the packet-escalation behavior must be composed into every worker
  bundle.
- **Triage needs its bundle.** `--triage` uses the repo's `bundles/triage.md`
  by default (fetched via git); air-gapped hosts need `--triage-bundle`.
- Recipe-gate polling (`--recipes`) needs the `recipes` tool available in the
  `amplifier` environment it shells out to.
- **Every `amplifier tool invoke` costs one amplifier session** in the invoking
  project's session store (`~/.amplifier/projects/<slug>/sessions/`) — bundle
  prep + session creation happen even for pure tool code with no LLM. That is
  why recipe-gate DISCOVERY never invokes amplifier: the poller reads the
  recipes tool's persisted state from disk (observation-only) and idle polling
  costs zero subprocesses. Invokes happen only to forward a human's
  approve/deny and launch resume. (Pre-fix, invoke-based discovery at
  ~1 poll/10s created 1,820 junk sessions in 5.75 hours.) The default poll
  cadence is one per 30 ticks (60s at `--interval 2`) as defense-in-depth.
- A worker turn that dies mid-escalation is not durable — the packet is, and
  the worker is re-driven via the packet's `links.resume` (producers fill it
  with `amplifier session resume <session-id>` whenever the producing session
  id is known; it is producer-dependent otherwise).
