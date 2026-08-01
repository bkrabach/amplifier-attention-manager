---
name: attention-manager
description: "Dispatch detached, judge-gated workers via the attention-manager CLI — walk-away autonomy with an escalation queue. Workers run in tmux under a supervisor; a goal-derived judge gates the finish line; blocked workers write decision packets you answer on your schedule. Use when leaving work running unattended, running parallel work units, or when 'done' must be provable with real evidence; prefer in-session goal loops (/goal) when you'll stay attending the session."
version: 0.1.0
---

# Attention Manager — drive the CLI from an Amplifier session

Attention-manager (AM) turns "watch the agent work" into "answer packets on
your schedule": `dispatch` launches a worker into a detached `am-*` tmux
session, a `supervise` loop observes it, a **judge** command gates the finish
line (exit 0 = `loop:closed`, nonzero = `loop:failed`, loud), and blocked
workers escalate by writing **packets** (question + enumerated options +
recommendation) to a disk queue instead of asking an absent human.

Drive everything below via `bash`. All state lives under
`~/.amplifier/attention/` (`$ATTENTION_HOME`).

## Prerequisites (check first)

```bash
command -v attention-manager \
  || uv tool install 'git+https://github.com/bkrabach/amplifier-attention-manager@main'
command -v tmux        # required — dispatch/supervise fail loud without it
command -v amplifier   # workers, triage, and the goal judge shell out to it
```

## When to use AM (the "who verifies?" rule)

Ask: **when the work claims done, who verifies it — you, or a machine?**
If you'll be present to read output and verify, an in-session goal loop is
simpler. If nobody is watching, the finish line needs a judge and the
decision points need packets. That is AM.

| Situation | Use |
|---|---|
| You stay attending; you verify results yourself | In-session goal loop (`/goal`) — simpler |
| You walk away mid-work (overnight, meetings) | AM: dispatch + judge + packets |
| Several independent work units in parallel | AM: one dispatch per unit, one queue |
| "Done" must be PROVEN (evidence, not prose) | AM with the goal-derived judge |
| Worker should run a stock-lean bundle, not your full session composition | AM: `--bundle` per dispatch |
| Quick question / small edit in front of you | Neither — just do it |

## Flow A — start the supervisor (once, leave it running)

Run from `$HOME`, not a project dir (every `amplifier tool invoke` the
supervisor makes costs a session in the invoking project's store):

```bash
cd ~ && attention-manager supervise --interval 5 --judge-timeout 1800 \
  --notify file:$HOME/.amplifier/attention/notify.jsonl
```

- **`--judge-timeout` MUST exceed the goal judge's own budget**
  (`GOAL_JUDGE_TIMEOUT`, default 1200s). The supervisor default is 60s —
  it will kill an agent judge mid-audit. 1800 > 1200: safe.
- Silence = healthy. Errors go to stderr; events to
  `$ATTENTION_HOME/events.jsonl`. Confirm liveness with
  `attention-manager status` from another shell.
- Optional: `--triage` (cold pre-triage of packets), `--recipes` (recipe
  approval gates → packets), `--notify ntfy:<url>` or `console`.
- Run it in the background (own tmux session or `run_in_background`) — it is
  a foreground loop.

## Flow B — dispatch with the goal-derived judge (recommended default)

Save the task text to a file; the judge derives its evidence bar from it at
judge time (mechanical checklists get gamed — this one held in autonomy evals):

```bash
printf '%s' "$TASK" > /abs/path/task.txt
attention-manager dispatch mywork \
  --task "$TASK" \
  --bundle 'git+https://github.com/bkrabach/amplifier-attention-manager@main#subdirectory=bundles/test-worker.md' \
  --judge 'GOAL_JUDGE_ROOT=/abs/path/to/worktree /abs/path/to/judges/goal-judge.sh /abs/path/task.txt'
```

- The `[UNATTENDED DISPATCH]` preamble is prepended to the task
  **automatically** (never end a turn asking permission; packetize real
  decisions; otherwise make the owner-aligned call and proceed). Opt out
  with `--no-preamble`. A custom `--worker-cmd` never gets the preamble.
- `GOAL_JUDGE_ROOT` must point at the WORK TREE. The judge's cwd is the
  worker *state* dir (`$ATTENTION_HOME/workers/am-<name>/` — `worker.log` +
  `meta.json`, not your repo). `$WORKER_LOG` is exported automatically.
- Get `goal-judge.sh` onto disk first if not present:
  `git clone --depth 1 https://github.com/bkrabach/amplifier-attention-manager /tmp/am && chmod +x /tmp/am/judges/goal-judge.sh`.
- Simple deterministic judges also work:
  `--judge 'grep MIGRATION-COMPLETE "$WORKER_LOG"'`. Verify a judge before
  trusting it: `attention-manager judge verify --cmd '...' --good <ok-file> --broken <bad-file>`.
- A worker that dies within ~3s on a bundle/load failure is reported loudly
  by `dispatch` itself (nonzero exit + log path).

## Flow C — the escalation queue (answer packets cold)

```bash
attention-manager queue list                # pending + BOUNCED, with SOURCE column
attention-manager queue show <pkt-id>       # full packet: question, options, recommendation
attention-manager answer <pkt-id> B --rationale "downstream has no owner this week"
```

- **Answer with the option ID only** (`A`, `B`, ...) — exactly one of the
  packet's enumerated option ids, not free text.
- `answer` works on BOUNCED packets too — the human override on a triage
  bounce.
- Compounding: `attention-manager rulebook proposals` then
  `rulebook apply <id>` — every answer proposes the rule that would have
  prevented the escalation. Phase-2 auto-answers: `auto list` /
  `auto confirm <pkt-id>` / `auto reject <pkt-id> --correct-option A --reason "..."`.

## Flow D — watch progress and read the ledger

```bash
attention-manager status            # workers this home dispatched + state + pending packets
attention-manager ledger --summary  # loops closed/failed, packets, rules, escalations/work-unit
tail -5 ~/.amplifier/attention/events.jsonl   # append-only event stream
```

The metric that matters: **escalations per healthy work unit must fall week
over week** — `ledger --summary` computes it (failed units excluded from the
denominator).

## Flow E — redispatch-on-fail loop

On `loop:failed` the goal judge prints `FAIL: <reason>` plus
`MISSING: <evidence absent>` lines — WHAT is missing, never HOW to build it.

1. Read the verdict (notify sink or the worker's log/judge scratch).
2. Feed the MISSING lines forward **verbatim** into the next task text:
   `"previous attempt failed the finish line; absent evidence: <MISSING lines>"`.
3. Redispatch with a **new worker name per cycle** (`mywork-r2`, `mywork-r3`).
4. Tell the redispatched worker to CONTINUE — prior work is preserved in the
   work tree; a worker told nothing will restart from scratch.

The bar stays goal-derived; the worker gets the gap, not the answer.

## Footguns (all verified)

- **Bundle refs reject SHORT git SHAs.** `#...@<7-char-sha>` fails to
  resolve. Use a branch (`@main`) or the full 40-char SHA.
- **Supervisor judge-timeout kills long agent judges.** Default 60s;
  `goal-judge.sh` runs up to `GOAL_JUDGE_TIMEOUT` (1200s). Always launch
  `supervise` with `--judge-timeout` above the judge's own budget.
- **Workers only escalate if their bundle composes
  `behaviors/packet-escalation.yaml`** (provides `request_decision` + the
  packet-writing approval gate). A plain bundle blocks or improvises instead
  of writing a packet. `bundles/test-worker.md` is the minimal working
  example — start from it.
- **Prior work is preserved in the workdir.** On redispatch, instruct the
  worker to inspect existing state and continue — not restart.
- **Relative-path judges fail.** Judges run in the worker STATE dir, not the
  work tree. Judge by absolute path or via `"$WORKER_LOG"`.
- **Isolating experiments: set BOTH env vars.** `ATTENTION_HOME` is honored
  (verified: `src/attention_manager/state.py` `default_home()` reads it, and
  dispatch forwards it into worker tmux sessions) — but the packet queue
  root is a SEPARATE variable: `ATTENTION_QUEUE_DIR` defaults to the literal
  `~/.amplifier/attention/queue`, NOT `$ATTENTION_HOME/queue`. For a fully
  isolated sandbox export both, for the supervisor AND every dispatch/queue
  command:

  ```bash
  export ATTENTION_HOME=/tmp/am-sandbox ATTENTION_QUEUE_DIR=/tmp/am-sandbox/queue
  ```

- **Every `amplifier tool invoke` costs one amplifier session** in the
  invoking project's store. Run the supervisor from `$HOME`, never a project
  dir.
- **Bells are transient; the notify sink is the durable record.** A finished
  worker's tmux session (and its bell) goes away — read
  `file:`/`ntfy:`/`console` notifications and `queue list` for history.
