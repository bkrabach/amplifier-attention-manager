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

## Use from Amplifier sessions

Normal Amplifier sessions can drive this whole flow themselves. The repo
ships an owner-side bundle (root `bundle.md`) with an `attention-manager`
skill covering the dispatch flows, the goal-derived judge pattern,
redispatch-on-fail, and the verified footguns:

```bash
amplifier bundle add git+https://github.com/bkrabach/amplifier-attention-manager@main
```

Or compose just `behaviors/attention-manager.yaml` into your own bundle. The
session then loads the skill before driving the CLI:
`load_skill(skill_name="attention-manager")`.

**Caveat (validated):** the skills tool's `config.skills` lists REPLACE
across composed bundles — last wins. Composing multiple skill-shipping
bundles requires merging the lists in your own `tool-skills` config.

**Troubleshooting — "Not a valid bundle: missing bundle.md" (hit live):**
sessions that referenced this repo as a bundle source before the bundle
landed (commit `6829f37`) keep a stale shallow clone under
`~/.amplifier/cache/`. Refresh it:

```bash
d=$(ls -d ~/.amplifier/cache/amplifier-attention-manager-*) \
  && git -C "$d" fetch --depth 1 origin main \
  && git -C "$d" reset --hard origin/main
```

(or run `amplifier update`).

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
  --task "migrate the config parser to the new schema; print MIGRATION-COMPLETE as your final line when done" \
  --bundle 'git+https://github.com/bkrabach/amplifier-attention-manager@main#subdirectory=bundles/test-worker.md' \
  --judge 'grep MIGRATION-COMPLETE "$WORKER_LOG"'
```

- `--bundle` takes anything `amplifier run -B` accepts: a git URI like the
  one above, or a **registered bundle name** (if your amplifier environment
  already has the bundle installed, e.g. `--bundle attention-test-worker`).
  A local file path only works if your amplifier environment resolves it —
  when in doubt, use the git URI form.
- A worker that dies within ~3s of dispatch on a bundle/module load failure
  (or a nonzero exit) is reported LOUDLY by `dispatch` itself (nonzero exit +
  log path) — no more silent instant deaths.
- **Unattended preamble (automatic).** The default composed worker command
  prepends a fixed `[UNATTENDED DISPATCH]` header to the task text: no human
  is reading mid-run, never end a turn asking permission or how to begin,
  use `request_decision` for genuine decisions, otherwise make the
  owner-aligned call and proceed. This is MECHANISM, not bundle prose —
  field evidence (11 unattended eval cycles): the equivalent paragraph in
  the worker bundle's context did not hold, 3/4 workers consent-stalled
  anyway. Opt out with `dispatch --no-preamble` (raw task passthrough). A
  custom `--worker-cmd` never gets the preamble — you own the entire
  command there.
- `--judge` gates the finish line: exit 0 → `loop:closed`, nonzero →
  `loop:failed` (loud + bell). No judge → the worker finishes unjudged —
  but an unjudged worker that dies with a nonzero exit is still loud
  (`WORKER FAILED` notification + bell + `worker_failed` ledger entry).
- **Where the judge runs (read this before writing your first judge):** the
  judge does NOT run where the task did its work. It runs in the worker's
  *state* directory `$ATTENTION_HOME/workers/<session>/` (contents:
  `worker.log`, `meta.json` — not your repo), with `$WORKER_LOG` (absolute
  path to the worker's captured output) and `$WORKER_EXIT` (the worker's
  exit code; empty if the session died without one) exported. A
  relative-path judge like `test -f status-a.txt` will fail even when the
  work succeeded. Judge files in the work tree by ABSOLUTE path, or judge
  the worker's output via `"$WORKER_LOG"` as in the example above. Full
  contract: `context/judge-contract.md`.
- Verify a judge before trusting it (the broken-test protocol). Runnable
  verbatim — the first two lines create the artifacts the third one judges:

  ```bash
  echo DONE > good.txt
  echo "not finished" > broken.txt
  attention-manager judge verify --cmd 'grep -q DONE "$ARTIFACT"' --good good.txt --broken broken.txt
  ```

  `--cmd` runs via `bash -c` with `$ARTIFACT` set to the artifact path under
  test; it must PASS the known-good artifact AND FAIL the deliberately broken
  one. (The dispatch-time `--judge` command instead runs in the worker's
  state dir with `WORKER_LOG`/`WORKER_EXIT` in its environment — see "Where
  the judge runs" above — so a `"$WORKER_LOG"` judge like the dispatch
  example needs no `$ARTIFACT`.)

## Goal-derived judge (recommended default)

For real (non-toy) work units, prefer `judges/goal-judge.sh`: an agent judge
whose evidence bar is **derived from the task text at judge time** by a fixed
forensic/honesty core (provenance tracing, real-vs-mocked verification,
bidirectional claims-vs-artifacts). Mechanical checklists get gamed — a worker
once fabricated sine-wave WAVs named as speech to satisfy a wav-exists check;
the goal-derived bar is the non-overfit design that held in the 2026-07-31
autonomy evals.

The judge is **provenance-first** with mandatory checks written into its
prompt: every "verified/working/tested/passing" claim must trace to the
producing command/log/artifact (`$WORKER_LOG` is scanned for real
network/API activity as ground truth — a claim with no producer =
fabrication); if the task's bar requires real services/APIs, mock servers
and invented endpoints presented as real evidence = fabrication; the
worker's own research is cross-checked against its implementation choices;
the bar is re-derived fresh from the task text each run (retry/feedback
context never lowers it; artifact existence is never the bar); and the
judge fails closed — uncertain after inspection means `met=false` with the
uncertainty in the missing list. Hardened after a false-release where
"12/12 E2E" all hit a self-authored mock and the bar drifted from "is it
real" to "do artifacts exist".

Canonical dispatch (task text saved to a file, judged against the work tree):

```bash
printf '%s' "$TASK" > /abs/path/task.txt
attention-manager dispatch mywork \
  --task "$TASK" \
  --bundle 'git+https://github.com/bkrabach/amplifier-attention-manager@main#subdirectory=bundles/test-worker.md' \
  --judge 'GOAL_JUDGE_ROOT=/abs/path/to/worktree /abs/path/to/judges/goal-judge.sh /abs/path/task.txt'
```

- **Raise the judge timeout**: the agent evaluation runs up to
  `GOAL_JUDGE_TIMEOUT` (default 1200s), so start the supervisor with
  `supervise --judge-timeout 1500` (the default 60s would kill it mid-audit).
- `GOAL_JUDGE_ROOT` must point at the work tree — the judge's cwd is the
  worker *state* dir (see "Where the judge runs" above), not where the work
  happened. `$WORKER_LOG` is handed to the judge agent automatically.
- **Redispatch-on-fail (recommended loop):** on failure the judge prints
  `FAIL: <reason>` plus `MISSING: <evidence absent>` lines — WHAT is missing,
  never HOW to build it. Feed that missing-list forward verbatim into the next
  dispatch's task text ("previous attempt failed the finish line; absent
  evidence: ...") and redispatch. The bar stays goal-derived; the worker gets
  the gap, not the answer.
- Unparseable verdicts are a LOUD exit-1 fail — this judge has no silent-pass
  path (a fabrication-flagged verdict also fails, even when `met` is true).

## Making workers escalate (marked tasks AND self-identified decision points)

Workers don't escalate by themselves — two pieces make it happen:

1. **The worker bundle composes the packet-escalation behavior**
   (`behaviors/packet-escalation.yaml`), which provides the
   `request_decision` tool and the permission-gate provider. A plain bundle
   will just block or improvise instead of writing a packet.
   `bundles/test-worker.md` in this repo is the minimal working example.
2. **The worker's instructions tell it when to call `request_decision`.**
   Two paths, both required in the bundle body:
   - **Marked tasks (mandatory path):** when a task contains a
     `NEEDS-HUMAN-DECISION` marker with enumerated options, the worker MUST
     call `request_decision` with those exact options plus its
     recommendation, block until the packet is answered, then proceed per
     the answer — never inventing an answer.
   - **Self-identified decision points (unattended rule):** a dispatched
     worker runs with nobody reading its output. If it reaches a decision,
     preference, or approval point the task did NOT mark, it must still
     call `request_decision` with its concrete options and recommendation —
     never ask in conversational output. Without this rule, a worker
     composes a perfectly packet-shaped answer ("option A or B? I recommend
     A — which would you prefer?") and delivers it to an absent user, then
     exits clean: the decision is lost and nothing rings. The same rule
     covers turn 1: a worker must never end its first turn asking whether
     or how to begin ("brainstorm mode or direct execution?") — observed
     workers consent-stalled exactly this way, exiting 0 with zero tool
     calls and zero packets — it packetizes the choice or makes the
     owner-aligned call and proceeds.

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

Bells are transient: a worker's tmux session ends shortly after the worker
finishes and takes its bell flag with it, so without muxplex there may be no
bell left to see. The durable record of escalations and finish lines is
`$ATTENTION_HOME/events.jsonl` plus the ledger (`ledger --summary`) — bells
are the live-attention surface, not history. The notify sink
(`file:` / `ntfy:` / `console`) announces batches (packets and finish
lines), but delivery is window/max batched and only flushes while the
supervisor is running — a short run can end with the `file:` sink never
created even though every verdict is in the ledger.

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
