#!/usr/bin/env bash
# local_trust_smoke.sh — THE LOCAL JUDGE for build step 6 (graduated trust
# Phase 2 + recipe-gate poller). Same discipline as the sibling judges.
#
# Uses a FAKE amplifier binary (no real LLM / no real recipes tool locally).
# The stub serves BOTH shapes the manager shells out to:
#   * `amplifier run -B <uri> <prompt>`   — triage/rule_delta verdict files
#     (recommend option A, confidence high, citing "Auto-answer rules")
#   * `amplifier tool invoke recipes ...` — the REAL observed `-o json`
#     envelope (noise line + result as a Python-repr string), logging every
#     invocation for assertion.
#
# Happy path:
#   1. Rulebook starts phase 1. Five consecutive matching human answers
#      (triage pass -> answer A -> rule_delta pass) walk the streak 1..5 and
#      the 5th PROMOTES the section — visible in the rulebook heading
#      annotation `<!-- phase:2 streak:5 -->` + exactly one trust:promoted.
#   2. The 6th packet AUTO-ANSWERS: canonical resolved packet in answered/
#      (answered_by manager-auto — what producers poll), review record in
#      queue/auto/ (reviewed:false), pending/ empty of it.
#   3. `auto reject` records the correction and DEMOTES the section back to
#      `<!-- phase:1 streak:0 -->` (loud trust:demoted).
#   4. Recipe poller: a pending approval becomes ONE recipe-gate packet
#      (dedupe across polls); answering it `approve` forwards
#      `operation=approve session_id=... stage_name=... message=<rationale>`
#      to the (fake) recipes tool; recipe_gates:resolved lands; then a
#      background `operation=resume session_id=...` is launched exactly once
#      (approve only MARKS the stage — resume continues the recipe) with its
#      output captured to <home>/recipe-gates/<session>.resume.log.
#
# Exit 0 + "PASS: <reason>" or exit 1 + "FAIL: <reason>".
#
# --self-test runs the broken-test protocol: the happy direction MUST pass
# AND a sabotaged run MUST fail loud. Sabotage = the harness SKIPS every
# manager pass (triage, auto review, recipe poll) but still asserts the
# outcomes — a judge that passes without the manager having run is decoration.

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

am() { python3 -m attention_manager.cli "$@"; }

write_fake_amplifier() {
    # $1 = destination file. Serves `run -B` (triage verdicts) AND
    # `tool invoke recipes` (approvals/approve/deny with the real envelope).
    cat >"$1" <<'PY'
#!/usr/bin/env python3
import json, os, re, sys

args = sys.argv[1:]

if args and args[0] == "run":  # triage / rule_delta one-shot session
    prompt = args[-1]
    phase = re.search(r"^PHASE: (\S+)", prompt, re.M).group(1)
    packet_id = re.search(r"^PACKET_ID: (\S+)", prompt, re.M).group(1)
    out = re.search(r"^OUTPUT_PATH: (.+)$", prompt, re.M).group(1).strip()
    with open(out, "w", encoding="utf-8") as f:
        if phase == "triage":
            json.dump({"packet_id": packet_id, "decision": "recommend",
                       "recommendation": {"option": "A", "rationale": "rule covers this", "confidence": "high"},
                       "why": "covered by the cited rules",
                       "rule_refs": ["Auto-answer rules"]}, f)
        else:  # rule_delta
            json.dump({"packet_id": packet_id, "none": True, "reason": "rule already exists"}, f)
    sys.exit(0)

# tool invoke recipes ... -o json  (the recipe-gate poller path)
log = os.environ.get("FAKE_INVOKE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\n")
kv = dict(a.split("=", 1) for a in args if "=" in a)
op = kv.get("operation")
if op == "approvals":
    path = os.environ.get("FAKE_APPROVALS_FILE", "")
    approvals = json.loads(open(path, encoding="utf-8").read()) if path and os.path.exists(path) else []
    result = {"pending_approvals": approvals, "count": len(approvals)}
else:
    result = {"session_id": kv.get("session_id"), "stage_name": kv.get("stage_name"), "status": "ok"}
print("Bundle 'amplifier-dev' prepared successfully")   # real observed noise line
print(json.dumps({"status": "success", "tool": "recipes", "result": str(result)}, indent=2))
PY
    chmod +x "$1"
}

seed_packet() {
    # Seeds one decidable A/B decision packet; prints its id.
    python3 - <<'PY'
from attention_manager.packet import Option, Packet, Source
from attention_manager.queue import PacketQueue

q = PacketQueue()
p = Packet(
    question="smoke: proceed with plan A or plan B?",
    options=[Option(id="A", label="Plan A", consequence="fast"), Option(id="B", label="Plan B", consequence="slow")],
    source=Source(kind="decision", muxplex_session="am-smoke"),
    context="All facts needed to decide are right here.",
)
q.write(p)
print(p.id)
PY
}

count_events() {
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

header_state() {
    # Prints the "phase:P streak:S" annotation of ## Auto-answer rules ("phase:1 streak:0" when bare).
    python3 - <<'PY'
from attention_manager.rulebook import Rulebook
phase, streak = Rulebook().get_section_state("Auto-answer rules")
print(f"phase:{phase} streak:{streak}")
PY
}

run_smoke() {
    # $1 = "trust" (happy path) or "skip-passes" (sabotage: judge must FAIL)
    local mode="$1"
    export ATTENTION_HOME="$(mktemp -d)"
    export ATTENTION_QUEUE_DIR="$(mktemp -d)"
    export ATTENTION_TRIAGE_BUNDLE="smoke://triage-bundle"
    export ATTENTION_AMPLIFIER_BIN="$ATTENTION_HOME/fake-amplifier"
    export FAKE_INVOKE_LOG="$ATTENTION_HOME/invoke.log"
    export FAKE_APPROVALS_FILE="$ATTENTION_HOME/approvals.json"
    write_fake_amplifier "$ATTENTION_AMPLIFIER_BIN"

    run_pass() {  # every manager pass goes through here so sabotage can skip them all
        if [ "$mode" = "trust" ]; then am "$@"; else return 0; fi
    }

    # -- part 1: five matching human answers walk the streak to promotion -----
    local i id
    for i in 1 2 3 4 5; do
        id="$(seed_packet)" || { echo "FAIL: could not seed packet $i"; return 1; }
        run_pass triage --once >/dev/null 2>&1 || { echo "FAIL: triage pass $i (recommend) exited non-zero"; return 1; }
        am answer "$id" A --rationale "smoke: agree with triage" >/dev/null \
            || { echo "FAIL: could not answer packet $i"; return 1; }
        run_pass triage --once >/dev/null 2>&1 || { echo "FAIL: triage pass $i (rule_delta) exited non-zero"; return 1; }
        local expected
        if [ "$i" -lt 5 ]; then expected="phase:1 streak:$i"; else expected="phase:2 streak:5"; fi
        if [ "$(header_state)" != "$expected" ]; then
            echo "FAIL: after matching answer $i expected '$expected' in the rulebook header, got '$(header_state)'"
            return 1
        fi
    done
    if ! grep -q '## Auto-answer rules <!-- phase:2 streak:5 -->' "$ATTENTION_HOME/rulebook.md"; then
        echo "FAIL: promotion not visible as a heading annotation in rulebook.md"
        return 1
    fi
    if [ "$(count_events "trust:promoted")" -ne 1 ]; then
        echo "FAIL: expected exactly 1 trust:promoted event, got $(count_events trust:promoted)"
        return 1
    fi
    echo "  5 consecutive matching human answers -> '## Auto-answer rules <!-- phase:2 streak:5 -->' (1 trust:promoted)"

    # -- part 2: the 6th packet auto-answers ----------------------------------
    local auto_id
    auto_id="$(seed_packet)" || { echo "FAIL: could not seed the 6th packet"; return 1; }
    run_pass triage --once >/dev/null 2>&1 || { echo "FAIL: triage pass 6 exited non-zero"; return 1; }
    if ! python3 - "$auto_id" <<'PY'
import json, sys
from attention_manager.queue import PacketQueue
q = PacketQueue()
subdir, _ = q.locate(sys.argv[1])
assert subdir == "answered", f"6th packet in {subdir}/, expected answered/ (auto-answer)"
p = q.get(sys.argv[1])
assert p.resolution is not None and p.resolution.answered_by == "manager-auto", "resolution not manager-auto"
assert p.resolution.answer == "A", f"auto answer {p.resolution.answer!r} != recommended A"
assert not q.path_for(sys.argv[1], "pending").exists(), "pending copy still present"
record = json.loads((q.root / "auto" / f"{sys.argv[1]}.json").read_text(encoding="utf-8"))
assert record["reviewed"] is False and record["answer"] == "A", f"bad auto record: {record}"
assert record["sections"] == ["Auto-answer rules"], f"bad sections: {record['sections']}"
PY
    then
        echo "FAIL: 6th packet was not auto-answered (answered/ + queue/auto/ record)"
        return 1
    fi
    if [ "$(count_events "triage:auto_answered")" -ne 1 ]; then
        echo "FAIL: expected exactly 1 triage:auto_answered event, got $(count_events triage:auto_answered)"
        return 1
    fi
    echo "  6th packet auto-answered: canonical copy in answered/ (manager-auto) + unreviewed record in queue/auto/"

    # -- part 3: auto reject -> demotion visible ------------------------------
    run_pass auto reject "$auto_id" --correct-option B --reason "smoke: human disagrees" >/dev/null 2>&1 \
        || { echo "FAIL: auto reject exited non-zero"; return 1; }
    if [ "$(header_state)" != "phase:1 streak:0" ]; then
        echo "FAIL: after auto reject expected 'phase:1 streak:0', got '$(header_state)'"
        return 1
    fi
    if ! grep -q '## Auto-answer rules <!-- phase:1 streak:0 -->' "$ATTENTION_HOME/rulebook.md"; then
        echo "FAIL: demotion not visible as a heading annotation in rulebook.md"
        return 1
    fi
    if [ "$(count_events "trust:demoted")" -ne 1 ]; then
        echo "FAIL: expected exactly 1 trust:demoted event, got $(count_events trust:demoted)"
        return 1
    fi
    echo "  auto reject: correction recorded, section demoted to '<!-- phase:1 streak:0 -->' (1 trust:demoted)"

    # -- part 4: recipe-gate poller loop --------------------------------------
    cat >"$FAKE_APPROVALS_FILE" <<'JSON'
[{"session_id": "recipe_smoke_1234", "recipe_name": "smoke-recipe", "stage_name": "deploy",
  "approval_prompt": "Deploy the smoke build?", "approval_timeout": 0,
  "approval_requested_at": "2026-07-28T06:00:00", "approval_default": "deny"}]
JSON
    run_pass recipes poll --once >/dev/null 2>&1 || { echo "FAIL: recipes poll (packetize) exited non-zero"; return 1; }
    run_pass recipes poll --once >/dev/null 2>&1 || { echo "FAIL: recipes poll (dedupe) exited non-zero"; return 1; }
    local gate_id
    gate_id="$(python3 - <<'PY'
from attention_manager.queue import PacketQueue
pending = [p for p in PacketQueue().list_pending() if p.source.kind == "recipe-gate"]
assert len(pending) == 1, f"expected exactly 1 recipe-gate packet, got {len(pending)}"
p = pending[0]
assert p.source.work_unit == "recipe_smoke_1234", p.source.work_unit
assert p.option_ids() == ["approve", "deny"], p.option_ids()
assert p.question == "Deploy the smoke build?", p.question
print(p.id)
PY
)" || { echo "FAIL: pending approval was not packetized exactly once (kind=recipe-gate)"; return 1; }
    am answer "$gate_id" approve --rationale "smoke: ship it" >/dev/null \
        || { echo "FAIL: could not answer the recipe-gate packet"; return 1; }
    run_pass recipes poll --once >/dev/null 2>&1 || { echo "FAIL: recipes poll (forward) exited non-zero"; return 1; }
    if ! python3 - <<'PY'
import json, os, sys
from pathlib import Path
log = Path(os.environ["FAKE_INVOKE_LOG"])
calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
approve = [c for c in calls if "operation=approve" in c]
assert len(approve) == 1, f"expected exactly 1 approve invocation, got {len(approve)} (calls: {calls})"
call = approve[0]
assert "session_id=recipe_smoke_1234" in call, call
assert "stage_name=deploy" in call, call
assert "message=smoke: ship it" in call, call
PY
    then
        echo "FAIL: the approve was not forwarded to the recipes tool with the rationale as message"
        return 1
    fi
    if [ "$(count_events "recipe_gates:resolved")" -ne 1 ]; then
        echo "FAIL: expected exactly 1 recipe_gates:resolved event, got $(count_events recipe_gates:resolved)"
        return 1
    fi

    # -- resume launched in the background exactly once (fire-and-forget) -----
    if ! python3 - <<'PY'
import json, os, sys, time
from pathlib import Path
log = Path(os.environ["FAKE_INVOKE_LOG"])
def resume_calls():
    if not log.exists():
        return []
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [c for c in calls if "operation=resume" in c]
# resume is a BACKGROUND child — poll for its stub log write.
deadline = time.monotonic() + 10.0
while time.monotonic() < deadline and not resume_calls():
    time.sleep(0.1)
calls = resume_calls()
assert len(calls) == 1, f"expected exactly 1 resume invocation, got {calls}"
assert "session_id=recipe_smoke_1234" in calls[0], calls[0]
resume_log = Path(os.environ["ATTENTION_HOME"]) / "recipe-gates" / "recipe_smoke_1234.resume.log"
assert resume_log.exists(), f"resume log {resume_log} was not created"
PY
    then
        echo "FAIL: resume was not launched in the background after the approve"
        return 1
    fi
    run_pass recipes poll --once >/dev/null 2>&1 || { echo "FAIL: recipes poll (idempotency) exited non-zero"; return 1; }
    sleep 0.3   # long enough for a wrongly re-launched resume to have logged
    if ! python3 - <<'PY'
import json, os, sys
from pathlib import Path
log = Path(os.environ["FAKE_INVOKE_LOG"])
calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
resume = [c for c in calls if "operation=resume" in c]
assert len(resume) == 1, f"resume launched more than once across polls: {resume}"
PY
    then
        echo "FAIL: resume was launched twice for the same gate (idempotency broken)"
        return 1
    fi
    if [ "$(count_events "recipe_gates:resume_launched")" -ne 1 ]; then
        echo "FAIL: expected exactly 1 recipe_gates:resume_launched event, got $(count_events recipe_gates:resume_launched)"
        return 1
    fi
    echo "  recipe gate: packetized once (deduped), approve forwarded with message, resume launched in background exactly once"

    echo "PASS: streak->promotion visible in header -> auto-answer (answered/ + auto record) -> reject demotes -> recipe gate packetized + approve forwarded"
    return 0
}

if [ "${1:-}" = "--self-test" ]; then
    echo "== self-test 1/2: happy path (must PASS) =="
    if ! run_smoke trust; then
        echo "FAIL: self-test — happy path did not pass"
        exit 1
    fi
    echo "== self-test 2/2: sabotaged path, all manager passes skipped (must FAIL loud) =="
    if run_smoke skip-passes; then
        echo "FAIL: self-test — sabotaged run passed (judge is decoration)"
        exit 1
    fi
    echo "PASS: self-test — judge passes on good run AND fails on sabotaged run"
    exit 0
fi

run_smoke trust
exit $?
