#!/usr/bin/env bash
# local_triage_smoke.sh — THE LOCAL JUDGE for build step 3 (rulebook + cold triage).
# Same discipline as local_roundtrip.sh / local_supervisor_smoke.sh.
#
# Uses a FAKE amplifier binary (no real LLM locally — real-LLM triage is the
# DTU eval's job). The stub parses the runner's machine-greppable prompt
# header (PHASE / PACKET_ID / OUTPUT_PATH) and writes canned verdict files,
# exercising the FULL disk-based verdict protocol end to end:
#
# Happy path: seed rulebook + 4 packets (decidable / cold-undecidable /
# invalid-verdict / PRE-ANSWERED — human-answered before any triage pass).
# One `triage --once` pass must: fill triage fields + recommendation on the
# decidable packet (atomically, still pending), move the undecidable one to
# bounced/ with a reason, handle the invalid-verdict one loudly (2
# triage:error events — one retry max — packet untouched, NO fabricated
# verdict), AND propose a rule_delta for the pre-answered packet (every
# human answer compounds, unconditionally — triaged or not; the dogfood
# defect was exactly this packet class silently never proposing). Then:
# answer the good packet, second pass proposes its rule_delta too
# (idempotent on a third pass), and `rulebook apply` lands the sentence in
# the correct section.
#
# Exit 0 + "PASS: <reason>" or exit 1 + "FAIL: <reason>".
#
# --self-test runs the broken-test protocol: the happy direction MUST pass
# AND a sabotaged run MUST fail loud. Sabotage = the harness SKIPS running
# triage entirely but still asserts triage outcomes — a judge that passes
# without triage having run is decoration.

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

am() { python3 -m attention_manager.cli "$@"; }

write_fake_amplifier() {
    # $1 = destination file. The FAKE amplifier CLI: accepts `run -B <uri>
    # <prompt>`, greps the prompt header, writes a canned verdict per packet
    # markers in the prompt (COLD-UNDECIDABLE / INVALID-VERDICT / default).
    cat >"$1" <<'PY'
#!/usr/bin/env python3
import json, re, sys

prompt = sys.argv[-1]
phase = re.search(r"^PHASE: (\S+)", prompt, re.M).group(1)
packet_id = re.search(r"^PACKET_ID: (\S+)", prompt, re.M).group(1)
out = re.search(r"^OUTPUT_PATH: (.+)$", prompt, re.M).group(1).strip()

def write(data):
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f)

if phase == "triage":
    if "COLD-UNDECIDABLE" in prompt:
        write({"packet_id": packet_id, "decision": "bounce", "recommendation": None,
               "why": "cold-reader test failed", "rule_refs": [],
               "bounce_reason": "question references material not in the packet"})
    elif "INVALID-VERDICT" in prompt:
        with open(out, "w", encoding="utf-8") as f:
            f.write("{this is not json")
    else:
        write({"packet_id": packet_id, "decision": "recommend",
               "recommendation": {"option": "A", "rationale": "packet facts favor A", "confidence": "high"},
               "why": "decidable cold from packet + rulebook",
               "rule_refs": ["Attention priorities"]})
else:  # rule_delta
    write({"packet_id": packet_id, "none": False, "section": "Auto-answer rules",
           "sentence": "Prefer option A for smoke-class decisions.",
           "reason": "this class recurs and the answer is mechanical"})
PY
    chmod +x "$1"
}

seed_packets() {
    # Seeds 4 packets via the ROOT queue lib; prints the four ids in order:
    # good / undecidable / invalid-verdict / pre-answered (human-answered
    # BEFORE any triage pass — must still produce a rule_delta proposal).
    python3 - <<'PY'
from attention_manager.packet import Option, Packet, Source
from attention_manager.queue import PacketQueue

q = PacketQueue()
good = Packet(
    question="smoke: proceed with plan A or plan B?",
    options=[Option(id="A", label="Plan A", consequence="fast"), Option(id="B", label="Plan B", consequence="slow")],
    source=Source(kind="decision", muxplex_session="am-smoke"),
    context="All facts needed to decide are right here.",
)
undecidable = Packet(
    question="smoke COLD-UNDECIDABLE: apply the fix discussed earlier?",
    options=[Option(id="A", label="Yes"), Option(id="B", label="No")],
    source=Source(kind="decision"),
    context="(references a discussion not included in this packet)",
)
invalid = Packet(
    question="smoke INVALID-VERDICT: the session will write a broken verdict",
    options=[Option(id="A", label="Yes"), Option(id="B", label="No")],
    source=Source(kind="decision"),
    context="Used to prove the runner never accepts a malformed verdict.",
)
pre_answered = Packet(
    question="smoke: answered before triage ever saw it — must still compound",
    options=[Option(id="A", label="Yes"), Option(id="B", label="No")],
    source=Source(kind="decision"),
    context="Human answered this before any triage pass ran.",
)
for p in (good, undecidable, invalid, pre_answered):
    q.write(p)
q.answer(pre_answered.id, "A", rationale="answered pre-triage", answered_by="human")
print(good.id)
print(undecidable.id)
print(invalid.id)
print(pre_answered.id)
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

run_smoke() {
    # $1 = "triage" (happy path) or "skip-triage" (sabotage: judge must FAIL)
    local mode="$1"
    export ATTENTION_HOME="$(mktemp -d)"
    export ATTENTION_QUEUE_DIR="$(mktemp -d)"
    export ATTENTION_TRIAGE_BUNDLE="smoke://triage-bundle"
    export ATTENTION_AMPLIFIER_BIN="$ATTENTION_HOME/fake-amplifier"
    write_fake_amplifier "$ATTENTION_AMPLIFIER_BIN"

    # Rulebook created from template on first use, with the design's sections.
    if ! am rulebook show | grep -q "## Auto-answer rules"; then
        echo "FAIL: rulebook template missing '## Auto-answer rules' section"
        return 1
    fi
    echo "  rulebook created from template (design sections present)"

    local ids good_id undecidable_id invalid_id pre_answered_id
    ids="$(seed_packets)" || { echo "FAIL: could not seed packets"; return 1; }
    good_id="$(echo "$ids" | sed -n 1p)"
    undecidable_id="$(echo "$ids" | sed -n 2p)"
    invalid_id="$(echo "$ids" | sed -n 3p)"
    pre_answered_id="$(echo "$ids" | sed -n 4p)"
    echo "  seeded packets: good=$good_id undecidable=$undecidable_id invalid=$invalid_id pre-answered=$pre_answered_id"

    if [ "$mode" = "triage" ]; then
        if ! am triage --once >"$ATTENTION_HOME/triage-pass-1.out" 2>&1; then
            echo "FAIL: triage --once exited non-zero: $(cat "$ATTENTION_HOME/triage-pass-1.out")"
            return 1
        fi
    else
        echo "  (sabotage: skipping the triage pass entirely)"
    fi

    # -- good packet: triage fields + recommendation filled, STILL pending ----
    if ! python3 - "$good_id" <<'PY'
import sys
from attention_manager.queue import PacketQueue
q = PacketQueue()
subdir, _ = q.locate(sys.argv[1])
p = q.get(sys.argv[1])
assert subdir == "pending", f"good packet in {subdir}/, expected pending/ (Phase 1 is recommend-only)"
assert p.triage is not None, "good packet has NO triage fields"
assert p.triage.handled_by == "manager-recommend", f"handled_by={p.triage.handled_by!r}"
assert p.triage.why and "recommend A" in p.triage.why, f"triage.why={p.triage.why!r}"
assert p.recommendation is not None and p.recommendation.option == "A", "recommendation not filled"
PY
    then
        echo "FAIL: good packet was not triaged (no recommend verdict recorded)"
        return 1
    fi
    echo "  good packet: triage fields + recommendation filled, still pending"

    # -- undecidable packet: moved to bounced/ with the reason ----------------
    if ! python3 - "$undecidable_id" <<'PY'
import sys
from attention_manager.queue import PacketQueue
q = PacketQueue()
subdir, _ = q.locate(sys.argv[1])
assert subdir == "bounced", f"undecidable packet in {subdir}/, expected bounced/"
p = q.get(sys.argv[1])
assert p.triage is not None and "bounce:" in (p.triage.why or ""), "bounce reason not merged into triage.why"
PY
    then
        echo "FAIL: cold-undecidable packet was not bounced"
        return 1
    fi
    echo "  undecidable packet: moved to bounced/ with reason in triage.why"

    # -- invalid-verdict packet: loud triage:error, packet UNTOUCHED ----------
    if ! python3 - "$invalid_id" <<'PY'
import sys
from attention_manager.queue import PacketQueue
q = PacketQueue()
subdir, _ = q.locate(sys.argv[1])
assert subdir == "pending", f"invalid-verdict packet in {subdir}/, expected pending/ (untouched)"
assert q.get(sys.argv[1]).triage is None, "invalid verdict must never fill triage fields"
PY
    then
        echo "FAIL: invalid-verdict packet was modified (a fabricated/accepted bad verdict?)"
        return 1
    fi
    if [ "$(count_events "triage:error")" -ne 2 ]; then
        echo "FAIL: expected exactly 2 triage:error events (one retry max, both logged), got $(count_events triage:error)"
        return 1
    fi
    if [ "$(count_events "triage:recommended")" -ne 1 ] || [ "$(count_events "triage:bounced")" -ne 1 ]; then
        echo "FAIL: expected 1 triage:recommended + 1 triage:bounced events, got $(count_events triage:recommended)/$(count_events triage:bounced)"
        return 1
    fi
    echo "  invalid verdict: 2 loud triage:error events (retry once), packet untouched"

    # -- pre-answered packet: proposal from PASS 1 (answered before triage) ----
    if ! python3 - "$pre_answered_id" <<'PY'
import os, sys
from attention_manager.rulebook import Rulebook
records = [r for r in Rulebook(home=os.environ["ATTENTION_HOME"]).list_proposals()
           if r.get("packet_id") == sys.argv[1]]
assert len(records) == 1, f"pre-answered packet has {len(records)} proposal records, expected 1"
assert records[0]["status"] in ("proposed", "none"), records[0]
PY
    then
        echo "FAIL: packet answered BEFORE triage produced no rule_delta proposal (every answer must compound)"
        return 1
    fi
    echo "  pre-answered packet: rule_delta proposal produced on the FIRST pass (unconditional compounding)"

    # -- ledger tells the triage story ----------------------------------------
    if ! python3 - <<'PY'
import json, os, sys
from pathlib import Path
kinds = []
for f in (Path(os.environ["ATTENTION_HOME"]) / "ledger").glob("*.jsonl"):
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            kinds.append(json.loads(line)["kind"])
missing = {"triage_recommended", "triage_bounced"} - set(kinds)
if missing:
    print(f"ledger missing kinds: {missing} (has {kinds})")
    sys.exit(1)
PY
    then
        echo "FAIL: ledger does not record the triage story"
        return 1
    fi
    echo "  ledger records triage_recommended + triage_bounced"

    # -- answer the good packet, second pass -> its rule_delta (2 total) ------
    if ! am answer "$good_id" A --rationale "smoke: agree with triage" >/dev/null; then
        echo "FAIL: could not answer the good packet"
        return 1
    fi
    if ! am triage --once >"$ATTENTION_HOME/triage-pass-2.out" 2>&1; then
        echo "FAIL: second triage pass exited non-zero: $(cat "$ATTENTION_HOME/triage-pass-2.out")"
        return 1
    fi
    local proposal_id
    proposal_id="$(am --json rulebook proposals | python3 -c "
import json, sys
proposals = [p for p in json.load(sys.stdin) if p.get('status') == 'proposed']
assert len(proposals) == 2, f'expected exactly 2 proposals (pre-answered + good), got {len(proposals)}'
good = [p for p in proposals if p['packet_id'] == '$good_id']
assert len(good) == 1, f'no proposal for the good packet: {proposals}'
assert good[0]['section'] == 'Auto-answer rules', good[0]
print(good[0]['id'])
")" || { echo "FAIL: expected exactly TWO rule_delta proposals after the answer (pre-answered + good)"; return 1; }
    if [ "$(count_events "rule_delta:proposed")" -ne 2 ]; then
        echo "FAIL: expected 2 rule_delta:proposed events, got $(count_events rule_delta:proposed)"
        return 1
    fi
    echo "  second pass: good packet's rule_delta proposed ($proposal_id); 2 proposals total"

    # -- idempotency: third pass must not re-propose --------------------------
    am triage --once >/dev/null 2>&1
    if [ "$(am --json rulebook proposals | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')" -ne 2 ]; then
        echo "FAIL: third pass double-proposed for the same packet (idempotency broken)"
        return 1
    fi
    echo "  third pass: still exactly two proposals (idempotent)"

    # -- apply lands the sentence in the RIGHT section -------------------------
    if ! am rulebook apply "$proposal_id" >/dev/null; then
        echo "FAIL: rulebook apply $proposal_id failed"
        return 1
    fi
    if ! am rulebook show | python3 -c '
import sys
content = sys.stdin.read()
section = content.split("## Auto-answer rules", 1)[1].split("## ", 1)[0]
assert "- Prefer option A for smoke-class decisions." in section, "sentence not under Auto-answer rules"
'
    then
        echo "FAIL: applied rule did not land under '## Auto-answer rules'"
        return 1
    fi
    echo "  rulebook apply: sentence landed under '## Auto-answer rules'"

    echo "PASS: rulebook template -> triage pass (recommend/bounce/loud-error) -> answer -> ONE rule_delta -> idempotent -> apply-to-section verified"
    return 0
}

if [ "${1:-}" = "--self-test" ]; then
    echo "== self-test 1/2: happy path (must PASS) =="
    if ! run_smoke triage; then
        echo "FAIL: self-test — happy path did not pass"
        exit 1
    fi
    echo "== self-test 2/2: sabotaged path, triage skipped (must FAIL loud) =="
    if run_smoke skip-triage; then
        echo "FAIL: self-test — sabotaged run passed (judge is decoration)"
        exit 1
    fi
    echo "PASS: self-test — judge passes on good run AND fails on sabotaged run"
    exit 0
fi

run_smoke triage
exit $?
