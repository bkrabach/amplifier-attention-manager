# Scenario 3 — Durability: the queue survives worker death

**What this proves:** design decision D5 ("state on disk everywhere") and the
durability caveat scoped honestly in the design: *the queue survives anything;
blocked worker turns are not durable and are re-driven via `session resume`
from the packet's re-entry data.* Killing the worker mid-block must leave the
packet pending, answerable, and carrying enough re-entry data to re-drive the
turn.

References: `docs/designs/attention-manager.md` §Tier 2 durability caveat +
Tuesday-test item 3, `context/packet-schema.md` (`links` / `source.session_id`).

## Setup

- Scenario-scoped queue: `ATTENTION_QUEUE_DIR=<work-dir>/<run-id>/s3-durability-restart/queue`.
- Worker bundle: `<repo-dir>/bundles/test-worker.md` (same shape as scenario 1,
  different decision question so packets are unambiguous).

## Steps

1. Launch scenario-1-style worker in background (own process group); record PID.
2. Poll until a `kind=decision` packet is pending (timeout 240s).
3. **SIGKILL the worker's whole process group** (`kill -9 -- -<pid>`) — the
   worker dies mid-block, exactly the non-durable case the design scopes.
4. Confirm the worker process is dead; wait ~3s for any dying writes to settle.
5. Assert the packet is STILL pending (`queue list` contains it AND
   `pending/<id>.json` exists).
6. Answer it via CLI: `attention-manager answer <id> B --rationale "eval"`.
7. Assert it moved to `answered/` with an intact resolution.
8. Assert re-entry data exists on the answered packet (see below). Do NOT
   actually run `session resume` — out of scope for step 1; this asserts the
   re-drive is *possible*.

## Re-entry data — honest definition

The packet schema makes `links.resume` OPTIONAL; the current
`tool-request-decision` implementation records the producing session as
`source.session_id` (from which `amplifier session resume <session_id>` is
constructed) and writes `links: {}`. The assertion therefore passes iff
**either** `links.resume` is a non-empty string **or** `source.session_id` is a
non-empty string. If neither is present, the re-drive promise is unmet and the
assertion FAILS — that is a real finding, not a grader gap.

## Grader assertions

| # | Assertion | Check |
|---|-----------|-------|
| 1 | `packet-appeared` | `kind=decision` packet pending within 240s |
| 2 | `worker-killed` | post-SIGKILL aliveness probe reports the PID dead |
| 3 | `packet-survives-kill` | after kill + settle, packet still in `queue list` AND `pending/<id>.json` exists |
| 4 | `answer-accepted` | `answer <id> B --rationale eval` exits 0 (queue answerable with no producer alive) |
| 5 | `packet-answered-intact` | `answered/<id>.json` has `resolution.answer == "B"`, `answered_by == "human"`, `answered_at` present |
| 6 | `reentry-data-present` | `links.resume` non-empty OR `source.session_id` non-empty on the answered packet |

## Pass criteria

ALL assertions pass. Unevaluable assertions are FAIL with reason
`could-not-evaluate`. Per-scenario hard timeout: 300s. Note the worker is
expected to be dead by design here — no worker-output assertions exist.
