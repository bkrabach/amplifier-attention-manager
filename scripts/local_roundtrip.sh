#!/usr/bin/env bash
# local_roundtrip.sh — THE LOCAL JUDGE for build step 1 (see context/judge-contract.md).
#
# Happy path: a "worker" subprocess calls the tool-request-decision module's
# execute() (its own packet IO) → the packet appears in the queue → this script
# answers option B via the attention-manager CLI (root queue lib) → the worker
# unblocks with answer B and the packet lands in answered/ with resolution.
#
# Exit 0 + "PASS: <reason>" or exit 1 + "FAIL: <reason>".
#
# --self-test runs the broken-test protocol (judge-contract.md): the happy
# direction MUST pass AND a sabotaged run (answer step skipped, tiny timeout)
# MUST fail. A judge that never fails is decoration.

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
export PYTHONPATH="$REPO_ROOT/src:$REPO_ROOT/modules/tool-request-decision${PYTHONPATH:+:$PYTHONPATH}"

am() { python3 -m attention_manager.cli "$@"; }

run_worker() {
    # $1 = max_wait_seconds for the tool. Prints "ANSWER=<id> BY=<by>" on success.
    python3 - "$1" <<'PY'
import asyncio, sys
from amplifier_module_tool_request_decision import RequestDecisionTool

async def main() -> int:
    tool = RequestDecisionTool(config={"poll_interval_s": 0.2, "max_wait_seconds": float(sys.argv[1])})
    result = await tool.execute({
        "question": "Roundtrip test: option A or option B?",
        "options": [
            {"id": "A", "label": "Option A", "consequence": "path A taken"},
            {"id": "B", "label": "Option B", "consequence": "path B taken"},
        ],
        "recommendation": {"option": "A", "rationale": "test recommendation", "confidence": "low"},
        "context": "local_roundtrip.sh judge run",
    })
    if result.success:
        print(f"ANSWER={result.output['answer']} BY={result.output['answered_by']}")
        return 0
    print(f"WORKER-ERROR: {result.output}")
    return 1

sys.exit(asyncio.run(main()))
PY
}

check_answered_file() {
    # $1 = packet id. Verifies resolution fields in answered/<id>.json.
    python3 - "$1" <<'PY'
import json, os, sys
from pathlib import Path

pid = sys.argv[1]
path = Path(os.environ["ATTENTION_QUEUE_DIR"]) / "answered" / f"{pid}.json"
if not path.exists():
    print(f"missing {path}")
    sys.exit(1)
res = json.loads(path.read_text(encoding="utf-8")).get("resolution") or {}
ok = res.get("answer") == "B" and res.get("answered_by") == "human" and res.get("answered_at") and res.get("rationale") == "test"
print(f"resolution={res}")
sys.exit(0 if ok else 1)
PY
}

run_roundtrip() {
    # $1 = "answer" (happy path) or "skip-answer" (sabotage: worker must fail loud)
    local mode="$1"
    local worker_wait worker_out worker_rc pkt_id
    export ATTENTION_QUEUE_DIR="$(mktemp -d)"
    worker_out="$(mktemp)"

    if [ "$mode" = "answer" ]; then worker_wait=60; else worker_wait=3; fi

    run_worker "$worker_wait" >"$worker_out" 2>&1 &
    local worker_pid=$!

    # Poll the queue via the CLI until the packet appears (15s budget).
    pkt_id=""
    for _ in $(seq 1 75); do
        pkt_id="$(am --json queue list | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["id"] if d else "")')" || true
        [ -n "$pkt_id" ] && break
        sleep 0.2
    done
    if [ -z "$pkt_id" ]; then
        kill "$worker_pid" 2>/dev/null
        echo "FAIL: no packet appeared in the queue within 15s"
        return 1
    fi
    echo "  packet observed: $pkt_id"

    if [ "$mode" = "answer" ]; then
        if ! am answer "$pkt_id" B --rationale test; then
            kill "$worker_pid" 2>/dev/null
            echo "FAIL: CLI answer command failed"
            return 1
        fi
    else
        echo "  (sabotage: skipping the answer step)"
    fi

    wait "$worker_pid"
    worker_rc=$?
    echo "  worker output: $(cat "$worker_out")"

    if [ "$mode" = "answer" ]; then
        if [ $worker_rc -ne 0 ] || ! grep -q "ANSWER=B" "$worker_out"; then
            echo "FAIL: worker did not receive answer B (rc=$worker_rc)"
            return 1
        fi
        if ! check_answered_file "$pkt_id"; then
            echo "FAIL: answered/$pkt_id.json missing or resolution fields wrong"
            return 1
        fi
        echo "PASS: packet $pkt_id written by tool, answered B via CLI, worker unblocked, resolution verified"
        return 0
    else
        # Sabotaged run: worker MUST fail loud, packet MUST stay pending.
        if [ $worker_rc -eq 0 ]; then
            echo "FAIL: sabotaged run — worker succeeded without an answer (silent default?)"
            return 1
        fi
        if ! grep -q "fail-loud" "$worker_out"; then
            echo "FAIL: sabotaged run — worker error is not the loud unanswered-timeout message"
            return 1
        fi
        echo "PASS: sabotaged run failed loudly as required (rc=$worker_rc, packet stayed pending)"
        return 0
    fi
}

if [ "${1:-}" = "--self-test" ]; then
    # Broken-test protocol: PASS on known-good, FAIL on deliberately broken.
    echo "== self-test 1/2: happy path (must PASS) =="
    if ! run_roundtrip answer; then
        echo "FAIL: self-test — happy path did not pass"
        exit 1
    fi
    echo "== self-test 2/2: sabotaged path, answer skipped (must fail loud) =="
    if ! run_roundtrip skip-answer; then
        echo "FAIL: self-test — sabotaged run did not fail as required"
        exit 1
    fi
    echo "PASS: self-test — judge passes on good run AND fails on sabotaged run"
    exit 0
fi

run_roundtrip answer
exit $?
