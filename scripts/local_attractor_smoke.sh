#!/usr/bin/env bash
# local_attractor_smoke.sh — THE LOCAL JUDGE for build step 5 (attractor
# async_ask producer). Same discipline as its siblings (context/judge-contract.md).
#
# Happy path: run evals/pipelines/gate.dot headless via `attention-manager
# workunit run` in the background from a scratch workdir; assert a
# kind=attractor-gate packet appears (options A/R, work_unit name, gate stage
# in context); answer A via the CLI; assert the pipeline completes exit 0,
# A.txt exists, R.txt is absent, and gate:packet_created / gate:answered /
# workunit:finished events + the workunit_finished ledger entry all landed.
#
# Sabotage direction: never answer — the workunit must STILL be awaiting at a
# short completion deadline, so a judge run that demands completion FAILs.
# That failure is the honest behavior (nothing fabricates an answer, D7).
#
# Dependency policy (FAIL LOUD, never skip silently): the real
# amplifier-module-loop-pipeline engine is required. If it is not importable,
# we attempt a user-env pip install; if it still cannot be imported, this
# judge FAILs with the reason and names the DTU (scenario 7) as the verifier.
#
# Exit 0 + "PASS: <reason>" or exit 1 + "FAIL: <reason>".
#
# --self-test runs the broken-test protocol: the happy direction MUST pass
# AND the sabotaged direction (demand completion without answering) MUST
# fail. A judge that can't fail is decoration.

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

am() { python3 -m attention_manager.cli "$@"; }

WU_PID=""
cleanup() {
    [ -n "$WU_PID" ] && kill "$WU_PID" 2>/dev/null
}
trap cleanup EXIT

ensure_loop_pipeline() {
    if python3 -c "import amplifier_module_loop_pipeline" 2>/dev/null; then
        return 0
    fi
    echo "== amplifier-module-loop-pipeline not importable; attempting user-env install =="
    python3 -m pip install --user \
        'amplifier-module-loop-pipeline @ git+https://github.com/microsoft/amplifier-bundle-attractor@main#subdirectory=modules/loop-pipeline' \
        || true
    if python3 -c "import amplifier_module_loop_pipeline" 2>/dev/null; then
        return 0
    fi
    echo "FAIL: amplifier-module-loop-pipeline is not importable and could not be installed into the user env — the local attractor smoke cannot run here. The DTU (evals/scenarios/scenario-7-attractor-gate.md) is the verifier for this step."
    exit 1
}

# run_smoke <mode>   mode = answer | sabotage
# Returns 0 on PASS, 1 on FAIL (echoes the reason).
run_smoke() {
    local mode="$1"
    local tmp
    tmp="$(mktemp -d)"
    export ATTENTION_HOME="$tmp/home"
    export ATTENTION_QUEUE_DIR="$tmp/queue"
    local workdir="$tmp/work"
    mkdir -p "$workdir"

    (cd "$workdir" && exec python3 -m attention_manager.cli workunit run \
        "$REPO_ROOT/evals/pipelines/gate.dot" --name smoke-gate) \
        >"$tmp/wu.log" 2>&1 &
    WU_PID=$!

    # Wait for the gate packet to appear.
    local pkt="" deadline=$((SECONDS + 30))
    while [ $SECONDS -lt $deadline ]; do
        pkt="$(am --json queue list 2>/dev/null \
            | python3 -c 'import json,sys; ps=json.load(sys.stdin); print(ps[0]["id"] if ps else "")')" || pkt=""
        [ -n "$pkt" ] && break
        if ! kill -0 "$WU_PID" 2>/dev/null; then
            echo "FAIL: workunit exited before publishing a gate packet — $(tail -5 "$tmp/wu.log")"
            return 1
        fi
        sleep 0.5
    done
    if [ -z "$pkt" ]; then
        echo "FAIL: no attractor-gate packet appeared within 30s"
        return 1
    fi

    # Assert the packet shape (kind, options A/R, work_unit, stage in context).
    # NOTE: the JSON goes to a file passed as argv — `python3 - <<heredoc` uses
    # stdin for the script itself, so piping data into it silently reads empty.
    if ! am --json queue show "$pkt" >"$tmp/packet.json"; then
        echo "FAIL: could not read packet $pkt back from the queue"
        return 1
    fi
    if ! python3 - "$tmp/packet.json" <<'PY'
import json, sys

with open(sys.argv[1], encoding="utf-8") as f:
    p = json.load(f)


def die(msg):
    print(f"packet assertion failed: {msg}")
    sys.exit(1)


if p.get("source", {}).get("kind") != "attractor-gate":
    die(f"source.kind={p.get('source', {}).get('kind')!r}, expected 'attractor-gate'")
if p.get("source", {}).get("work_unit") != "smoke-gate":
    die(f"source.work_unit={p.get('source', {}).get('work_unit')!r}, expected 'smoke-gate'")
ids = [o["id"] for o in p.get("options", [])]
if ids != ["A", "R"]:
    die(f"option ids={ids}, expected ['A', 'R']")
labels = [o["label"] for o in p.get("options", [])]
if labels != ["[A] Approve", "[R] Reject"]:
    die(f"option labels={labels}")
if "stage: gate" not in p.get("context", ""):
    die(f"gate stage missing from context: {p.get('context')!r}")
PY
    then
        echo "FAIL: attractor-gate packet shape is wrong (see assertion above)"
        return 1
    fi

    if [ "$mode" = "sabotage" ]; then
        # Never answer. Demand completion within a short window — the workunit
        # must honestly still be awaiting, so this direction FAILs.
        local sab_deadline=$((SECONDS + 6))
        while [ $SECONDS -lt $sab_deadline ]; do
            if ! kill -0 "$WU_PID" 2>/dev/null; then
                echo "FAIL: workunit completed WITHOUT an answer — something fabricated a resolution"
                return 1
            fi
            sleep 0.5
        done
        echo "FAIL: workunit still awaiting the unanswered gate at the completion deadline (honest block)"
        kill "$WU_PID" 2>/dev/null
        wait "$WU_PID" 2>/dev/null
        WU_PID=""
        return 1
    fi

    # Happy path: answer A via the CLI and require a clean finish.
    if ! am answer "$pkt" A --rationale "smoke approve"; then
        echo "FAIL: could not answer packet $pkt with option A"
        return 1
    fi
    wait "$WU_PID"
    local rc=$?
    WU_PID=""
    if [ "$rc" -ne 0 ]; then
        echo "FAIL: workunit exited $rc after the answer — $(tail -5 "$tmp/wu.log")"
        return 1
    fi
    if [ ! -f "$workdir/A.txt" ]; then
        echo "FAIL: A.txt was not created — the answer did not route the [A] Approve edge"
        return 1
    fi
    if [ -f "$workdir/R.txt" ]; then
        echo "FAIL: R.txt exists — the pipeline took the reject edge (misroute)"
        return 1
    fi
    local ev="$ATTENTION_HOME/events.jsonl"
    for event in "gate:packet_created" "gate:answered" "workunit:finished"; do
        if ! grep -q "\"event\": \"$event\"" "$ev" 2>/dev/null; then
            echo "FAIL: event $event missing from $ev"
            return 1
        fi
    done
    if ! grep -qr "\"kind\": \"workunit_finished\"" "$ATTENTION_HOME/ledger/" 2>/dev/null; then
        echo "FAIL: workunit_finished ledger entry missing"
        return 1
    fi
    echo "PASS: gate packet published (A/R, smoke-gate, stage gate), answer A routed the pipeline to A.txt, exit 0, events + ledger recorded"
    return 0
}

ensure_loop_pipeline

if [ "${1:-}" = "--self-test" ]; then
    echo "== self-test 1/2: happy path (must PASS) =="
    if ! run_smoke answer; then
        echo "FAIL: self-test — happy path did not pass"
        exit 1
    fi
    echo "== self-test 2/2: sabotage — never answer, demand completion (must FAIL loud) =="
    if run_smoke sabotage; then
        echo "FAIL: self-test — sabotaged run passed (a judge that can't fail is decoration)"
        exit 1
    fi
    echo "PASS: self-test — judge passes the honest run AND fails when the gate is left unanswered"
    exit 0
fi

run_smoke answer
