# Dogfood Quickstart — the Tuesday Test

Run a real workday through the attention manager. Not a demo: real work units,
real escalations, real answers. The proof gate is design §Metrics — one day
where the system manages your attention instead of the other way around.

## Install

```bash
uv tool install 'amplifier-attention-manager[attractor] @ git+https://github.com/bkrabach/amplifier-attention-manager@main'
```

Requires tmux (fail-loud without it) and a working `amplifier` CLI on PATH
(triage and the recipe-gate bridge shell out to it).

## Start the manager (one terminal, leave it running)

```bash
attention-manager supervise --triage --recipes --notify ntfy:<your-ntfy-url> --interval 2
```

- The ntfy URL is yours to supply (e.g. `ntfy:https://ntfy.sh/<your-topic>`).
  No ntfy? Use `--notify file:/tmp/attention.jsonl` or `--notify console`.
- Bells are ON by default (`--no-bells` to disable): escalations and failed
  loops ring the worker's tmux bell so muxplex surfaces them.
- State lives under `~/.amplifier/attention/` (override: `$ATTENTION_HOME`).

## Dispatch real work

```bash
attention-manager dispatch portfix \
  --task "migrate the config parser to the new schema" \
  --bundle <worker-bundle-uri> \
  --judge 'cd ~/repos/thing && python -m pytest -q'
```

- `--judge` gates the finish line: exit 0 → `loop:closed`, nonzero →
  `loop:failed` (loud + bell). No judge → the worker finishes unjudged.
- Verify a judge before trusting it: `attention-manager judge verify --cmd ... --good ... --broken ...`

## The muxplex view

Workers appear in muxplex automatically as `am-*` tmux sessions. When a worker
escalates (packet lands) or a loop fails, its session bell rings —
`sort=attention` floats it to the top, muxplex-deck shows amber. You (or the
muxplex UI) clear bells; the manager never does. Hop into any session from the
browser at any time — the manager won't fight you for the view.

NOTE: muxplex views filter what surfaces. If your active view is a curated one
(not "all"), belled `am-*` sessions won't appear in it — add them to the view
you dogfood from (or use "all"). The bell state itself is always there
(host-verified: `GET /api/sessions` shows `bell.unseen_count`/`last_fired_at`
for `am-*` sessions the moment the manager rings).

Answer escalations on your schedule, cold, from the packet alone:

```bash
attention-manager queue list
attention-manager queue show <pkt-id>
attention-manager answer <pkt-id> B --rationale "downstream has no owner this week"
```

## Make answers compound (the queue that learns)

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
between an attention firewall and a snooze button. Everything needed to compute
it is in the ledger and queue files.

## Honest v1 rough edges

- **Workers don't escalate by themselves.** The packet-escalation behavior
  (`behaviors/packet-escalation.yaml`) must be composed into every worker
  bundle — a plain bundle will just block or improvise instead of writing a
  packet.
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
  the worker is re-driven via the packet's `links.resume`.
