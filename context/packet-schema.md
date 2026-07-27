# Packet Schema — the on-disk file contract (AUTHORITATIVE)

This document is the contract ("the stud" in bricks-and-studs terms) between
every producer and consumer of the attention-manager escalation bus. The
`attention_manager` package (CLI/app/tests) and the worker-side modules
(`tool-request-decision`, `hooks-packet-approval`) each implement their own IO
against **this document** — they do not share code. Duplicated IO is
acceptable; divergent formats are not. The shared tests in `tests/` cross-check
the implementations against the same files.

Schema version: **1**. Any breaking change to this contract bumps
`schema_version` and updates this document first.

## Directory layout

```
<root>/                 # $ATTENTION_QUEUE_DIR, else ~/.amplifier/attention/queue
├── pending/            # packets awaiting an answer
├── answered/           # resolved packets (resolution filled) — authoritative once present
├── auto/               # Phase-2+ manager auto-answer log (human calibration review)
└── bounced/            # malformed packets returned to producers (failed cold-reader test)
```

- One packet per file: `<packet id>.json` (e.g. `pending/pkt-20260726-162001-a3f2.json`).
- Subdirectories are created on demand.
- The queue is rebuilt from the filesystem on every scan. No queue state lives
  anywhere else (design decision D5): kill any process at 60%, restart, resume at 60%.

## Atomic-write rule

Every write of a packet file MUST be atomic: write the full JSON to a
`*.json.tmp` file in the same directory, then `os.replace()` (rename) it to the
final `*.json` name. Readers MUST ignore `*.tmp` files. A partially written
packet must never be observable under its final name.

## Resolution flow

1. Producer writes packet to `pending/<id>.json` (atomic), then polls
   `answered/<id>.json` (poll interval ~1s).
2. Answerer (CLI `attention-manager answer`, or the manager) validates that the
   chosen option is one of the packet's declared `options` ids, fills
   `resolution`, writes the resolved packet to `answered/<id>.json` (atomic),
   then removes `pending/<id>.json`.
   - A crash between the two steps leaves both files present; **`answered/` is
     authoritative** whenever it exists.
3. Producer sees `answered/<id>.json`, reads `resolution`, unblocks.

Fail-loud (design decision D7): nothing ever fabricates a resolution. Timeouts
either apply an option **explicitly declared** in `urgency.on_timeout` (marked
`answered_by: "timeout-default"`) or surface an error and leave the packet
pending.

## JSON schema (field by field)

```jsonc
{
  // REQUIRED. Sortable unique id: pkt-<UTC yyyymmdd-HHMMSS>-<4 hex chars>.
  "id": "pkt-20260726-162001-a3f2",

  // REQUIRED. ISO-8601 UTC creation time, e.g. "2026-07-26T16:20:01Z".
  "created_at": "2026-07-26T16:20:01Z",

  // REQUIRED. Integer contract version. Currently 1.
  "schema_version": 1,

  // REQUIRED.
  "source": {
    // REQUIRED. One of: "decision" | "permission" | "attractor-gate" | "recipe-gate".
    "kind": "decision",
    // OPTIONAL. Producing Amplifier session id (for links.resume / hop-in).
    "session_id": "abc123",
    // OPTIONAL. Work-unit name if part of a dispatched unit.
    "work_unit": "portfix",
    // OPTIONAL. muxplex tmux session name (hop-in target), e.g. "am-portfix-3".
    "muxplex_session": "am-portfix-3"
  },

  // REQUIRED. ONE decision, one sentence. Non-empty.
  "question": "Migrate the config parser now, or keep the compat shim?",

  // REQUIRED. Non-empty list. Each option: required "id" + "label" (non-empty
  // strings), optional "consequence". Option ids MUST be unique.
  // For source.kind == "permission": EXACTLY two options with ids "allow" and "deny".
  "options": [
    {"id": "A", "label": "Migrate now", "consequence": "breaks two downstream repos"},
    {"id": "B", "label": "Keep shim", "consequence": "carries buggy path ~2 weeks"}
  ],

  // OPTIONAL. Producer's recommendation. "option" MUST be one of options[].id.
  "recommendation": {"option": "B", "rationale": "no downstream owner this week", "confidence": "medium"},

  // REQUIRED (may be empty string). BOUNDED decision material — the minimal
  // facts needed to decide cold, not a transcript. MAX 8000 characters;
  // packets exceeding this are invalid (bounce, don't truncate silently).
  "context": "…",

  // REQUIRED (may be empty object). Re-entry links.
  "links": {
    "resume": "amplifier session resume abc123",   // OPTIONAL
    "files": ["path/one.py", "path/two.py"]        // OPTIONAL
  },

  // REQUIRED.
  "urgency": {
    // REQUIRED. "batch" (default) | "today" | "now". "now" is rare and must justify itself.
    "tier": "batch",
    // OPTIONAL. ISO-8601 deadline.
    "deadline": "2026-07-27T00:00:00Z",
    // OPTIONAL. EXPLICIT declared timeout policy — never a silent default.
    // "apply-option" REQUIRES "option" (one of options[].id).
    // If on_timeout is present, "deadline" is REQUIRED (a policy without a
    // deadline is meaningless).
    "on_timeout": {"action": "apply-option", "option": "B"}
  },

  // OPTIONAL. Filled by the manager's triage pass (step 3+).
  "triage": {"handled_by": "manager", "rule_refs": ["rulebook §3.2"], "why": "one-line reasoning"},

  // OPTIONAL until answered; REQUIRED in answered/ and auto/.
  "resolution": {
    // REQUIRED. MUST be one of options[].id.
    "answer": "B",
    // OPTIONAL.
    "rationale": "downstream owners unavailable",
    // REQUIRED. "human" | "manager" | "timeout-default".
    "answered_by": "human",
    // REQUIRED. ISO-8601 UTC.
    "answered_at": "2026-07-26T17:00:00Z"
  }
}
```

Unknown extra fields are tolerated by readers (forward compatibility) but
producers must not rely on them.

## Validation rules (fail loud)

Writers MUST validate before writing; a validation failure raises with a
specific message — never write a best-effort packet:

1. `id` starts with `pkt-`; `schema_version == 1`; `created_at` present.
2. `source.kind` ∈ {decision, permission, attractor-gate, recipe-gate}.
3. `question` non-empty.
4. `options` non-empty; every option has non-empty `id` and `label`; ids unique.
5. `permission` packets: exactly the two options `allow` / `deny`.
6. `recommendation.option`, `urgency.on_timeout.option`, and
   `resolution.answer` (when present) MUST each be one of `options[].id`.
7. `context` ≤ 8000 characters.
8. `urgency.tier` ∈ {batch, today, now}; `on_timeout.action` ∈
   {apply-option, fail-loud}; `apply-option` requires `option`.
9. `resolution` (when present) has `answer`, `answered_by`, `answered_at`.

## The cold-reader test

A packet must be answerable by a reader with **nothing but the packet and the
rulebook** — no worker transcript, no session context. If a cold reader cannot
decide, the packet is malformed: move it to `bounced/` with a `triage.why`
explaining what is missing, and the producing worker must enrich and resubmit.
This is enforced structurally because triage itself reads cold.
