#!/usr/bin/env bash
# local_finishline_smoke.sh — THE LOCAL JUDGE for build step 4 (judge-gated
# finish lines). Same discipline as its siblings (see context/judge-contract.md).
#
# Happy path: first, `judge verify` is broken-tested against a good/broken
# artifact pair (a working grep-judge must PASS verify; a decoration judge
# that always passes must FAIL verify). Then two fake workers are dispatched
# with judges:
#   G writes an artifact containing the required marker and exits 0
#   B writes an artifact WITHOUT the marker and exits 0
# Both carry the SAME judge: `grep -q AM-FINISH-MARKER artifact.txt` (run by
# the supervisor with cwd = the worker's dir, so the relative path proves the
# cwd contract). Artifact-based rather than WORKER_LOG-grep by design: the
# worker writes the artifact file directly (race-free), whereas pane output
# reaches worker.log through pipe-pane asynchronously. WORKER_LOG/WORKER_EXIT
# env plumbing is covered by tests/test_judge.py.
#
# The supervisor must: close the loop for G (loop:closed + populated
# judge.log), fail the loop for B (loop:failed, loud), mark BOTH
# worker:finished events judged:true, push finish_line + finish_line_failed
# notification items through the batcher, and render both loops by name in
# `ledger --summary`.
#
# Exit 0 + "PASS: <reason>" or exit 1 + "FAIL: <reason>".
#
# --self-test runs the broken-test protocol: the happy direction MUST pass
# AND a sabotaged run (assert loop:closed for BOTH workers) MUST fail —
# because B legitimately fails its judge. A judge that can't fail is
# decoration.

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if ! command -v tmux >/dev/null 2>&1; then
    echo "FAIL: tmux is not installed — the step-4 smoke cannot run (no degraded mode)"
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

MARKER="AM-FINISH-MARKER"
JUDGE_CMD='if grep -q AM-FINISH-MARKER artifact.txt 2>/dev/null; then echo "PASS: marker found (worker exit: $WORKER_EXIT)"; else echo "FAIL: marker missing from artifact"; exit 1; fi'

run_smoke() {
    # $1 = "honest" (expect G closed + B failed) or
    #      "sabotage" (assert loop:closed for BOTH — must FAIL, B fails its judge)
    local mode="$1"
    export ATTENTION_HOME="$(mktemp -d)"
    export ATTENTION_QUEUE_DIR="$(mktemp -d)"
    NOTIFY_FILE="$(mktemp)"
    : >"$NOTIFY_FILE"
    SUP_LOG="$(mktemp)"
    local suffix="$$-$RANDOM"
    local ng="fin-good-$suffix" nb="fin-bad-$suffix"
    local sg="am-$ng" sb="am-$nb"
    SESSIONS=("$sg" "$sb")

    # -- Step A: judge verify (broken-test protocol) inside the smoke -------------
    local good_art="$ATTENTION_HOME/good-artifact.txt" broken_art="$ATTENTION_HOME/broken-artifact.txt"
    echo "$MARKER" >"$good_art"
    echo "no marker here" >"$broken_art"
    local verify_cmd='if grep -q AM-FINISH-MARKER "$ARTIFACT"; then echo "PASS: marker"; else echo "FAIL: no marker"; exit 1; fi'
    if ! am judge verify --cmd "$verify_cmd" --good "$good_art" --broken "$broken_art" >/dev/null; then
        echo "FAIL: judge verify rejected a WORKING judge (both directions should behave)"
        return 1
    fi
    echo "  judge verify: working grep-judge verified in both directions"
    if am judge verify --cmd 'echo "PASS: always"' --good "$good_art" --broken "$broken_art" >/dev/null 2>&1; then
        echo "FAIL: judge verify ACCEPTED a decoration judge that never fails"
        return 1
    fi
    echo "  judge verify: decoration judge (always passes) correctly REJECTED"

    # -- Step B: dispatch G (marker) and B (no marker) with the same judge ---------
    # Workers write their artifact directly into their own worker dir (created
    # by dispatch before tmux runs) — race-free, no pipe-pane dependency.
    if ! am dispatch "$ng" --task "finish-line good worker" \
        --worker-cmd "echo $MARKER > $ATTENTION_HOME/workers/$sg/artifact.txt" \
        --judge "$JUDGE_CMD"; then
        echo "FAIL: dispatch $ng failed"
        return 1
    fi
    if ! am dispatch "$nb" --task "finish-line bad worker" \
        --worker-cmd "echo marker deliberately omitted > $ATTENTION_HOME/workers/$sb/artifact.txt" \
        --judge "$JUDGE_CMD"; then
        echo "FAIL: dispatch $nb failed"
        return 1
    fi
    echo "  dispatched $sg (marker) and $sb (no marker), same judge"

    start_supervisor

    if ! wait_for_event_count "worker:finished" 2 30; then
        echo "FAIL: expected 2 worker:finished events, got $(count_events worker:finished) (sup log: $(tail -5 "$SUP_LOG"))"
        return 1
    fi

    local closed failed
    closed="$(count_events loop:closed)"
    failed="$(count_events loop:failed)"

    if [ "$mode" = "sabotage" ]; then
        # Sabotage direction: demand loop:closed for BOTH workers. B legitimately
        # fails its judge, so this MUST fail — a judge that can't fail is decoration.
        if [ "$closed" -eq 2 ]; then
            echo "FAIL: sabotage — BOTH loops closed, including the marker-less worker (judge is decoration)"
            return 1
        fi
        echo "FAIL: sabotage — expected loop:closed for both, got closed=$closed failed=$failed (judge failing as designed)"
        return 1
    fi

    # -- Honest-direction assertions ------------------------------------------------
    if [ "$closed" -ne 1 ] || [ "$failed" -ne 1 ]; then
        echo "FAIL: expected exactly 1 loop:closed + 1 loop:failed, got closed=$closed failed=$failed"
        return 1
    fi
    # loop:closed must be G's, loop:failed must be B's — with reasons/logs.
    if ! python3 - "$ATTENTION_HOME/events.jsonl" "$sg" "$sb" <<'PY'
import json, sys
from pathlib import Path
path, good, bad = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
events = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
closed = [e for e in events if e["event"] == "loop:closed"]
failed = [e for e in events if e["event"] == "loop:failed"]
finished = [e for e in events if e["event"] == "worker:finished"]
ok = (
    closed[0]["session"] == good
    and "PASS" in closed[0]["judge_output"]
    and failed[0]["session"] == bad
    and failed[0]["reason"] == "judge exited 1"
    and len(finished) == 2
    and all(e["judged"] is True for e in finished)
    and {e["session"]: e["judge_result"] for e in finished} == {good: "closed", bad: "failed"}
)
sys.exit(0 if ok else 1)
PY
    then
        echo "FAIL: loop:closed/loop:failed sessions or worker:finished judged/judge_result fields wrong"
        return 1
    fi
    echo "  loop:closed for $sg + loop:failed for $sb (loud); both worker:finished judged:true"

    for s in "$sg" "$sb"; do
        if [ ! -s "$ATTENTION_HOME/workers/$s/judge.log" ]; then
            echo "FAIL: judge.log for $s is missing or empty"
            return 1
        fi
    done
    echo "  judge.log populated for both workers"

    # Notification items: finish_line (G) + finish_line_failed (B) through the batcher.
    local notify_ok=""
    for _ in $(seq 1 20); do
        notify_ok="$(python3 - "$NOTIFY_FILE" <<'PY'
import json, sys
from pathlib import Path
kinds = []
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if line.strip():
        kinds += [p["kind"] for p in json.loads(line)["packets"]]
if sorted(kinds) == ["finish_line", "finish_line_failed"]:
    print("ok")
PY
)"
        [ "$notify_ok" = "ok" ] && break
        sleep 0.5
    done
    if [ "$notify_ok" != "ok" ]; then
        echo "FAIL: notification items missing finish_line + finish_line_failed kinds; file: $(cat "$NOTIFY_FILE")"
        return 1
    fi
    echo "  notification batch carries finish_line + finish_line_failed items"

    # Ledger summary: the closure ritual renders both loops by name.
    local summary
    summary="$(am ledger --summary)"
    if ! echo "$summary" | grep -q "Loops closed (1):" ||
        ! echo "$summary" | grep -q "$sg" ||
        ! echo "$summary" | grep -q "Loops failed (1):" ||
        ! echo "$summary" | grep -q "$sb"; then
        echo "FAIL: ledger --summary does not render both loops by name; got:"
        echo "$summary"
        return 1
    fi
    echo "  ledger --summary renders both loops by name (closed: $sg, failed: $sb)"

    kill "$SUP_PID" 2>/dev/null && wait "$SUP_PID" 2>/dev/null
    SUP_PID=""
    echo "PASS: judge verify → dispatch(G,B with judges) → loop:closed(G) + loop:failed(B, loud) → judged:true both → finish-line notifications → ledger summary"
    return 0
}

if [ "${1:-}" = "--self-test" ]; then
    echo "== self-test 1/2: happy path (must PASS) =="
    if ! run_smoke honest; then
        echo "FAIL: self-test — happy path did not pass"
        exit 1
    fi
    cleanup
    SUP_PID=""
    SESSIONS=()
    echo "== self-test 2/2: sabotage — assert loop:closed for BOTH (must FAIL loud) =="
    if run_smoke sabotage; then
        echo "FAIL: self-test — sabotaged run passed (a judge that can't fail is decoration)"
        exit 1
    fi
    echo "PASS: self-test — judge passes on honest run AND fails when demanding closure for a failing worker"
    exit 0
fi

run_smoke honest
exit $?
