#!/usr/bin/env bash
# local_supervisor_smoke.sh — THE LOCAL JUDGE for build step 2 (supervisor loop).
# Same discipline as local_roundtrip.sh (see context/judge-contract.md).
#
# Happy path: two fake workers are dispatched into am-* tmux sessions via
# --worker-cmd (each echoes an amplifier-style "Session ID: <uuid>" line, then
# writes a decision packet carrying that same uuid as source.session_id via
# the ROOT queue lib and polls for its resolution — cross-checking
# module-free). The supervisor observes them, emits packet:created for both,
# joins each packet to its worker via the session id and RINGS the worker's
# tmux bell (window_bell_flag=1 + bell:rung events — the muxplex surface),
# sends ONE batched notification covering both packets, survives a mid-run
# kill+restart with no duplicate events, and after both packets are answered
# via the CLI, both workers exit 0 and the supervisor emits packet:answered +
# worker:finished(judged:false) for each, with the full story in the ledger.
#
# Exit 0 + "PASS: <reason>" or exit 1 + "FAIL: <reason>".
#
# --self-test runs the broken-test protocol: the happy direction MUST pass AND
# a sabotaged run (answers skipped → workers still running at the judge's
# timeout) MUST fail loudly. A judge that never fails is decoration.
#
# Note: the fake worker is the design's "python3 one-liner using the root
# queue lib", written to a file instead of inlined purely to avoid three
# layers of shell quoting (bash → tmux → bash -c).

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if ! command -v tmux >/dev/null 2>&1; then
    echo "FAIL: tmux is not installed — the step-2 smoke cannot run (no degraded mode)"
    exit 1
fi

am() { python3 -m attention_manager.cli "$@"; }

SUP_PID=""
SESSIONS=()

cleanup() {
    [ -n "$SUP_PID" ] && kill "$SUP_PID" 2>/dev/null
    for s in "${SESSIONS[@]:-}"; do
        [ -n "$s" ] && tmux kill-session -t "=$s" 2>/dev/null
    done
}
trap cleanup EXIT

write_fake_worker() {
    # $1 = destination file. Worker: announce an amplifier-style session id
    # (observe() extracts it from worker.log — the bell-join key), write a
    # decision packet carrying the SAME id as source.session_id (root queue
    # lib), poll for resolution, exit 0 iff answered with the expected option.
    cat >"$1" <<'PY'
import sys
import uuid

from attention_manager.packet import Option, Packet, Source
from attention_manager.queue import PacketQueue

name, expected, timeout_s = sys.argv[1], sys.argv[2], float(sys.argv[3])
sid = str(uuid.uuid4())
print(f"Session ID: {sid}", flush=True)  # captured by pipe-pane -> worker.log -> observe()
q = PacketQueue()
p = Packet(
    question=f"smoke [{name}]: proceed with option {expected}?",
    options=[Option(id="A", label="Proceed"), Option(id="B", label="Abort")],
    source=Source(kind="decision", session_id=sid, muxplex_session=f"am-{name}"),
)
q.write(p)
print(f"packet written: {p.id}", flush=True)
r = q.await_resolution(p.id, poll_s=0.3, timeout_s=timeout_s)  # raises on timeout (fail loud)
print(f"resolved: {r.answer} by {r.answered_by}", flush=True)
sys.exit(0 if r.answer == expected else 1)
PY
}

count_events() {
    # $1 = event name. Counts occurrences in the supervisor's events.jsonl.
    python3 - "$ATTENTION_HOME/events.jsonl" "$1" <<'PY'
import json, sys
from pathlib import Path
path, name = Path(sys.argv[1]), sys.argv[2]
count = 0
if path.exists():
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and json.loads(line).get("event") == name:
            count += 1
print(count)
PY
}

wait_for_event_count() {
    # $1 = event, $2 = expected count, $3 = timeout seconds
    local i
    for i in $(seq 1 $(($3 * 2))); do
        [ "$(count_events "$1")" -ge "$2" ] && return 0
        sleep 0.5
    done
    return 1
}

start_supervisor() {
    python3 -m attention_manager.cli supervise --interval 1 \
        --notify "file:$NOTIFY_FILE" --batch-window 3 >>"$SUP_LOG" 2>&1 &
    SUP_PID=$!
}

run_smoke() {
    # $1 = "answer" (happy path) or "skip-answer" (sabotage: judge must FAIL)
    local mode="$1"
    export ATTENTION_HOME="$(mktemp -d)"
    export ATTENTION_QUEUE_DIR="$(mktemp -d)"
    NOTIFY_FILE="$(mktemp)"
    : >"$NOTIFY_FILE"
    SUP_LOG="$(mktemp)"
    local suffix="$$-$RANDOM"
    local n1="smoke1-$suffix" n2="smoke2-$suffix"
    SESSIONS=("am-$n1" "am-$n2")

    local worker_py="$ATTENTION_HOME/fake_worker.py"
    write_fake_worker "$worker_py"
    local envprefix="ATTENTION_QUEUE_DIR=$ATTENTION_QUEUE_DIR PYTHONPATH=$PYTHONPATH"

    # Dispatch BOTH workers first (both packets exist before the supervisor's
    # first tick → the one-batch assertion is deterministic).
    for n in "$n1" "$n2"; do
        if ! am dispatch "$n" --task "smoke worker $n" \
            --worker-cmd "$envprefix python3 $worker_py $n A 90"; then
            echo "FAIL: dispatch $n failed"
            return 1
        fi
    done

    for s in "${SESSIONS[@]}"; do
        if ! tmux has-session -t "=$s" 2>/dev/null; then
            echo "FAIL: tmux session $s does not exist after dispatch"
            return 1
        fi
    done
    echo "  both am-* sessions exist: ${SESSIONS[*]}"

    start_supervisor

    if ! wait_for_event_count "packet:created" 2 20; then
        echo "FAIL: expected 2 packet:created events, got $(count_events packet:created) (sup log: $(tail -5 "$SUP_LOG"))"
        return 1
    fi
    echo "  2 packet:created events observed"

    # Muxplex bell surface: each packet joins to its worker via the session id
    # (late binding tolerated) and rings the worker's tmux bell exactly once.
    if ! wait_for_event_count "bell:rung" 2 20; then
        echo "FAIL: expected 2 bell:rung events, got $(count_events bell:rung) (sup log: $(tail -5 "$SUP_LOG"))"
        return 1
    fi
    for s in "${SESSIONS[@]}"; do
        flag="$(tmux list-windows -t "=$s" -F '#{window_bell_flag}' | head -1)"
        if [ "$flag" != "1" ]; then
            echo "FAIL: expected window_bell_flag=1 on $s after packet bell, got '$flag'"
            return 1
        fi
    done
    echo "  2 bell:rung events + window_bell_flag=1 on both am-* sessions"

    # ONE batch notification containing both packets (batching proof).
    local batch_ok=""
    for _ in $(seq 1 30); do
        batch_ok="$(python3 - "$NOTIFY_FILE" <<'PY'
import json, sys
from pathlib import Path
lines = [ln for ln in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if ln.strip()]
if len(lines) == 1 and json.loads(lines[0])["count"] == 2:
    print("ok")
PY
)"
        [ "$batch_ok" = "ok" ] && break
        sleep 0.5
    done
    if [ "$batch_ok" != "ok" ]; then
        echo "FAIL: expected exactly ONE batch notification covering both packets; file: $(cat "$NOTIFY_FILE")"
        return 1
    fi
    echo "  ONE batch notification covers both packets"

    # Single-instance lock: a SECOND supervise against the same ATTENTION_HOME
    # must fail loud (exit 1 + lock error) while the first one runs. This is
    # the S4 DTU failure mode: a supervisor surviving a botched kill plus a
    # restarted one silently duplicated packet:answered/worker:finished events.
    local second_out=""
    if second_out="$(am supervise --once --notify "file:$NOTIFY_FILE" 2>&1)"; then
        echo "FAIL: second supervisor was allowed to run against the same home (single-instance lock broken)"
        return 1
    fi
    if ! echo "$second_out" | grep -qi "another supervisor"; then
        echo "FAIL: second supervise failed, but without the lock error (got: $second_out)"
        return 1
    fi
    if [ "$(count_events "supervisor:started")" -ne 1 ]; then
        echo "FAIL: refused supervisor still wrote events (supervisor:started count: $(count_events supervisor:started))"
        return 1
    fi
    echo "  second supervisor refused loud (single-instance lock held; no events written)"

    # Kill the supervisor mid-run (packets created, not yet answered), restart,
    # and prove state rebuild: no duplicate packet:created events (D5). The
    # restart also proves the lock is RELEASED on clean shutdown (and flock
    # dies with the process on SIGKILL — see tests/test_supervisor_lock.py).
    kill "$SUP_PID" 2>/dev/null
    wait "$SUP_PID" 2>/dev/null
    SUP_PID=""
    start_supervisor
    sleep 3
    local created_after
    created_after="$(count_events packet:created)"
    if [ "$created_after" -ne 2 ]; then
        echo "FAIL: after supervisor restart expected still 2 packet:created events, got $created_after (duplicate tracking — D5 broken)"
        return 1
    fi
    echo "  supervisor killed + restarted: still exactly 2 packet:created (state rebuilt, no dupes)"

    if [ "$mode" = "answer" ]; then
        local ids
        ids="$(am --json queue list | python3 -c 'import json,sys; print("\n".join(p["id"] for p in json.load(sys.stdin)))')"
        if [ "$(echo "$ids" | grep -c .)" -ne 2 ]; then
            echo "FAIL: expected 2 pending packets to answer, got: $ids"
            return 1
        fi
        local pid
        for pid in $ids; do
            if ! am answer "$pid" A --rationale "smoke"; then
                echo "FAIL: CLI answer failed for $pid"
                return 1
            fi
        done
    else
        echo "  (sabotage: skipping the answer step)"
    fi

    # Judge assertion: both workers exit 0 (sentinel in logs) within budget.
    local budget=25
    [ "$mode" = "skip-answer" ] && budget=12
    local deadline=$((SECONDS + budget)) exited=0
    while [ $SECONDS -lt $deadline ]; do
        exited=0
        for s in "${SESSIONS[@]}"; do
            grep -q "__AM_WORKER_EXIT:0__" "$ATTENTION_HOME/workers/$s/worker.log" 2>/dev/null && exited=$((exited + 1))
        done
        [ "$exited" -eq 2 ] && break
        sleep 0.5
    done

    if [ "$mode" = "skip-answer" ]; then
        # Sabotaged run: workers must STILL be running (unanswered) — judge FAILs loud.
        if [ "$exited" -eq 2 ]; then
            echo "FAIL: sabotaged run — workers exited 0 without answers (silent default?)"
            return 1
        fi
        echo "FAIL: workers still running at timeout — packets were never answered (sabotage direction behaving as designed)"
        return 1
    fi

    if [ "$exited" -ne 2 ]; then
        echo "FAIL: expected both workers to exit 0, got $exited/2 (logs under $ATTENTION_HOME/workers/)"
        return 1
    fi
    echo "  both workers exited 0 (sentinel verified in logs)"

    if ! wait_for_event_count "packet:answered" 2 20; then
        echo "FAIL: expected 2 packet:answered events, got $(count_events packet:answered)"
        return 1
    fi
    if ! wait_for_event_count "worker:finished" 2 20; then
        echo "FAIL: expected 2 worker:finished events, got $(count_events worker:finished)"
        return 1
    fi

    # worker:finished must carry exit_code 0 and the honest judged:false field
    # (loop:closed is judge-gated — step 4; faking finish lines violates D7).
    if ! python3 - "$ATTENTION_HOME/events.jsonl" <<'PY'
import json, sys
from pathlib import Path
finished = [json.loads(l) for l in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l).get("event") == "worker:finished"]
ok = len(finished) == 2 and all(e["exit_code"] == 0 and e["judged"] is False for e in finished)
sys.exit(0 if ok else 1)
PY
    then
        echo "FAIL: worker:finished events missing exit_code=0 / judged:false"
        return 1
    fi
    echo "  2 packet:answered + 2 worker:finished(judged:false) events verified"

    # Ledger has the full story: dispatches, creations, answers, finishes, batch.
    if ! python3 - <<'PY'
import json, os, sys
from collections import Counter
from pathlib import Path
ledger_dir = Path(os.environ["ATTENTION_HOME"]) / "ledger"
kinds = Counter()
for f in ledger_dir.glob("*.jsonl"):
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            kinds[json.loads(line)["kind"]] += 1
expected = {"dispatched": 2, "packet_created": 2, "packet_answered": 2, "worker_finished": 2}
bad = {k: (kinds.get(k, 0), v) for k, v in expected.items() if kinds.get(k, 0) != v}
if bad or kinds.get("notified_batch", 0) < 1:
    print(f"ledger kinds: {dict(kinds)}; mismatches: {bad}")
    sys.exit(1)
PY
    then
        echo "FAIL: ledger does not tell the full story (see kinds above)"
        return 1
    fi
    echo "  ledger verified: dispatched x2, packet_created x2, packet_answered x2, worker_finished x2, notified_batch >=1"

    kill "$SUP_PID" 2>/dev/null && wait "$SUP_PID" 2>/dev/null
    SUP_PID=""
    echo "PASS: dispatch → observe → batch-notify → kill/restart (no dupes) → answer → workers exit 0 → events + ledger verified"
    return 0
}

if [ "${1:-}" = "--self-test" ]; then
    echo "== self-test 1/2: happy path (must PASS) =="
    if ! run_smoke answer; then
        echo "FAIL: self-test — happy path did not pass"
        exit 1
    fi
    cleanup
    SUP_PID=""
    SESSIONS=()
    echo "== self-test 2/2: sabotaged path, answers skipped (must FAIL loud) =="
    if run_smoke skip-answer; then
        echo "FAIL: self-test — sabotaged run passed (judge is decoration)"
        exit 1
    fi
    echo "PASS: self-test — judge passes on good run AND fails on sabotaged run"
    exit 0
fi

run_smoke answer
exit $?
