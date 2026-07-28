#!/usr/bin/env python3
"""attention-manager evaluation harness (build steps 1-6).

Runs the scenarios in evals/scenarios/ INSIDE an already-running DTU
(or any environment reachable via an exec command prefix):

    python evals/run_evals.py \\
        --dtu-exec "amplifier-digital-twin exec --stream dtu-a1b2c3d4" \\
        --output-dir /path/to/results

    python evals/run_evals.py --dry-run          # no DTU needed: print plans
    python evals/run_evals.py --scenario 2 ...   # run one scenario

Exec-prefix contract: `<prefix> -- bash -c '<command>'` must execute the
command inside the target environment. Raw-passthrough prefixes (e.g.
`amplifier-digital-twin exec --stream <id>`, `docker exec <c>`,
`incus exec <c>`) are preferred; the JSON envelope printed by
`amplifier-digital-twin exec <id>` (no --stream) is auto-detected and
unwrapped.

Design decision: this is a small self-contained script by intent. The
amplifier_evaluation library's harness owns the full DTU lifecycle
(launch -> install -> grade -> destroy per trial); our contract is "run
inside an existing DTU via an exec prefix", and every grader assertion here
is mechanical (files, strings, exit codes), so we follow the library's
structural conventions (per-scenario state capture, structured results,
honest verdicts) without importing it.

Graders are honest: an assertion that cannot be evaluated is FAIL with
reason "could-not-evaluate" — never silently skipped.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# -- constants -----------------------------------------------------------------

SCENARIO_TIMEOUT_S = 300.0  # per-scenario hard timeout
PACKET_WAIT_S = 240.0  # budget for the packet to appear
POLL_INTERVAL_S = 2.0  # queue-poll interval (no long-blocking calls)
EXEC_TIMEOUT_S = 90.0  # local timeout per exec round-trip
POST_KILL_SETTLE_S = 3.0  # settle time after SIGKILL before durability check

DEFAULT_OUTPUT_ROOT = "/home/bkrabach/dev/better-attention/.amplifier/evaluation/amplifier-attention-manager"
DEFAULT_REPO_DIR = "/root/amplifier-attention-manager"  # in-DTU checkout (verify against real DTU)
DEFAULT_WORK_DIR = "/tmp/am-evals"  # in-DTU scratch root
DEFAULT_AM_CLI = "attention-manager"  # in-DTU CLI command prefix

S1_PROMPT = (
    "This task contains a NEEDS-HUMAN-DECISION point.\n\n"
    "NEEDS-HUMAN-DECISION: Should the config file format be JSON or YAML?\n"
    "Options:\n"
    "- A: use JSON (consequence: stricter syntax, no comments)\n"
    "- B: use YAML (consequence: human-friendlier, extra parser dependency)\n\n"
    "Call the request_decision tool with exactly these two options (ids A and B),\n"
    "include your recommendation with a one-line rationale and confidence, and wait\n"
    "for the answer. Then print exactly `DECISION RECEIVED: <answer>` and state which\n"
    "format you will use per the answer. Then you are done."
)

S2_PROMPT = "Run `echo eval-gate-ok` using the bash tool and report its output verbatim."

S3_PROMPT = (
    "This task contains a NEEDS-HUMAN-DECISION point.\n\n"
    "NEEDS-HUMAN-DECISION: Should the cache eviction policy be LRU or FIFO?\n"
    "Options:\n"
    "- A: use LRU (consequence: better hit rate, more bookkeeping)\n"
    "- B: use FIFO (consequence: simpler, worse hit rate under skew)\n\n"
    "Call the request_decision tool with exactly these two options (ids A and B),\n"
    "include your recommendation with a one-line rationale and confidence, and wait\n"
    "for the answer. Then print exactly `DECISION RECEIVED: <answer>` and state which\n"
    "policy you will use per the answer. Then you are done."
)

# -- scenario 4 (supervised fleet) constants ----------------------------------

S4_TIMEOUT_S = 600.0  # two parallel LLM workers + batch window + restart
S4_TICK_INTERVAL_S = 2.0  # supervise --interval
S4_BATCH_WINDOW_S = 30.0  # supervise --batch-window
S4_BATCH_MAX = 10  # supervise --batch-max
S4_NOTIFY_WAIT_S = 120.0  # budget for batch notification(s) after both packets
S4_EVENTS_WAIT_S = 90.0  # budget for event counts to catch up (ticks every 2s)
S4_WORKER_WAIT_S = 180.0  # budget for DECISION RECEIVED in worker logs
S4_RESTART_SETTLE_S = 6.0  # ~2 ticks + margin after supervisor restart

S4_W1_PROMPT = (
    "This task contains a NEEDS-HUMAN-DECISION point.\n\n"
    "NEEDS-HUMAN-DECISION [w1]: Should the config file format be JSON or YAML?\n"
    "Options:\n"
    "- A: use JSON (consequence: stricter syntax, no comments)\n"
    "- B: use YAML (consequence: human-friendlier, extra parser dependency)\n\n"
    "Call the request_decision tool with exactly these two options (ids A and B).\n"
    "Include the literal tag [w1] at the start of the question you pass to the tool.\n"
    "Include your recommendation with a one-line rationale and confidence, and wait\n"
    "for the answer. Then print exactly `DECISION RECEIVED: <answer>` and state which\n"
    "format you will use per the answer. Then you are done."
)

S4_W2_PROMPT = (
    "This task contains a NEEDS-HUMAN-DECISION point.\n\n"
    "NEEDS-HUMAN-DECISION [w2]: Should the cache eviction policy be LRU or FIFO?\n"
    "Options:\n"
    "- A: use LRU (consequence: better hit rate, more bookkeeping)\n"
    "- B: use FIFO (consequence: simpler, worse hit rate under skew)\n\n"
    "Call the request_decision tool with exactly these two options (ids A and B).\n"
    "Include the literal tag [w2] at the start of the question you pass to the tool.\n"
    "Include your recommendation with a one-line rationale and confidence, and wait\n"
    "for the answer. Then print exactly `DECISION RECEIVED: <answer>` and state which\n"
    "policy you will use per the answer. Then you are done."
)

# Worker names are FIXED by the flow contract (tmux sessions am-w1 / am-w2).
S4_WORKERS: dict[str, dict[str, Any]] = {
    "w1": {
        "session": "am-w1",
        "prompt": S4_W1_PROMPT,
        "tag": "[w1]",
        "keywords": ("json", "yaml", "config file format"),
        "answer": "A",
        "needle": "DECISION RECEIVED: A",
    },
    "w2": {
        "session": "am-w2",
        "prompt": S4_W2_PROMPT,
        "tag": "[w2]",
        "keywords": ("lru", "fifo", "eviction"),
        "answer": "B",
        "needle": "DECISION RECEIVED: B",
    },
}

S4_ASSERTION_NAMES = [
    "supervisor-started",
    "workers-dispatched",
    "tmux-sessions-exist",
    "two-decision-packets",
    "events-packet-created-x2",
    "all-created-packets-notified",
    "supervisor-killed",
    "restart-no-duplicate-events",
    "packet-worker-mapping",
    "answers-accepted",
    "w1-received-A",
    "w2-received-B",
    "events-packet-answered-x2-latency",
    "events-worker-finished-x2-judged-false",
    "events-no-duplicates",
    "ledger-full-story",
]

# -- scenario 5 (cold triage) constants ------------------------------------------

S5_TIMEOUT_S = 600.0  # three sequential passes, up to 3 real LLM sessions (+retries)
S5_SESSION_TIMEOUT_S = 180.0  # per-LLM-session budget passed via `triage --timeout`
S5_TRIAGE_EXEC_TIMEOUT_S = 500.0  # local budget for one synchronous `triage --once` exec

S5_SEED_RULE = "Prefer option ids whose consequence mentions lower operational risk when confidence is otherwise equal."

# The design's five rulebook sections, canonical order (rulebook.py SECTIONS —
# duplicated deliberately: the harness runs on the host and must not import the repo).
S5_RULEBOOK_SECTIONS = (
    "Attention priorities",
    "Auto-answer rules",
    "Escalation thresholds",
    "Edge cases",
    "When you cannot proceed",
)

S5_RULEBOOK_CONTENT = f"""\
# Attention Manager Rulebook

Read by every triage pass (packet + THIS FILE only — cold). Every human answer
should compound into a rule here: answered once, rule added, same class never
asked again. Rules are single sentences under a section, as `- ` bullets.

## Attention priorities

_What reaches the human first, and in what order. Highest-signal rules only._

## Auto-answer rules

_What the manager may decide itself, with explicit bounds. Phase 1: these are
recommendations only — nothing is auto-answered._

- {S5_SEED_RULE}

## Escalation thresholds

_When a routine situation stops being routine and must surface to the human._

## Edge cases

_Known exceptions to the rules above. If this section grows fast, a rule above
is badly written._

## When you cannot proceed

_What to do when no rule applies: bounce malformed packets, surface genuine
decisions with a why. Never invent an answer._
"""

# Seeds P1 (cold-decidable, NO producer recommendation) + P2 (cold-undecidable
# by construction) via the ROOT queue lib; prints the two ids in order.
S5_SEED_PACKETS_SCRIPT = """\
from attention_manager.packet import Option, Packet, Source
from attention_manager.queue import PacketQueue

q = PacketQueue()
p1 = Packet(
    question="Choose rollout strategy for the config change",
    options=[
        Option(id="A", label="big-bang rollout", consequence="faster but riskier"),
        Option(id="B", label="staged rollout", consequence="slower, lower operational risk"),
    ],
    source=Source(kind="decision"),
    context=(
        "The change updates the config parser. Downstream consumers auto-reload config. "
        "A big-bang rollout completes in one deploy, but a regression would hit all "
        "consumers at once. A staged rollout takes three deploys over two days with "
        "canary checks at each stage. There is no deadline pressure this week, and "
        "confidence in the change itself is equal between the two options."
    ),
)
p2 = Packet(
    question="Proceed with the approach we discussed earlier?",
    options=[Option(id="yes", label="Yes"), Option(id="no", label="No")],
    source=Source(kind="decision"),
    context="",
)
q.write(p1)
q.write(p2)
print(p1.id)
print(p2.id)
"""

S5_ASSERTION_NAMES = [
    "rulebook-seeded",
    "packets-seeded",
    "triage-pass-1-ok",
    "p1-recommended-pending",
    "p2-bounced",
    "events-triage",
    "ledger-triage",
    "answer-opposite-accepted",
    "triage-pass-2-ok",
    "one-rule-delta-record",
    "third-pass-idempotent",
    "rulebook-apply-branch",
]

# -- scenario 6 (judged finish lines) constants ------------------------------------

S6_TIMEOUT_S = 300.0  # FAKE workers, no LLM — deterministic judge mechanics
S6_FINISH_WAIT_S = 120.0  # budget for all 3 worker:finished events
S6_NOTIFY_WAIT_S = 60.0  # budget for finish-line notification items
S6_BATCH_WINDOW_S = 5.0  # supervise --batch-window (speed; nothing batching-related is asserted beyond delivery)

S6_MARKER = "GOOD-MARKER"

# Judge for `judge verify` — reads $ARTIFACT per the judge contract, prints a reason both ways.
S6_VERIFY_JUDGE_CMD = (
    f'if grep -q {S6_MARKER} "$ARTIFACT"; then echo "PASS: marker"; else echo "FAIL: no marker"; exit 1; fi'
)

# Judge for dispatched workers — RELATIVE artifact.txt (proves the judge-cwd
# contract: the supervisor runs judges with cwd = the worker's dir), prints a
# reason both ways per the contract, and references $WORKER_EXIT (env plumbing
# visible in judge.log / judge_output).
S6_WORKER_JUDGE_CMD = (
    f"if grep -q {S6_MARKER} artifact.txt 2>/dev/null; "
    f'then echo "PASS: marker found (worker exit: $WORKER_EXIT)"; '
    f'else echo "FAIL: marker missing from artifact"; exit 1; fi'
)

# Fixed worker names per the flow contract: g/b/u -> am-g / am-b / am-u.
S6_SESSIONS = ("am-g", "am-b", "am-u")

S6_ASSERTION_NAMES = [
    "judge-verify-pass",
    "judge-verify-rejects-decoration",
    "supervisor-started",
    "workers-dispatched",
    "three-worker-finished",
    "loop-closed-good",
    "judge-log-good",
    "loop-failed-bad",
    "finished-judged-fields",
    "unjudged-worker",
    "notify-finish-line-items",
    "ledger-counts",
    "ledger-summary-renders",
]

# -- scenario 7 (attractor gate) constants -------------------------------------------

S7_TIMEOUT_S = 300.0  # deterministic, NO LLM: hexagon + parallelogram tool nodes only
S7_NAME = "wu-eval"  # workunit name -> source.work_unit on the gate packet
S7_GATE_WAIT_S = 60.0  # budget for the attractor-gate packet to appear
S7_COMPLETE_WAIT_S = 60.0  # budget for workunit completion after the answer
S7_EVENT_WAIT_S = 15.0  # budget for gate:packet_created (emitted alongside the write)
S7_QUESTION = "Approve the work unit?"  # gate.dot hexagon prompt

# Markers proving the [attractor] extra is missing (attractor_gate.INSTALL_HINT
# names the extra; a raw traceback names the module). Honest env failure —
# graded could-not-evaluate with the text captured, never softened or skipped.
S7_MISSING_EXTRA_MARKERS = ("amplifier-attention-manager[attractor]", "amplifier_module_loop_pipeline")

S7_ASSERTION_NAMES = [
    "workunit-launched",
    "gate-packet-shape",
    "events-gate-created",
    "answer-accepted",
    "workunit-completed",
    "route-A-taken",
    "events-answered-finished",
    "ledger-workunit-finished",
]

# -- scenario 8 (graduated trust auto-answer) constants -------------------------------

S8_TIMEOUT_S = 600.0  # one real-LLM triage pass over 2 packets (+retries)
S8_PROMOTED_HEADING = "## Auto-answer rules <!-- phase:2 streak:5 -->"
S8_DEMOTED_HEADING = "## Auto-answer rules <!-- phase:1 streak:0 -->"
S8_RULE_IMPLIED_OPTION = "B"  # staged rollout — the seed rule prefers lower operational risk

# The S5 rulebook with the Auto-answer rules section PRE-PROMOTED to Phase 2
# (streak 5) via the heading annotation (rulebook.py _HEADING_RE format). The
# streak-walk to promotion is proven deterministically by local_trust_smoke.sh;
# this scenario spends its LLM budget on the auto-answer bounds themselves.
S8_RULEBOOK_CONTENT = S5_RULEBOOK_CONTENT.replace("## Auto-answer rules", S8_PROMOTED_HEADING, 1)

# P1 = S5-style rule-covered rollout decision (auto-answer expected).
# P2 = decidable-cold control NOT covered by any rulebook rule (its decision
# basis is stated IN the packet), so it must stay Phase-1 recommend-only.
S8_SEED_PACKETS_SCRIPT = """\
from attention_manager.packet import Option, Packet, Source
from attention_manager.queue import PacketQueue

q = PacketQueue()
p1 = Packet(
    question="Choose rollout strategy for the config change",
    options=[
        Option(id="A", label="big-bang rollout", consequence="faster but riskier"),
        Option(id="B", label="staged rollout", consequence="slower, lower operational risk"),
    ],
    source=Source(kind="decision"),
    context=(
        "The change updates the config parser. Downstream consumers auto-reload config. "
        "A big-bang rollout completes in one deploy, but a regression would hit all "
        "consumers at once. A staged rollout takes three deploys over two days with "
        "canary checks at each stage. There is no deadline pressure this week, and "
        "confidence in the change itself is equal between the two options."
    ),
)
p2 = Packet(
    question="Choose a name for the internal test fixture",
    options=[
        Option(id="fixture-a", label="fixture-a", consequence="shorter, matches existing fixture names"),
        Option(id="fixture-b", label="fixture-b", consequence="slightly longer, equally descriptive"),
    ],
    source=Source(kind="decision"),
    context=(
        "A new internal test fixture needs a name. Existing fixtures in this test suite "
        "follow the shortest-distinctive-name convention. Both candidates are otherwise "
        "equivalent; nothing else depends on the choice."
    ),
)
q.write(p1)
q.write(p2)
print(p1.id)
print(p2.id)
"""

S8_ASSERTION_NAMES = [
    "rulebook-seeded-prepromoted",
    "packets-seeded",
    "triage-pass-ok",
    "p1-auto-answered",
    "p1-auto-record",
    "events-ledger-auto",
    "p2-not-auto-answered",
    "auto-reject-accepted",
    "auto-record-reviewed",
    "section-demoted",
    "event-trust-demoted",
]

# -- scenario 9 (recipe-gate bridge) constants -----------------------------------------

S9_TIMEOUT_S = 600.0  # deterministic recipe, but each amplifier invoke carries bundle-prep latency
S9_INVOKE_TIMEOUT_S = 240.0  # local budget per in-DTU amplifier/poll exec
S9_COMPLETE_WAIT_S = 120.0  # budget for the auto-resumed recipe to complete (list polling)
S9_RECIPE_REL = "evals/recipes/gate-recipe.yaml"
S9_STAGE_NAME = "before-gate"  # the gate stage in gate-recipe.yaml
S9_FINAL_STEP = "step-two"  # the after-gate step: completion == this appears in completed_steps
# Completion surface (verified against the real recipes tool locally):
# operation=list -> {'sessions': [{'session_id', 'recipe_name', 'started',
# 'current_step_index', 'completed_steps': [...]}], 'count': N} — NO status
# field; for gate-recipe.yaml, completion == "step-two" in completed_steps.

# Markers proving the recipes tool is unavailable in the DTU's default bundle
# (honest env failure — graded could-not-evaluate with output captured, same
# pattern as S7's missing-extra handling).
S9_TOOL_MISSING_MARKERS = ("Tool 'recipes' not found", "tool-recipes", "not found in prepared bundle")

S9_ASSERTION_NAMES = [
    "recipe-executed-paused",
    "gate-packetized",
    "events-packetized",
    "answer-approve-accepted",
    "forward-approve",
    "resume-launched",
    "recipe-completes",
    "dedupe-no-second-packet-or-resume",
    "ledger-recipe-gates",
]

CNE = "could-not-evaluate"

# CSI + OSC ANSI escape sequences (tmux pipe-pane logs carry terminal control codes).
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# -- data ------------------------------------------------------------------------


@dataclass
class Config:
    dtu_exec: str | None
    output_dir: Path
    run_id: str
    am_cli: str = DEFAULT_AM_CLI
    repo_dir: str = DEFAULT_REPO_DIR
    work_dir: str = DEFAULT_WORK_DIR
    amplifier_args: str = ""


@dataclass
class ExecResult:
    rc: int
    stdout: str
    stderr: str
    command: str


@dataclass
class Assertion:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "pass": self.passed, "detail": self.detail}


@dataclass
class ScenarioResult:
    scenario: str
    assertions: list[Assertion] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def passed(self) -> bool:
        return bool(self.assertions) and all(a.passed for a in self.assertions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "pass": self.passed,
            "assertions": [a.to_dict() for a in self.assertions],
            "duration_s": round(self.duration_s, 1),
        }


@dataclass
class ScenarioSpec:
    number: int
    slug: str
    title: str
    kind: str  # packet source.kind expected
    prompt: str
    bundle_rel: str  # bundle path relative to repo_dir
    answer_option: str
    timeout_s: float = SCENARIO_TIMEOUT_S  # per-scenario hard deadline


SPECS = {
    1: ScenarioSpec(
        number=1,
        slug="s1-decision-roundtrip",
        title="Scenario 1 — decision roundtrip",
        kind="decision",
        prompt=S1_PROMPT,
        bundle_rel="bundles/test-worker.md",
        answer_option="B",
    ),
    2: ScenarioSpec(
        number=2,
        slug="s2-permission-gate",
        title="Scenario 2 — permission gate through real hook wiring",
        kind="permission",
        prompt=S2_PROMPT,
        bundle_rel="evals/bundles/test-worker-gated.md",
        answer_option="allow",
    ),
    3: ScenarioSpec(
        number=3,
        slug="s3-durability-restart",
        title="Scenario 3 — durability / worker death",
        kind="decision",
        prompt=S3_PROMPT,
        bundle_rel="bundles/test-worker.md",
        answer_option="B",
    ),
    4: ScenarioSpec(
        number=4,
        slug="s4-supervised-fleet",
        title="Scenario 4 — supervised fleet roundtrip (step 2)",
        kind="decision",
        prompt="",  # two per-worker prompts; see S4_WORKERS
        bundle_rel="bundles/test-worker.md",
        answer_option="",  # per-worker answers; see S4_WORKERS
        timeout_s=S4_TIMEOUT_S,
    ),
    5: ScenarioSpec(
        number=5,
        slug="s5-cold-triage",
        title="Scenario 5 — cold triage pass (step 3)",
        kind="decision",
        prompt="",  # packets are seeded directly; see S5_SEED_PACKETS_SCRIPT
        bundle_rel="bundles/triage.md",
        answer_option="",  # answer is the OPPOSITE of triage's recommendation
        timeout_s=S5_TIMEOUT_S,
    ),
    6: ScenarioSpec(
        number=6,
        slug="s6-judged-finish-lines",
        title="Scenario 6 — judged finish lines (step 4, fake workers)",
        kind="decision",  # unused: no packets in this scenario
        prompt="",  # fake --worker-cmd workers; see run_scenario_6
        bundle_rel="",  # no bundle: judge mechanics are deterministic (S4 proves LLM supervision)
        answer_option="",
        timeout_s=S6_TIMEOUT_S,
    ),
    7: ScenarioSpec(
        number=7,
        slug="s7-attractor-gate",
        title="Scenario 7 — attractor gate (step 5, deterministic pipeline)",
        kind="attractor-gate",
        prompt="",  # no LLM: the pipeline is hexagon + parallelogram tool nodes only
        bundle_rel="evals/pipelines/gate.dot",  # the pipeline, not a bundle
        answer_option="A",
        timeout_s=S7_TIMEOUT_S,
    ),
    8: ScenarioSpec(
        number=8,
        slug="s8-graduated-trust",
        title="Scenario 8 — graduated trust auto-answer (step 6, real LLM)",
        kind="decision",
        prompt="",  # packets are seeded directly; see S8_SEED_PACKETS_SCRIPT
        bundle_rel="bundles/triage.md",
        answer_option="",  # P1 is auto-answered; `auto reject` uses the opposite option
        timeout_s=S8_TIMEOUT_S,
    ),
    9: ScenarioSpec(
        number=9,
        slug="s9-recipe-gate-bridge",
        title="Scenario 9 — recipe-gate bridge (step 6, deterministic recipe)",
        kind="recipe-gate",
        prompt="",  # no LLM: two bash echo steps with one approval gate
        bundle_rel=S9_RECIPE_REL,  # the recipe, not a bundle
        answer_option="approve",
        timeout_s=S9_TIMEOUT_S,
    ),
}


# -- in-DTU command builders (shared by executor and --dry-run planner) -----------


def _env_prefix(queue_dir: str, home: str | None = None) -> str:
    parts = [f"export ATTENTION_QUEUE_DIR={shlex.quote(queue_dir)};"]
    if home:
        parts.append(f"export ATTENTION_HOME={shlex.quote(home)};")
    return " ".join(parts)


def cmd_queue_list(cfg: Config, queue_dir: str) -> str:
    return f"{_env_prefix(queue_dir)} {cfg.am_cli} --json queue list"


def cmd_queue_show(cfg: Config, queue_dir: str, packet_id: str) -> str:
    return f"{_env_prefix(queue_dir)} {cfg.am_cli} --json queue show {shlex.quote(packet_id)}"


def cmd_answer(cfg: Config, queue_dir: str, packet_id: str, option: str, rationale: str) -> str:
    return (
        f"{_env_prefix(queue_dir)} {cfg.am_cli} answer "
        f"{shlex.quote(packet_id)} {shlex.quote(option)} --rationale {shlex.quote(rationale)}"
    )


def cmd_launch_worker(cfg: Config, queue_dir: str, dtu_sdir: str, bundle_path: str, prompt: str) -> str:
    """Background-launch a worker in its own process group; record its PGID.

    setsid puts the worker in a new session (PGID == PID of the setsid'd bash)
    so scenario 3 can SIGKILL the whole group. The PGID is written to
    worker.pgid FROM INSIDE the new session (`echo $$`) — `$!` of the launch
    line is the pid of the background *subshell* wrapping `mkdir && setsid`,
    which is NOT the worker's process group (proven in the S4 incident: the
    group kill missed the real process and it survived). stdout+stderr ->
    worker.log; exit -> worker.exit.
    """
    log = f"{dtu_sdir}/worker.log"
    exit_file = f"{dtu_sdir}/worker.exit"
    pgid_file = f"{dtu_sdir}/worker.pgid"
    # `amplifier run` rejects bare filesystem paths for -B — only registered
    # names or URIs (file://, git+https://) are accepted (verified in DTU).
    bundle_uri = bundle_path if "://" in bundle_path else f"file://{bundle_path}"
    inner = (
        f"echo $$ > {shlex.quote(pgid_file)}; "
        f"{_env_prefix(queue_dir)} "
        f"amplifier run -B {shlex.quote(bundle_uri)}"
        + (f" {cfg.amplifier_args}" if cfg.amplifier_args else "")
        + f" {shlex.quote(prompt)} > {shlex.quote(log)} 2>&1; echo $? > {shlex.quote(exit_file)}"
    )
    return (
        f"mkdir -p {shlex.quote(queue_dir)} {shlex.quote(dtu_sdir)} && "
        f"setsid bash -c {shlex.quote(inner)} < /dev/null > /dev/null 2>&1 & echo $!"
    )


def cmd_worker_alive(pid: str) -> str:
    return f"kill -0 {pid} 2>/dev/null && echo alive || echo dead"


def cmd_kill_group(pid: str) -> str:
    return f"kill -9 -- -{pid} 2>/dev/null; kill -9 {pid} 2>/dev/null; true"


def cmd_read_pgid(pgid_file: str) -> str:
    return f"cat {shlex.quote(pgid_file)} 2>/dev/null || true"


def cmd_cat(path: str) -> str:
    return f"cat {shlex.quote(path)}"


def cmd_file_exists(path: str) -> str:
    return f"test -f {shlex.quote(path)} && echo yes || echo no"


def cmd_file_absent(path: str) -> str:
    return f"test ! -f {shlex.quote(path)} && echo absent || echo present"


# -- scenario-4 command builders (supervisor fleet) --------------------------------


def cmd_supervise_launch(
    cfg: Config,
    queue_dir: str,
    home: str,
    dtu_sdir: str,
    notify_path: str,
    sup_log: str,
    pgid_file: str,
    batch_window_s: float = S4_BATCH_WINDOW_S,
) -> str:
    """Background-launch the supervisor in its own process group; record its PGID.

    stdout+stderr APPEND to sup_log so the pre- and post-restart runs share one
    supervisor.log. ATTENTION_QUEUE_DIR + ATTENTION_HOME are exported (both
    supervise and dispatch need them).

    KILL CORRECTNESS (S4 incident): `$!` of this launch line is the pid of the
    background *subshell* wrapping `mkdir && setsid ...` — NOT the supervisor's
    process group. Killing `-$!` missed the real supervisor, it survived, and
    the restarted supervisor ran CONCURRENTLY with it (duplicate
    packet:answered events). The truthful PGID is captured from inside the new
    session: `echo $$` runs in the setsid'd bash (session/group leader), and
    `exec` replaces that bash with the supervisor, so pgid_file == the
    supervisor's own PID == its PGID, regardless of shell fork optimizations.
    """
    inner = (
        f"echo $$ > {shlex.quote(pgid_file)}; "
        f"{_env_prefix(queue_dir, home)} "
        f"exec {cfg.am_cli} supervise --interval {S4_TICK_INTERVAL_S:g} "
        f"--notify {shlex.quote('file:' + notify_path)} "
        f"--batch-window {batch_window_s:g} --batch-max {S4_BATCH_MAX} "
        f">> {shlex.quote(sup_log)} 2>&1"
    )
    return (
        f"mkdir -p {shlex.quote(queue_dir)} {shlex.quote(home)} {shlex.quote(dtu_sdir)} && "
        f"setsid bash -c {shlex.quote(inner)} < /dev/null > /dev/null 2>&1 & echo $!"
    )


def cmd_dispatch(cfg: Config, queue_dir: str, home: str, name: str, task: str, bundle_uri: str) -> str:
    """Dispatch a worker into an am-<name> tmux session (env exported so the
    dispatch CLI forwards ATTENTION_QUEUE_DIR/ATTENTION_HOME into the pane)."""
    return (
        f"{_env_prefix(queue_dir, home)} {cfg.am_cli} dispatch {shlex.quote(name)} "
        f"--task {shlex.quote(task)} --bundle {shlex.quote(bundle_uri)}"
    )


def cmd_tmux_has(session: str) -> str:
    return f"tmux has-session -t ={session} 2>/dev/null && echo yes || echo no"


def cmd_tmux_ls() -> str:
    return "tmux ls 2>/dev/null || true"


def cmd_tmux_kill(session: str) -> str:
    return f"tmux kill-session -t ={session} 2>/dev/null; true"


def cmd_ledger_cat(home: str) -> str:
    return f"cat {shlex.quote(home)}/ledger/*.jsonl 2>/dev/null || true"


# -- scenario-5 command builders (cold triage) --------------------------------------


def cmd_seed_rulebook(home: str, content: str = S5_RULEBOOK_CONTENT) -> str:
    """Write a seeded rulebook and echo it back for grading."""
    rulebook_path = f"{home}/rulebook.md"
    return (
        f"mkdir -p {shlex.quote(home)} && "
        f"printf %s {shlex.quote(content)} > {shlex.quote(rulebook_path)} && "
        f"cat {shlex.quote(rulebook_path)}"
    )


def cmd_seed_packets(cfg: Config, queue_dir: str, script: str = S5_SEED_PACKETS_SCRIPT) -> str:
    """Seed packets via the ROOT queue lib (repo src on PYTHONPATH); prints the ids."""
    src = shlex.quote(f"{cfg.repo_dir}/src")
    return (
        f"{_env_prefix(queue_dir)} export PYTHONPATH={src}${{PYTHONPATH:+:$PYTHONPATH}}; "
        f"mkdir -p {shlex.quote(queue_dir)} && python3 -c {shlex.quote(script)}"
    )


def cmd_triage_once(cfg: Config, queue_dir: str, home: str, bundle_uri: str, out_log: str) -> str:
    """One synchronous cold-triage pass; stdout+stderr tee'd to an in-DTU log."""
    return (
        f"{_env_prefix(queue_dir, home)} {cfg.am_cli} triage --once "
        f"--bundle {shlex.quote(bundle_uri)} --timeout {S5_SESSION_TIMEOUT_S:g} "
        f"2>&1 | tee {shlex.quote(out_log)}; exit ${{PIPESTATUS[0]}}"
    )


def cmd_rulebook_apply(cfg: Config, queue_dir: str, home: str, proposal_id: str) -> str:
    return f"{_env_prefix(queue_dir, home)} {cfg.am_cli} rulebook apply {shlex.quote(proposal_id)}"


# -- scenario-8 command builders (graduated trust) ------------------------------------


def cmd_auto_reject(cfg: Config, queue_dir: str, home: str, packet_id: str, correct_option: str) -> str:
    return (
        f"{_env_prefix(queue_dir, home)} {cfg.am_cli} auto reject {shlex.quote(packet_id)} "
        f"--correct-option {shlex.quote(correct_option)} --reason eval"
    )


# -- scenario-9 command builders (recipe-gate bridge) ---------------------------------


def cmd_recipes_invoke(queue_dir: str, home: str, workdir: str, op_args: str, out_log: str) -> str:
    """Run `amplifier tool invoke recipes <op_args> -o json` from the scenario workdir.

    cwd matters: recipe sessions are project-scoped by working directory, and
    RecipeGatePoller invokes from ITS cwd — execute, every poll, and resume
    must all share this workdir. Output tee'd to an in-DTU log for artifacts.
    """
    return (
        f"{_env_prefix(queue_dir, home)} mkdir -p {shlex.quote(workdir)} && cd {shlex.quote(workdir)} && "
        f"amplifier tool invoke recipes {op_args} -o json 2>&1 | tee {shlex.quote(out_log)}; "
        f"exit ${{PIPESTATUS[0]}}"
    )


def cmd_recipes_poll(cfg: Config, queue_dir: str, home: str, workdir: str, out_log: str) -> str:
    """One recipe-gate poller pass (packetize new gates + forward answered ones)."""
    return (
        f"{_env_prefix(queue_dir, home)} mkdir -p {shlex.quote(workdir)} && cd {shlex.quote(workdir)} && "
        f"{cfg.am_cli} recipes poll --once 2>&1 | tee {shlex.quote(out_log)}; exit ${{PIPESTATUS[0]}}"
    )


# -- scenario-6 command builders (judged finish lines) -------------------------------


def cmd_make_verify_artifacts(dtu_sdir: str) -> str:
    """Write the good (marker) / broken (no marker) artifacts for `judge verify`."""
    good = f"{dtu_sdir}/good-artifact.txt"
    broken = f"{dtu_sdir}/broken-artifact.txt"
    return (
        f"mkdir -p {shlex.quote(dtu_sdir)} && "
        f"echo {S6_MARKER} > {shlex.quote(good)} && "
        f"echo 'no marker here' > {shlex.quote(broken)} && echo seeded"
    )


def cmd_judge_verify(cfg: Config, queue_dir: str, home: str, judge_cmd: str, good: str, broken: str) -> str:
    return (
        f"{_env_prefix(queue_dir, home)} {cfg.am_cli} judge verify "
        f"--cmd {shlex.quote(judge_cmd)} --good {shlex.quote(good)} --broken {shlex.quote(broken)}"
    )


def cmd_dispatch_fake(
    cfg: Config, queue_dir: str, home: str, name: str, task: str, worker_cmd: str, judge_cmd: str | None
) -> str:
    """Dispatch a fake --worker-cmd worker, optionally with a --judge command."""
    judge_part = f" --judge {shlex.quote(judge_cmd)}" if judge_cmd else ""
    return (
        f"{_env_prefix(queue_dir, home)} {cfg.am_cli} dispatch {shlex.quote(name)} "
        f"--task {shlex.quote(task)} --worker-cmd {shlex.quote(worker_cmd)}{judge_part}"
    )


def cmd_ledger_summary(cfg: Config, queue_dir: str, home: str) -> str:
    return f"{_env_prefix(queue_dir, home)} {cfg.am_cli} --json ledger --summary"


# -- scenario-7 command builders (attractor gate) -------------------------------------


def cmd_workunit_launch(
    cfg: Config, queue_dir: str, home: str, dtu_sdir: str, workdir: str, pipeline_path: str, name: str
) -> str:
    """Background-launch `workunit run` in its own process group, cwd = workdir.

    The pipeline's tool nodes (`tool_command="echo A > A.txt"`) execute relative
    to the PROCESS cwd (verified: workunit.py never chdirs; the local smoke does
    `cd "$workdir" && exec ... workunit run`), hence the explicit `cd` here.
    Same pgid-file pattern as the supervisor/worker launches: `echo $$` inside
    the setsid'd bash is the group leader; wu.exit captures the exit code.
    """
    pgid_file = f"{dtu_sdir}/wu.pgid"
    log = f"{dtu_sdir}/wu.log"
    exit_file = f"{dtu_sdir}/wu.exit"
    inner = (
        f"echo $$ > {shlex.quote(pgid_file)}; "
        f"{_env_prefix(queue_dir, home)} "
        f"cd {shlex.quote(workdir)} && "
        f"{cfg.am_cli} workunit run {shlex.quote(pipeline_path)} --name {shlex.quote(name)} "
        f"> {shlex.quote(log)} 2>&1; echo $? > {shlex.quote(exit_file)}"
    )
    return (
        f"mkdir -p {shlex.quote(queue_dir)} {shlex.quote(home)} {shlex.quote(workdir)} && "
        f"setsid bash -c {shlex.quote(inner)} < /dev/null > /dev/null 2>&1 & echo $!"
    )


# -- exec layer --------------------------------------------------------------------


class DtuExec:
    """Executes commands inside the DTU via the caller-supplied exec prefix."""

    def __init__(self, prefix: str, log_file: Path):
        self.prefix = shlex.split(prefix)
        self.log_file = log_file

    def run(self, cmd: str, label: str = "", timeout: float = EXEC_TIMEOUT_S) -> ExecResult:
        argv = [*self.prefix, "--", "bash", "-c", cmd]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
            result = self._unwrap(proc, cmd)
        except subprocess.TimeoutExpired:
            result = ExecResult(rc=124, stdout="", stderr=f"local exec timeout after {timeout}s", command=cmd)
        self._log(label, result)
        return result

    @staticmethod
    def _unwrap(proc: subprocess.CompletedProcess[str], cmd: str) -> ExecResult:
        """Unwrap the amplifier-digital-twin JSON-mode envelope when present."""
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
        try:
            envelope = json.loads(stdout)
            if isinstance(envelope, dict) and "exit_code" in envelope and "stdout" in envelope:
                return ExecResult(
                    rc=int(envelope["exit_code"]),
                    stdout=str(envelope["stdout"]),
                    stderr=str(envelope.get("stderr", "")),
                    command=cmd,
                )
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return ExecResult(rc=rc, stdout=stdout, stderr=stderr, command=cmd)

    def _log(self, label: str, result: ExecResult) -> None:
        stamp = datetime.now(UTC).isoformat()
        entry = (
            f"[{stamp}] {label}\n"
            f"  $ {result.command}\n"
            f"  rc={result.rc}\n"
            f"  stdout: {result.stdout.strip()[:2000]}\n"
            f"  stderr: {result.stderr.strip()[:2000]}\n"
        )
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(entry)


# -- scenario runtime --------------------------------------------------------------


def _poll_pgid_file(ex: DtuExec, pgid_file: str, label: str, budget_s: float = 15.0) -> str | None:
    """Read the pgid file written from inside the setsid'd session (poll: the
    background launch races the reader). Returns the pgid string or None."""
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        result = ex.run(cmd_read_pgid(pgid_file), label=label)
        pgid = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        if pgid.isdigit():
            return pgid
        time.sleep(0.5)
    return None


class Scenario:
    """Shared per-scenario runtime: paths, deadline, assertions, snapshots."""

    def __init__(self, cfg: Config, spec: ScenarioSpec):
        self.cfg = cfg
        self.spec = spec
        self.out_dir = cfg.output_dir / spec.slug
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.dtu_sdir = f"{cfg.work_dir}/{cfg.run_id}/{spec.slug}"
        self.queue_dir = f"{self.dtu_sdir}/queue"
        self.deadline = time.monotonic() + spec.timeout_s
        self.assertions: list[Assertion] = []
        self.snapshots = self.out_dir / "queue_snapshots.jsonl"
        assert cfg.dtu_exec is not None
        self.ex = DtuExec(cfg.dtu_exec, self.out_dir / "harness.log")
        self.worker_pid: str | None = None

    # -- bookkeeping ---------------------------------------------------------

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def check(self, name: str, condition: bool, detail: str) -> bool:
        self.assertions.append(Assertion(name, bool(condition), detail))
        status = "PASS" if condition else "FAIL"
        print(f"    [{status}] {name}: {detail[:160]}")
        return bool(condition)

    def cne(self, name: str, reason: str) -> None:
        self.check(name, False, f"{CNE}: {reason}")

    def snapshot(self, label: str, result: ExecResult) -> None:
        line = {
            "ts": datetime.now(UTC).isoformat(),
            "label": label,
            "rc": result.rc,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        with self.snapshots.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    # -- steps -----------------------------------------------------------------

    def launch_worker(self, prompt: str, bundle_rel: str) -> str | None:
        bundle = f"{self.cfg.repo_dir}/{bundle_rel}"
        result = self.ex.run(
            cmd_launch_worker(self.cfg, self.queue_dir, self.dtu_sdir, bundle, prompt),
            label="launch-worker",
        )
        launcher_pid = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        if result.rc != 0 or not launcher_pid.isdigit():
            self.cne(
                "worker-launched",
                f"launch rc={result.rc}, pid output {launcher_pid!r}, stderr={result.stderr.strip()!r}",
            )
            return None
        # The worker's REAL process group id is written by the setsid'd bash
        # itself (worker.pgid) — `$!` is only the wrapping subshell.
        pgid = _poll_pgid_file(self.ex, f"{self.dtu_sdir}/worker.pgid", "worker-pgid-read")
        if pgid is None:
            self.cne("worker-launched", f"worker.pgid never appeared (launcher pid {launcher_pid})")
            return None
        self.worker_pid = pgid
        return pgid

    def wait_for_packet(self, kind: str) -> dict[str, Any] | None:
        budget = min(PACKET_WAIT_S, max(0.0, self.remaining()))
        started = time.monotonic()
        while time.monotonic() - started < budget:
            result = self.ex.run(cmd_queue_list(self.cfg, self.queue_dir), label="queue-list-poll")
            self.snapshot("queue-list-poll", result)
            if result.rc == 0:
                try:
                    packets = json.loads(result.stdout)
                except json.JSONDecodeError:
                    packets = []
                for packet in packets:
                    if isinstance(packet, dict) and (packet.get("source") or {}).get("kind") == kind:
                        (self.out_dir / "packet-pending.json").write_text(
                            json.dumps(packet, indent=2), encoding="utf-8"
                        )
                        return packet
            time.sleep(POLL_INTERVAL_S)
        return None

    def answer(self, packet_id: str, option: str) -> ExecResult:
        result = self.ex.run(cmd_answer(self.cfg, self.queue_dir, packet_id, option, "eval"), label="answer")
        self.snapshot(
            "post-answer", self.ex.run(cmd_queue_list(self.cfg, self.queue_dir), label="queue-list-post-answer")
        )
        return result

    def wait_worker_done(self) -> str | None:
        """Poll for worker.exit; return its content (exit code string) or None."""
        exit_file = f"{self.dtu_sdir}/worker.exit"
        while self.remaining() > 0:
            result = self.ex.run(cmd_file_exists(exit_file), label="worker-exit-poll")
            if result.stdout.strip() == "yes":
                content = self.ex.run(cmd_cat(exit_file), label="worker-exit-read")
                (self.out_dir / "worker.exit").write_text(content.stdout, encoding="utf-8")
                return content.stdout.strip()
            time.sleep(POLL_INTERVAL_S)
        return None

    def fetch_worker_log(self) -> str:
        result = self.ex.run(cmd_cat(f"{self.dtu_sdir}/worker.log"), label="worker-log-fetch")
        (self.out_dir / "worker.log").write_text(result.stdout, encoding="utf-8")
        return result.stdout

    def read_answered_packet(self, packet_id: str) -> dict[str, Any] | None:
        result = self.ex.run(cmd_cat(f"{self.queue_dir}/answered/{packet_id}.json"), label="answered-read")
        if result.rc != 0:
            return None
        try:
            packet = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        (self.out_dir / "packet-answered.json").write_text(json.dumps(packet, indent=2), encoding="utf-8")
        return packet

    def cleanup(self) -> None:
        if self.worker_pid:
            self.ex.run(cmd_kill_group(self.worker_pid), label="cleanup-kill-worker")


# -- graders shared across scenarios ------------------------------------------------


def grade_answered(s: Scenario, packet_id: str, expected_answer: str) -> None:
    packet = s.read_answered_packet(packet_id)
    if packet is None:
        s.cne("packet-answered-authoritative", f"answered/{packet_id}.json missing or unparseable")
        return
    resolution = packet.get("resolution") or {}
    s.check(
        "packet-answered-authoritative",
        resolution.get("answer") == expected_answer
        and resolution.get("answered_by") == "human"
        and bool(resolution.get("answered_at")),
        f"resolution={resolution}",
    )


def grade_worker_completion(s: Scenario) -> str:
    exit_code = s.wait_worker_done()
    if exit_code is None:
        s.cne("worker-finished", "worker.exit never appeared before scenario deadline")
        s.cne("worker-exit-zero", "no exit code to grade")
    else:
        s.check("worker-finished", True, "worker process completed")
        s.check("worker-exit-zero", exit_code == "0", f"worker.exit={exit_code!r}")
    return s.fetch_worker_log()


# -- scenarios ------------------------------------------------------------------------


def run_scenario_1(cfg: Config) -> ScenarioResult:
    spec = SPECS[1]
    s = Scenario(cfg, spec)
    started = time.monotonic()
    try:
        pid = s.launch_worker(spec.prompt, spec.bundle_rel)
        if pid is None:
            return _finish(s, started)

        packet = s.wait_for_packet(kind=spec.kind)
        if packet is None:
            s.cne("packet-appeared", f"no kind={spec.kind} packet within {PACKET_WAIT_S:.0f}s")
            s.fetch_worker_log()
            for name in (
                "packet-schema-valid",
                "answer-accepted",
                "worker-finished",
                "worker-exit-zero",
                "worker-received-answer",
                "packet-answered-authoritative",
                "pending-removed",
            ):
                s.cne(name, "no packet appeared")
            return _finish(s, started)

        packet_id = str(packet.get("id", ""))
        s.check("packet-appeared", True, f"packet {packet_id} (kind={spec.kind})")

        show = s.ex.run(cmd_queue_show(cfg, s.queue_dir, packet_id), label="queue-show")
        schema_ok, schema_detail = _grade_decision_schema(show)
        s.check("packet-schema-valid", schema_ok, schema_detail)

        answer = s.answer(packet_id, spec.answer_option)
        s.check("answer-accepted", answer.rc == 0, f"answer rc={answer.rc} stderr={answer.stderr.strip()!r}")

        worker_log = grade_worker_completion(s)
        s.check(
            "worker-received-answer",
            f"DECISION RECEIVED: {spec.answer_option}" in worker_log,
            f"looked for 'DECISION RECEIVED: {spec.answer_option}' in worker.log ({len(worker_log)} chars)",
        )

        grade_answered(s, packet_id, spec.answer_option)
        gone = s.ex.run(cmd_file_absent(f"{s.queue_dir}/pending/{packet_id}.json"), label="pending-absent")
        s.check("pending-removed", gone.stdout.strip() == "absent", f"pending/{packet_id}.json: {gone.stdout.strip()}")
        return _finish(s, started)
    finally:
        s.cleanup()


def run_scenario_2(cfg: Config) -> ScenarioResult:
    spec = SPECS[2]
    s = Scenario(cfg, spec)
    started = time.monotonic()
    try:
        pid = s.launch_worker(spec.prompt, spec.bundle_rel)
        if pid is None:
            return _finish(s, started)

        packet = s.wait_for_packet(kind=spec.kind)
        if packet is None:
            # THE failure mode this scenario exists to catch — captured loudly.
            worker_log = s.fetch_worker_log()
            s.cne(
                "permission-packet-appeared",
                f"no kind=permission packet within {PACKET_WAIT_S:.0f}s — the session likely fell back to "
                "console/default approval, auto-denied ('No approval provider available'), or an auto-approval "
                "rule swallowed the gate. This is exactly the wiring defect this scenario tests for. "
                f"worker.log tail: {worker_log[-500:]!r}",
            )
            for name in (
                "options-exactly-allow-deny",
                "answer-accepted",
                "worker-finished",
                "worker-exit-zero",
                "bash-output-present",
                "packet-answered-allow",
            ):
                s.cne(name, "no permission packet appeared")
            return _finish(s, started)

        packet_id = str(packet.get("id", ""))
        s.check("permission-packet-appeared", True, f"packet {packet_id} (kind=permission)")

        option_ids = [o.get("id") for o in packet.get("options", []) if isinstance(o, dict)]
        s.check(
            "options-exactly-allow-deny",
            len(option_ids) == 2 and set(option_ids) == {"allow", "deny"},
            f"options={option_ids}",
        )

        answer = s.answer(packet_id, spec.answer_option)
        s.check("answer-accepted", answer.rc == 0, f"answer rc={answer.rc} stderr={answer.stderr.strip()!r}")

        worker_log = grade_worker_completion(s)
        s.check(
            "bash-output-present",
            "eval-gate-ok" in worker_log,
            f"looked for 'eval-gate-ok' in worker.log ({len(worker_log)} chars)",
        )

        packet_answered = s.read_answered_packet(packet_id)
        if packet_answered is None:
            s.cne("packet-answered-allow", f"answered/{packet_id}.json missing or unparseable")
        else:
            resolution = packet_answered.get("resolution") or {}
            s.check(
                "packet-answered-allow",
                resolution.get("answer") == "allow" and resolution.get("answered_by") == "human",
                f"resolution={resolution}",
            )
        return _finish(s, started)
    finally:
        s.cleanup()


def run_scenario_3(cfg: Config) -> ScenarioResult:
    spec = SPECS[3]
    s = Scenario(cfg, spec)
    started = time.monotonic()
    try:
        pid = s.launch_worker(spec.prompt, spec.bundle_rel)
        if pid is None:
            return _finish(s, started)

        packet = s.wait_for_packet(kind=spec.kind)
        if packet is None:
            s.cne("packet-appeared", f"no kind={spec.kind} packet within {PACKET_WAIT_S:.0f}s")
            s.fetch_worker_log()
            for name in (
                "worker-killed",
                "packet-survives-kill",
                "answer-accepted",
                "packet-answered-intact",
                "reentry-data-present",
            ):
                s.cne(name, "no packet appeared")
            return _finish(s, started)

        packet_id = str(packet.get("id", ""))
        s.check("packet-appeared", True, f"packet {packet_id} pending (kind={spec.kind})")

        # SIGKILL the whole worker process group mid-block.
        s.ex.run(cmd_kill_group(pid), label="sigkill-worker")
        alive = s.ex.run(cmd_worker_alive(pid), label="post-kill-alive-check")
        s.check("worker-killed", alive.stdout.strip() == "dead", f"post-SIGKILL probe: {alive.stdout.strip()!r}")

        time.sleep(POST_KILL_SETTLE_S)
        listing = s.ex.run(cmd_queue_list(cfg, s.queue_dir), label="queue-list-post-kill")
        s.snapshot("post-kill", listing)
        still_listed = packet_id in listing.stdout
        pending_file = s.ex.run(cmd_file_exists(f"{s.queue_dir}/pending/{packet_id}.json"), label="pending-exists")
        s.check(
            "packet-survives-kill",
            still_listed and pending_file.stdout.strip() == "yes",
            f"listed={still_listed}, pending file={pending_file.stdout.strip()}",
        )

        answer = s.answer(packet_id, spec.answer_option)
        s.check("answer-accepted", answer.rc == 0, f"answer rc={answer.rc} stderr={answer.stderr.strip()!r}")

        answered = s.read_answered_packet(packet_id)
        if answered is None:
            s.cne("packet-answered-intact", f"answered/{packet_id}.json missing or unparseable")
            s.cne("reentry-data-present", "no answered packet to inspect")
        else:
            resolution = answered.get("resolution") or {}
            s.check(
                "packet-answered-intact",
                resolution.get("answer") == spec.answer_option
                and resolution.get("answered_by") == "human"
                and bool(resolution.get("answered_at")),
                f"resolution={resolution}",
            )
            links_resume = (answered.get("links") or {}).get("resume") or ""
            session_id = (answered.get("source") or {}).get("session_id") or ""
            s.check(
                "reentry-data-present",
                bool(str(links_resume).strip()) or bool(str(session_id).strip()),
                f"links.resume={links_resume!r}, source.session_id={session_id!r} "
                "(re-drive is possible iff either is present; resume itself is out of scope)",
            )
        return _finish(s, started)
    finally:
        s.cleanup()


# -- scenario 4: supervised fleet roundtrip (step 2) --------------------------------


def _parse_jsonl(text: str) -> tuple[list[dict[str, Any]], int]:
    """Parse JSONL leniently; return (records, malformed_line_count)."""
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(record, dict):
            records.append(record)
        else:
            malformed += 1
    return records, malformed


def _fetch_events(s: Scenario, home: str) -> tuple[list[dict[str, Any]], int]:
    result = s.ex.run(cmd_cat(f"{home}/events.jsonl"), label="events-fetch")
    if result.rc != 0:
        return [], 0
    return _parse_jsonl(result.stdout)


def _events_named(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("event") == name]


def _duplicate_packet_events(events: list[dict[str, Any]]) -> dict[str, int]:
    """(event, packet_id) pairs seen more than once for the per-packet events.

    A non-empty result is the direct signature of two supervisors writing the
    same home concurrently (single-writer invariant violated).
    """
    counts: dict[str, int] = {}
    for e in events:
        name = str(e.get("event"))
        if name in ("packet:created", "packet:answered"):
            key = f"{name}:{e.get('packet_id')}"
            counts[key] = counts.get(key, 0) + 1
    return {key: n for key, n in counts.items() if n > 1}


def _poll(s: Scenario, budget: float, fn: Any) -> Any:
    """Poll fn() every POLL_INTERVAL_S until non-None, budget, or scenario deadline."""
    deadline = time.monotonic() + min(budget, max(0.0, s.remaining()))
    while time.monotonic() < deadline:
        value = fn()
        if value is not None:
            return value
        time.sleep(POLL_INTERVAL_S)
    return None


def _map_packets_to_workers(packets: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
    """Map each packet to exactly one worker via tag (primary) / keywords (fallback).

    Returns {"w1": packet, "w2": packet} or None when ambiguous/incomplete.
    """
    mapping: dict[str, dict[str, Any]] = {}
    for packet in packets:
        question = str(packet.get("question", "")).lower()
        hits = [
            name for name, w in S4_WORKERS.items() if w["tag"] in question or any(k in question for k in w["keywords"])
        ]
        if len(hits) != 1 or hits[0] in mapping:
            return None
        mapping[hits[0]] = packet
    return mapping if set(mapping) == set(S4_WORKERS) else None


def _cne_rest(s: Scenario, names: list[str], reason: str) -> None:
    """FAIL every not-yet-recorded assertion in `names` as could-not-evaluate."""
    recorded = {a.name for a in s.assertions}
    for name in names:
        if name not in recorded:
            s.cne(name, reason)


def _collect_s4_artifacts(s: Scenario, home: str, notify_path: str, sup_log: str) -> None:
    """Best-effort artifact collection — runs even when the scenario bails early."""
    artifacts = [
        ("supervisor-log", cmd_cat(sup_log), "supervisor.log"),
        ("events-copy", cmd_cat(f"{home}/events.jsonl"), "events.jsonl"),
        ("ledger-copy", cmd_ledger_cat(home), "ledger.jsonl"),
        ("notify-copy", cmd_cat(notify_path), "notify.jsonl"),
        ("tmux-ls", cmd_tmux_ls(), "tmux-ls.txt"),
        ("w1-log", cmd_cat(f"{home}/workers/am-w1/worker.log"), "worker-am-w1.log"),
        ("w2-log", cmd_cat(f"{home}/workers/am-w2/worker.log"), "worker-am-w2.log"),
    ]
    for label, cmd, filename in artifacts:
        try:
            result = s.ex.run(cmd, label=f"artifact-{label}")
            if result.rc == 0:
                (s.out_dir / filename).write_text(result.stdout, encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — artifact collection must never mask the verdict
            print(f"    (artifact {label} not collected: {e})")


def run_scenario_4(cfg: Config) -> ScenarioResult:
    spec = SPECS[4]
    s = Scenario(cfg, spec)
    started = time.monotonic()
    home = f"{s.dtu_sdir}/home"
    notify_path = f"{s.dtu_sdir}/notify.jsonl"
    sup_log = f"{s.dtu_sdir}/supervisor.log"
    bundle_uri = f"file://{cfg.repo_dir}/{spec.bundle_rel}"
    sup_pgid_file = f"{s.dtu_sdir}/supervisor.pgid"
    sup_pids: list[str] = []
    try:
        # 0. Pre-clean fixed-name sessions (dispatch fails loud on an existing one).
        for w in S4_WORKERS.values():
            s.ex.run(cmd_tmux_kill(str(w["session"])), label=f"pre-kill-{w['session']}")

        # 1. Supervisor up (background, own process group). The kill target is
        #    the REAL pgid from supervisor.pgid (written inside the setsid'd
        #    session) — `$!` is only the wrapping subshell (S4 incident).
        launch = s.ex.run(
            cmd_supervise_launch(cfg, s.queue_dir, home, s.dtu_sdir, notify_path, sup_log, sup_pgid_file),
            label="supervise-launch",
        )
        if launch.rc != 0:
            s.cne("supervisor-started", f"launch rc={launch.rc}, stderr={launch.stderr.strip()!r}")
            _cne_rest(s, S4_ASSERTION_NAMES, "supervisor never started")
            return _finish(s, started)
        pid = _poll_pgid_file(s.ex, sup_pgid_file, "supervisor-pgid-read")
        if pid is None:
            s.cne("supervisor-started", f"supervisor.pgid never appeared (launch stdout {launch.stdout.strip()!r})")
            _cne_rest(s, S4_ASSERTION_NAMES, "supervisor never started")
            return _finish(s, started)
        sup_pids.append(pid)
        s.check("supervisor-started", True, f"supervisor pgid {pid} (from supervisor.pgid)")

        # 2. Dispatch the fleet.
        dispatch_details: list[str] = []
        dispatch_ok = True
        for name, w in S4_WORKERS.items():
            result = s.ex.run(
                cmd_dispatch(cfg, s.queue_dir, home, name, str(w["prompt"]), bundle_uri),
                label=f"dispatch-{name}",
            )
            dispatch_details.append(
                f"{name}: rc={result.rc}" + (f" stderr={result.stderr.strip()!r}" if result.rc else "")
            )
            dispatch_ok = dispatch_ok and result.rc == 0
        s.check("workers-dispatched", dispatch_ok, "; ".join(dispatch_details))
        if not dispatch_ok:
            _cne_rest(s, S4_ASSERTION_NAMES, "dispatch failed")
            return _finish(s, started)

        # 3. Both tmux sessions exist.
        session_states = {
            str(w["session"]): s.ex.run(
                cmd_tmux_has(str(w["session"])), label=f"tmux-has-{w['session']}"
            ).stdout.strip()
            for w in S4_WORKERS.values()
        }
        s.check(
            "tmux-sessions-exist",
            all(state == "yes" for state in session_states.values()),
            f"{session_states}",
        )

        # 4. Two decision packets (real LLM workers — generous budget).
        def poll_two_packets() -> list[dict[str, Any]] | None:
            result = s.ex.run(cmd_queue_list(cfg, s.queue_dir), label="queue-list-poll")
            s.snapshot("queue-list-poll", result)
            if result.rc != 0:
                return None
            try:
                listed = json.loads(result.stdout)
            except json.JSONDecodeError:
                return None
            decisions = [p for p in listed if isinstance(p, dict) and (p.get("source") or {}).get("kind") == spec.kind]
            return decisions if len(decisions) >= 2 else None

        packets = _poll(s, PACKET_WAIT_S, poll_two_packets)
        if packets is None:
            s.cne("two-decision-packets", f"fewer than 2 kind={spec.kind} packets within {PACKET_WAIT_S:.0f}s")
            _cne_rest(s, S4_ASSERTION_NAMES, "packets never appeared")
            return _finish(s, started)
        packet_ids = sorted(str(p.get("id", "")) for p in packets)
        s.check("two-decision-packets", len(packets) == 2, f"count={len(packets)}, ids={packet_ids}")
        (s.out_dir / "packet-pending.json").write_text(json.dumps(packets, indent=2), encoding="utf-8")

        # 5. Exactly 2 packet:created events.
        def poll_created() -> list[dict[str, Any]] | None:
            events, _ = _fetch_events(s, home)
            created = _events_named(events, "packet:created")
            return created if len(created) >= 2 else None

        created = _poll(s, S4_EVENTS_WAIT_S, poll_created)
        if created is None:
            events_now, malformed = _fetch_events(s, home)
            s.check(
                "events-packet-created-x2",
                False,
                f"expected 2 packet:created, saw {len(_events_named(events_now, 'packet:created'))} "
                f"(malformed lines: {malformed})",
            )
        else:
            s.check(
                "events-packet-created-x2",
                len(created) == 2,
                f"count={len(created)}, packet_ids={sorted(str(e.get('packet_id')) for e in created)}",
            )

        # 6. Notifications: HARD = every created packet id appears in well-formed
        #    batch records. SOFT (detail only) = ONE batch covered both.
        def poll_notify() -> tuple[list[dict[str, Any]], int] | None:
            result = s.ex.run(cmd_cat(notify_path), label="notify-fetch")
            if result.rc != 0:
                return None
            batches, bad = _parse_jsonl(result.stdout)
            shaped = [b for b in batches if "count" in b and isinstance(b.get("packets"), list)]
            notified_ids = {str(p.get("id")) for b in shaped for p in b["packets"]}
            if bad == 0 and len(shaped) == len(batches) and set(packet_ids) <= notified_ids:
                return batches, bad
            return None

        notify_result = _poll(s, S4_NOTIFY_WAIT_S, poll_notify)
        if notify_result is None:
            sink_raw = s.ex.run(cmd_cat(notify_path), label="notify-final-fetch")
            s.check(
                "all-created-packets-notified",
                False,
                f"created ids {packet_ids} not fully covered by well-formed batch records within "
                f"{S4_NOTIFY_WAIT_S:.0f}s; sink content: {sink_raw.stdout.strip()[:500]!r}",
            )
        else:
            batches, _ = notify_result
            covered_by_one = any(set(packet_ids) <= {str(p.get("id")) for p in b["packets"]} for b in batches)
            single = len(batches) == 1 and covered_by_one
            soft = (
                "single-batch=yes"
                if single
                else f"single-batch=no ({len(batches)} batches — packets arrived >window apart; "
                "acceptable: every packet was batch-notified)"
            )
            s.check(
                "all-created-packets-notified",
                True,
                f"both ids notified across {len(batches)} well-formed batch record(s); {soft}",
            )

        # 7. Durability: SIGKILL supervisor group, VERIFY it is dead, restart,
        #    ~2 ticks, no dupes. The dead-check is load-bearing: in the S4
        #    incident the group kill missed the real supervisor, it survived,
        #    and the "restart" silently ran TWO supervisors concurrently
        #    (duplicate packet:answered events, corrupted single-writer state).
        s.ex.run(cmd_kill_group(pid), label="sigkill-supervisor")
        time.sleep(1.0)
        alive_probe = s.ex.run(cmd_worker_alive(pid), label="post-kill-supervisor-alive")
        killed = alive_probe.stdout.strip() == "dead"
        s.check(
            "supervisor-killed", killed, f"post-SIGKILL probe of supervisor pgid {pid}: {alive_probe.stdout.strip()!r}"
        )
        if not killed:
            s.cne(
                "restart-no-duplicate-events", "old supervisor survived the kill — restarting would run two supervisors"
            )
        else:
            relaunch = s.ex.run(
                cmd_supervise_launch(cfg, s.queue_dir, home, s.dtu_sdir, notify_path, sup_log, sup_pgid_file),
                label="supervise-relaunch",
            )
            pid2 = _poll_pgid_file(s.ex, sup_pgid_file, "supervisor-pgid-read-2") if relaunch.rc == 0 else None
            if pid2 is None or pid2 == pid:
                s.cne("restart-no-duplicate-events", f"supervisor relaunch failed: rc={relaunch.rc}, pgid {pid2!r}")
            else:
                sup_pids.append(pid2)
                time.sleep(min(S4_RESTART_SETTLE_S, max(0.0, s.remaining())))
                events_after, malformed_after = _fetch_events(s, home)
                created_after = _events_named(events_after, "packet:created")
                s.check(
                    "restart-no-duplicate-events",
                    len(created_after) == 2,
                    f"packet:created count after SIGKILL+restart+~2 ticks: {len(created_after)} "
                    f"(restart pgid {pid2}; malformed lines: {malformed_after})",
                )

        # 8. Map packets to workers (tag primary, keywords fallback).
        mapping = _map_packets_to_workers(packets)
        if mapping is None:
            questions = {str(p.get("id")): str(p.get("question", ""))[:80] for p in packets}
            s.cne("packet-worker-mapping", f"ambiguous/incomplete tag+keyword mapping; questions={questions}")
            _cne_rest(s, S4_ASSERTION_NAMES, "cannot answer without a packet->worker mapping")
            return _finish(s, started)
        mapping_detail = {name: str(pkt.get("id")) for name, pkt in mapping.items()}
        s.check("packet-worker-mapping", True, f"{mapping_detail}")

        # 9. Answer both (w1->A, w2->B).
        answer_details: list[str] = []
        answers_ok = True
        for name, w in S4_WORKERS.items():
            pkt_id = str(mapping[name].get("id"))
            result = s.answer(pkt_id, str(w["answer"]))
            answer_details.append(f"{name}: {pkt_id} -> {w['answer']} rc={result.rc}")
            answers_ok = answers_ok and result.rc == 0
        s.check("answers-accepted", answers_ok, "; ".join(answer_details))

        # 10/11. Worker logs contain DECISION RECEIVED (poll both together).
        found = dict.fromkeys(S4_WORKERS, False)
        log_deadline = time.monotonic() + min(S4_WORKER_WAIT_S, max(0.0, s.remaining()))
        while time.monotonic() < log_deadline and not all(found.values()):
            for name, w in S4_WORKERS.items():
                if found[name]:
                    continue
                result = s.ex.run(cmd_cat(f"{home}/workers/{w['session']}/worker.log"), label=f"worker-log-{name}")
                if result.rc == 0 and str(w["needle"]) in _strip_ansi(result.stdout):
                    found[name] = True
            if not all(found.values()):
                time.sleep(POLL_INTERVAL_S)
        for name, w in S4_WORKERS.items():
            s.check(
                f"{name}-received-{w['answer']}",
                found[name],
                f"looked for {w['needle']!r} in {w['session']}/worker.log (ANSI-stripped, <= {S4_WORKER_WAIT_S:.0f}s)",
            )

        # 12/13. Events: 2 packet:answered (latency_s, covering BOTH packets) +
        # 2 worker:finished (judged:false, covering BOTH sessions). The poll
        # requires DISTINCT coverage, not raw counts — in the S4 incident,
        # duplicate am-w1 finish events satisfied a count>=2 poll while am-w2's
        # finish was still in flight, masking both defects.
        expected_sessions = {str(w["session"]) for w in S4_WORKERS.values()}

        def poll_final_events() -> list[dict[str, Any]] | None:
            events, _ = _fetch_events(s, home)
            answered_ids = {str(e.get("packet_id")) for e in _events_named(events, "packet:answered")}
            finished_sessions = {str(e.get("session")) for e in _events_named(events, "worker:finished")}
            if answered_ids >= set(packet_ids) and finished_sessions >= expected_sessions:
                return events
            return None

        final_events = _poll(s, S4_EVENTS_WAIT_S, poll_final_events)
        if final_events is None:
            final_events, _ = _fetch_events(s, home)
            answered_events = _events_named(final_events, "packet:answered")
            finished_events = _events_named(final_events, "worker:finished")
            s.check(
                "events-packet-answered-x2-latency",
                False,
                f"within budget only {len(answered_events)} packet:answered "
                f"(ids={sorted({str(e.get('packet_id')) for e in answered_events})}, need both of {packet_ids})",
            )
            s.check(
                "events-worker-finished-x2-judged-false",
                False,
                f"within budget only {len(finished_events)} worker:finished "
                f"(sessions={sorted({str(e.get('session')) for e in finished_events})}, "
                f"need both of {sorted(expected_sessions)})",
            )
        else:
            answered_events = _events_named(final_events, "packet:answered")
            s.check(
                "events-packet-answered-x2-latency",
                len(answered_events) == 2
                and {str(e.get("packet_id")) for e in answered_events} == set(packet_ids)
                and all(e.get("latency_s") is not None for e in answered_events),
                f"count={len(answered_events)}, ids={sorted(str(e.get('packet_id')) for e in answered_events)}, "
                f"latencies={[e.get('latency_s') for e in answered_events]}",
            )
            finished_events = _events_named(final_events, "worker:finished")
            s.check(
                "events-worker-finished-x2-judged-false",
                len(finished_events) == 2
                and {str(e.get("session")) for e in finished_events} == expected_sessions
                and all(e.get("judged") is False and e.get("exit_code") == 0 for e in finished_events),
                f"count={len(finished_events)}, "
                f"details={[{k: e.get(k) for k in ('session', 'exit_code', 'judged')} for e in finished_events]}",
            )

        # 13b. No duplicate (event, packet_id) pairs — the direct signature of
        # two supervisors writing the same home (S4 incident: each packet got
        # TWO identical packet:answered events).
        dupes = _duplicate_packet_events(final_events)
        s.check(
            "events-no-duplicates",
            not dupes,
            f"duplicate (event, packet_id) pairs: {dupes}" if dupes else "no duplicate packet:created/packet:answered",
        )

        # 14. Ledger tells the full story.
        ledger_result = s.ex.run(cmd_ledger_cat(home), label="ledger-fetch")
        ledger_records, ledger_malformed = _parse_jsonl(ledger_result.stdout)
        kinds: dict[str, int] = {}
        for record in ledger_records:
            kind = str(record.get("kind"))
            kinds[kind] = kinds.get(kind, 0) + 1
        expected = {"dispatched": 2, "packet_created": 2, "packet_answered": 2, "worker_finished": 2}
        mismatches = {k: (kinds.get(k, 0), v) for k, v in expected.items() if kinds.get(k, 0) != v}
        s.check(
            "ledger-full-story",
            not mismatches and kinds.get("notified_batch", 0) >= 1 and ledger_malformed == 0,
            f"kinds={kinds}, mismatches={mismatches or 'none'}, malformed_lines={ledger_malformed}",
        )
        return _finish(s, started)
    finally:
        _collect_s4_artifacts(s, home, notify_path, sup_log)
        for sup_pid in sup_pids:
            s.ex.run(cmd_kill_group(sup_pid), label="cleanup-kill-supervisor")
        for w in S4_WORKERS.values():
            s.ex.run(cmd_tmux_kill(str(w["session"])), label=f"cleanup-kill-{w['session']}")


# -- scenario 5: cold triage pass (step 3) -------------------------------------------


def _collect_s5_artifacts(s: Scenario, home: str) -> None:
    """Best-effort artifact collection — runs even when the scenario bails early."""
    session_logs_cmd = (
        f'for f in {shlex.quote(home)}/triage/*/*; do echo "=== $f ==="; cat "$f"; echo; done 2>/dev/null || true'
    )
    artifacts = [
        ("rulebook-after", cmd_cat(f"{home}/rulebook.md"), "rulebook-after.md"),
        ("proposals", cmd_cat(f"{home}/rulebook-proposals.jsonl"), "rulebook-proposals.jsonl"),
        ("events", cmd_cat(f"{home}/events.jsonl"), "events.jsonl"),
        ("ledger", cmd_ledger_cat(home), "ledger.jsonl"),
        ("triage-sessions", session_logs_cmd, "triage-sessions.log"),
    ]
    for label, cmd, filename in artifacts:
        try:
            result = s.ex.run(cmd, label=f"artifact-{label}")
            if result.rc == 0:
                (s.out_dir / filename).write_text(result.stdout, encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — artifact collection must never mask the verdict
            print(f"    (artifact {label} not collected: {e})")


def _read_packet_file(s: Scenario, path: str, label: str) -> dict[str, Any] | None:
    result = s.ex.run(cmd_cat(path), label=label)
    if result.rc != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _rulebook_section_body(content: str, section: str) -> str | None:
    """Return the text under '## <section>' up to the next '## ' heading."""
    marker = f"## {section}"
    lines = content.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == marker), None)
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


def _s5_triage_pass(s: Scenario, cfg: Config, home: str, bundle_uri: str, n: int) -> ExecResult:
    """Run `triage --once` pass N; persist its stdout as an artifact."""
    out_log = f"{s.dtu_sdir}/triage-pass-{n}.out"
    result = s.ex.run(
        cmd_triage_once(cfg, s.queue_dir, home, bundle_uri, out_log),
        label=f"triage-pass-{n}",
        timeout=min(S5_TRIAGE_EXEC_TIMEOUT_S, max(30.0, s.remaining())),
    )
    (s.out_dir / f"triage-pass-{n}.out").write_text(result.stdout, encoding="utf-8")
    return result


def run_scenario_5(cfg: Config) -> ScenarioResult:
    spec = SPECS[5]
    s = Scenario(cfg, spec)
    started = time.monotonic()
    home = f"{s.dtu_sdir}/home"
    bundle_uri = f"file://{cfg.repo_dir}/{spec.bundle_rel}"
    try:
        # 1. Seed the rulebook (template + seed rule) and grade the readback.
        seeded = s.ex.run(cmd_seed_rulebook(home), label="seed-rulebook")
        (s.out_dir / "rulebook-before.md").write_text(seeded.stdout, encoding="utf-8")
        headings_ok = all(f"## {sec}" in seeded.stdout for sec in S5_RULEBOOK_SECTIONS)
        if not s.check(
            "rulebook-seeded",
            seeded.rc == 0 and S5_SEED_RULE in seeded.stdout and headings_ok,
            f"rc={seeded.rc}, seed rule present={S5_SEED_RULE in seeded.stdout}, all 5 headings={headings_ok}",
        ):
            _cne_rest(s, S5_ASSERTION_NAMES, "rulebook seeding failed")
            return _finish(s, started)

        # 2. Seed P1/P2 via the root queue lib; ids print in order.
        seeded_p = s.ex.run(cmd_seed_packets(cfg, s.queue_dir), label="seed-packets")
        ids = [ln.strip() for ln in seeded_p.stdout.splitlines() if ln.strip().startswith("pkt-")]
        if not s.check(
            "packets-seeded",
            seeded_p.rc == 0 and len(ids) == 2 and ids[0] != ids[1],
            f"rc={seeded_p.rc}, ids={ids}, stderr={seeded_p.stderr.strip()[:200]!r}",
        ):
            _cne_rest(s, S5_ASSERTION_NAMES, "packet seeding failed")
            return _finish(s, started)
        p1, p2 = ids
        s.snapshot("post-seed", s.ex.run(cmd_queue_list(cfg, s.queue_dir), label="queue-list-post-seed"))

        # 3. Pass 1 — real LLM triage on both packets (synchronous).
        pass1 = _s5_triage_pass(s, cfg, home, bundle_uri, 1)
        s.check(
            "triage-pass-1-ok",
            pass1.rc == 0,
            f"rc={pass1.rc} (rc==0 means the pass ran; per-packet verdicts are graded below) "
            f"stdout tail: {pass1.stdout.strip()[-300:]!r}",
        )

        # 4. P1: recommended, still pending, triage fields filled.
        p1_packet = _read_packet_file(s, f"{s.queue_dir}/pending/{p1}.json", "p1-pending-read")
        rec_opt: str | None = None
        if p1_packet is None:
            elsewhere = s.ex.run(cmd_queue_show(cfg, s.queue_dir, p1), label="p1-locate")
            s.check(
                "p1-recommended-pending",
                False,
                f"pending/{p1}.json missing/unparseable — queue show says: {elsewhere.stdout.strip()[:300]!r}",
            )
        else:
            (s.out_dir / "p1-pending.json").write_text(json.dumps(p1_packet, indent=2), encoding="utf-8")
            triage = p1_packet.get("triage") or {}
            recommendation = p1_packet.get("recommendation") or {}
            rec_opt = recommendation.get("option")
            s.check(
                "p1-recommended-pending",
                triage.get("handled_by") == "manager-recommend"
                and bool(str(triage.get("why", "")).strip())
                and isinstance(triage.get("rule_refs"), list)
                and rec_opt in ("A", "B"),
                f"triage={{handled_by: {triage.get('handled_by')!r}, why: {str(triage.get('why', ''))[:120]!r}, "
                f"rule_refs: {triage.get('rule_refs')!r}}}, recommendation.option={rec_opt!r}",
            )

        # 5. P2: bounced with a reason; pending entry gone.
        p2_packet = _read_packet_file(s, f"{s.queue_dir}/bounced/{p2}.json", "p2-bounced-read")
        if p2_packet is None:
            elsewhere = s.ex.run(cmd_queue_show(cfg, s.queue_dir, p2), label="p2-locate")
            s.check(
                "p2-bounced",
                False,
                f"bounced/{p2}.json missing/unparseable — queue show says: {elsewhere.stdout.strip()[:300]!r}",
            )
        else:
            (s.out_dir / "p2-bounced.json").write_text(json.dumps(p2_packet, indent=2), encoding="utf-8")
            p2_why = str((p2_packet.get("triage") or {}).get("why", ""))
            bounce_reason = p2_why.split("bounce:", 1)[1].strip() if "bounce:" in p2_why else ""
            p2_pending_gone = s.ex.run(cmd_file_absent(f"{s.queue_dir}/pending/{p2}.json"), label="p2-pending-absent")
            s.check(
                "p2-bounced",
                bool(bounce_reason) and p2_pending_gone.stdout.strip() == "absent",
                f"bounce reason={bounce_reason[:150]!r}, pending entry: {p2_pending_gone.stdout.strip()}",
            )

        # 6. Events: exactly 1 recommended(P1) + 1 bounced(P2); errors recorded in detail.
        events, malformed = _fetch_events(s, home)
        recommended = [e for e in _events_named(events, "triage:recommended") if e.get("packet_id") == p1]
        bounced = [e for e in _events_named(events, "triage:bounced") if e.get("packet_id") == p2]
        n_errors = len(_events_named(events, "triage:error"))
        s.check(
            "events-triage",
            len(recommended) == 1 and len(bounced) == 1,
            f"triage:recommended(P1)={len(recommended)}, triage:bounced(P2)={len(bounced)}, "
            f"triage:error={n_errors} (retries are loud, not failures), malformed lines={malformed}",
        )

        # 7. Ledger records the triage story.
        ledger_records, _ = _parse_jsonl(s.ex.run(cmd_ledger_cat(home), label="ledger-fetch-1").stdout)
        kinds: dict[str, int] = {}
        for record in ledger_records:
            kind = str(record.get("kind"))
            kinds[kind] = kinds.get(kind, 0) + 1
        s.check(
            "ledger-triage",
            kinds.get("triage_recommended", 0) == 1 and kinds.get("triage_bounced", 0) == 1,
            f"kinds={kinds}",
        )

        # 8. Answer P1 with the OPPOSITE of triage's recommendation.
        if rec_opt not in ("A", "B"):
            s.cne("answer-opposite-accepted", "no valid triage recommendation on P1 to oppose")
            _cne_rest(s, S5_ASSERTION_NAMES, "cannot proceed without an answered P1")
            return _finish(s, started)
        opposite = "B" if rec_opt == "A" else "A"
        answer = s.answer(p1, opposite)
        s.check(
            "answer-opposite-accepted",
            answer.rc == 0,
            f"triage recommended {rec_opt} -> answered {opposite} (rc={answer.rc}, "
            f"stderr={answer.stderr.strip()[:150]!r})",
        )
        p1_answered = _read_packet_file(s, f"{s.queue_dir}/answered/{p1}.json", "p1-answered-read")
        if p1_answered is not None:
            (s.out_dir / "p1-answered.json").write_text(json.dumps(p1_answered, indent=2), encoding="utf-8")

        # 9. Pass 2 — rule_delta for the answered P1.
        pass2 = _s5_triage_pass(s, cfg, home, bundle_uri, 2)
        s.check("triage-pass-2-ok", pass2.rc == 0, f"rc={pass2.rc}, stdout tail: {pass2.stdout.strip()[-300:]!r}")

        # 10. Exactly ONE rule_delta record for P1 (proposal or explicit none).
        proposals_raw = s.ex.run(cmd_cat(f"{home}/rulebook-proposals.jsonl"), label="proposals-fetch-1")
        records, bad = _parse_jsonl(proposals_raw.stdout if proposals_raw.rc == 0 else "")
        p1_records = [r for r in records if r.get("packet_id") == p1]
        branch: str | None = None
        proposal_record: dict[str, Any] | None = None
        if len(p1_records) != 1:
            s.check(
                "one-rule-delta-record",
                False,
                f"expected exactly 1 record for P1, got {len(p1_records)} "
                f"(file rc={proposals_raw.rc}, total records={len(records)}, malformed={bad})",
            )
        else:
            record = p1_records[0]
            status = record.get("status")
            if status == "proposed":
                branch = "proposal"
                proposal_record = record
                ok = bool(str(record.get("sentence", "")).strip()) and record.get("section") in S5_RULEBOOK_SECTIONS
                s.check(
                    "one-rule-delta-record",
                    ok,
                    f"branch=proposal; section={record.get('section')!r}, "
                    f"sentence={str(record.get('sentence', ''))[:120]!r}",
                )
            elif status == "none":
                branch = "none"
                ok = bool(str(record.get("reason", "")).strip())
                s.check(
                    "one-rule-delta-record",
                    ok,
                    f"branch=none (explicit one-off); reason={str(record.get('reason', ''))[:150]!r}",
                )
            else:
                s.check("one-rule-delta-record", False, f"unexpected record status {status!r}: {record}")

        # 11. Pass 3 — idempotency: P1's record count unchanged.
        pass3 = _s5_triage_pass(s, cfg, home, bundle_uri, 3)
        records_after, _ = _parse_jsonl(
            s.ex.run(cmd_cat(f"{home}/rulebook-proposals.jsonl"), label="proposals-fetch-2").stdout
        )
        p1_after = [r for r in records_after if r.get("packet_id") == p1]
        s.check(
            "third-pass-idempotent",
            pass3.rc == 0 and len(p1_after) == len(p1_records) == 1,
            f"pass3 rc={pass3.rc}, P1 record count before={len(p1_records)} after={len(p1_after)}",
        )

        # 12. Apply branch.
        if branch == "proposal" and proposal_record is not None:
            proposal_id = str(proposal_record.get("id"))
            target_section = str(proposal_record.get("section"))
            sentence = str(proposal_record.get("sentence", "")).strip()
            applied = s.ex.run(cmd_rulebook_apply(cfg, s.queue_dir, home, proposal_id), label="rulebook-apply")
            rulebook_after = s.ex.run(cmd_cat(f"{home}/rulebook.md"), label="rulebook-after-read")
            section_body = _rulebook_section_body(rulebook_after.stdout, target_section)
            landed = section_body is not None and f"- {sentence}" in section_body
            s.check(
                "rulebook-apply-branch",
                applied.rc == 0 and landed,
                f"branch=proposal; apply rc={applied.rc}, sentence under '## {target_section}': {landed} "
                f"(stderr={applied.stderr.strip()[:150]!r})",
            )
        elif branch == "none":
            s.check(
                "rulebook-apply-branch",
                True,
                "branch=none — explicit none-record with non-empty reason is a valid Phase-1 outcome; nothing to apply",
            )
        else:
            s.cne("rulebook-apply-branch", "no valid rule_delta record to apply (see one-rule-delta-record)")
        return _finish(s, started)
    finally:
        _collect_s5_artifacts(s, home)


# -- scenario 6: judged finish lines (step 4) ----------------------------------------


def _collect_s6_artifacts(s: Scenario, home: str, notify_path: str, sup_log: str) -> None:
    """Best-effort artifact collection — runs even when the scenario bails early."""
    artifacts = [
        ("supervisor-log", cmd_cat(sup_log), "supervisor.log"),
        ("events-copy", cmd_cat(f"{home}/events.jsonl"), "events.jsonl"),
        ("ledger-copy", cmd_ledger_cat(home), "ledger.jsonl"),
        ("notify-copy", cmd_cat(notify_path), "notify.jsonl"),
        ("tmux-ls", cmd_tmux_ls(), "tmux-ls.txt"),
        ("judge-log-g", cmd_cat(f"{home}/workers/am-g/judge.log"), "judge-am-g.log"),
        ("judge-log-b", cmd_cat(f"{home}/workers/am-b/judge.log"), "judge-am-b.log"),
        ("worker-log-g", cmd_cat(f"{home}/workers/am-g/worker.log"), "worker-am-g.log"),
        ("worker-log-b", cmd_cat(f"{home}/workers/am-b/worker.log"), "worker-am-b.log"),
        ("worker-log-u", cmd_cat(f"{home}/workers/am-u/worker.log"), "worker-am-u.log"),
    ]
    for label, cmd, filename in artifacts:
        try:
            result = s.ex.run(cmd, label=f"artifact-{label}")
            if result.rc == 0:
                (s.out_dir / filename).write_text(result.stdout, encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — artifact collection must never mask the verdict
            print(f"    (artifact {label} not collected: {e})")


def run_scenario_6(cfg: Config) -> ScenarioResult:
    spec = SPECS[6]
    s = Scenario(cfg, spec)
    started = time.monotonic()
    home = f"{s.dtu_sdir}/home"
    notify_path = f"{s.dtu_sdir}/notify.jsonl"
    sup_log = f"{s.dtu_sdir}/supervisor.log"
    sup_pgid_file = f"{s.dtu_sdir}/supervisor.pgid"
    good_art = f"{s.dtu_sdir}/good-artifact.txt"
    broken_art = f"{s.dtu_sdir}/broken-artifact.txt"
    sup_pids: list[str] = []
    try:
        # 0. Pre-clean fixed-name sessions.
        for session in S6_SESSIONS:
            s.ex.run(cmd_tmux_kill(session), label=f"pre-kill-{session}")

        # 1a. judge verify — a WORKING judge passes both directions.
        seeded = s.ex.run(cmd_make_verify_artifacts(s.dtu_sdir), label="seed-verify-artifacts")
        if seeded.rc != 0:
            s.cne("judge-verify-pass", f"could not seed verify artifacts (rc={seeded.rc})")
            _cne_rest(s, S6_ASSERTION_NAMES, "verify artifacts missing")
            return _finish(s, started)
        verify = s.ex.run(
            cmd_judge_verify(cfg, s.queue_dir, home, S6_VERIFY_JUDGE_CMD, good_art, broken_art),
            label="judge-verify",
        )
        (s.out_dir / "judge-verify.out").write_text(verify.stdout + verify.stderr, encoding="utf-8")
        s.check(
            "judge-verify-pass",
            verify.rc == 0 and "VERDICT: PASS" in verify.stdout,
            f"rc={verify.rc}, VERDICT: PASS present={'VERDICT: PASS' in verify.stdout}",
        )

        # 1b. A decoration judge (never fails) must be REJECTED.
        decoration = s.ex.run(
            cmd_judge_verify(cfg, s.queue_dir, home, "true", good_art, broken_art),
            label="judge-verify-decoration",
        )
        (s.out_dir / "judge-verify-decoration.out").write_text(decoration.stdout + decoration.stderr, encoding="utf-8")
        s.check(
            "judge-verify-rejects-decoration",
            decoration.rc != 0,
            f"rc={decoration.rc} (nonzero required: a judge that never fails is decoration)",
        )

        # 2. Supervisor up (short batch window; nothing beyond delivery asserted).
        s.ex.run(
            cmd_supervise_launch(
                cfg,
                s.queue_dir,
                home,
                s.dtu_sdir,
                notify_path,
                sup_log,
                sup_pgid_file,
                batch_window_s=S6_BATCH_WINDOW_S,
            ),
            label="supervise-launch",
        )
        pid = _poll_pgid_file(s.ex, sup_pgid_file, "supervisor-pgid-read")
        if pid is None:
            s.cne("supervisor-started", "supervisor.pgid never appeared")
            _cne_rest(s, S6_ASSERTION_NAMES, "supervisor never started")
            return _finish(s, started)
        sup_pids.append(pid)
        s.check("supervisor-started", True, f"supervisor pgid {pid} (from supervisor.pgid)")

        # 3. Dispatch the fleet: G (marker artifact + judge), B (no marker, SAME judge), U (no judge).
        fleet = {
            "g": (f"echo {S6_MARKER} > {home}/workers/am-g/artifact.txt", S6_WORKER_JUDGE_CMD),
            "b": (f"echo marker deliberately omitted > {home}/workers/am-b/artifact.txt", S6_WORKER_JUDGE_CMD),
            "u": ("echo unjudged", None),
        }
        dispatch_details: list[str] = []
        dispatch_ok = True
        for name, (worker_cmd, judge_cmd) in fleet.items():
            result = s.ex.run(
                cmd_dispatch_fake(
                    cfg, s.queue_dir, home, name, f"finish-line {name} worker (fake)", worker_cmd, judge_cmd
                ),
                label=f"dispatch-{name}",
            )
            dispatch_details.append(
                f"{name}: rc={result.rc}" + (f" stderr={result.stderr.strip()[:120]!r}" if result.rc else "")
            )
            dispatch_ok = dispatch_ok and result.rc == 0
        s.check("workers-dispatched", dispatch_ok, "; ".join(dispatch_details))
        if not dispatch_ok:
            _cne_rest(s, S6_ASSERTION_NAMES, "dispatch failed")
            return _finish(s, started)

        # 4. All three worker:finished events.
        def poll_finished() -> list[dict[str, Any]] | None:
            events_now, _ = _fetch_events(s, home)
            finished_now = _events_named(events_now, "worker:finished")
            return finished_now if len(finished_now) >= 3 else None

        finished = _poll(s, S6_FINISH_WAIT_S, poll_finished)
        if finished is None:
            events_now, _ = _fetch_events(s, home)
            s.check(
                "three-worker-finished",
                False,
                f"expected 3 worker:finished within {S6_FINISH_WAIT_S:.0f}s, "
                f"saw {len(_events_named(events_now, 'worker:finished'))}",
            )
            _cne_rest(s, S6_ASSERTION_NAMES, "workers never finished")
            return _finish(s, started)
        by_session = {str(e.get("session")): e for e in finished}
        s.check(
            "three-worker-finished",
            len(finished) == 3 and set(by_session) == set(S6_SESSIONS),
            f"sessions={sorted(by_session)}",
        )

        # 5. Judge-gated loop events.
        events, malformed = _fetch_events(s, home)
        closed = _events_named(events, "loop:closed")
        failed = _events_named(events, "loop:failed")

        closed_ok = (
            len(closed) == 1
            and closed[0].get("session") == "am-g"
            and bool(str(closed[0].get("judge_output", "")).strip())
        )
        s.check(
            "loop-closed-good",
            closed_ok,
            f"loop:closed count={len(closed)}, session={closed[0].get('session') if closed else None!r}, "
            f"judge_output tail={str(closed[0].get('judge_output', ''))[:120]!r}"
            if closed
            else f"loop:closed count=0 (malformed lines: {malformed})",
        )

        judge_log = s.ex.run(cmd_cat(f"{home}/workers/am-g/judge.log"), label="judge-log-g-read")
        s.check(
            "judge-log-good",
            judge_log.rc == 0 and bool(judge_log.stdout.strip()),
            f"rc={judge_log.rc}, size={len(judge_log.stdout.strip())} chars",
        )

        failed_ok = (
            len(failed) == 1 and failed[0].get("session") == "am-b" and bool(str(failed[0].get("reason", "")).strip())
        )
        s.check(
            "loop-failed-bad",
            failed_ok,
            f"loop:failed count={len(failed)}, session={failed[0].get('session') if failed else None!r}, "
            f"reason={str(failed[0].get('reason', ''))!r} (expected 'judge exited 1')"
            if failed
            else "loop:failed count=0",
        )

        g_ev, b_ev, u_ev = by_session.get("am-g", {}), by_session.get("am-b", {}), by_session.get("am-u", {})
        s.check(
            "finished-judged-fields",
            g_ev.get("judged") is True
            and g_ev.get("judge_result") == "closed"
            and b_ev.get("judged") is True
            and b_ev.get("judge_result") == "failed",
            f"am-g: judged={g_ev.get('judged')}, judge_result={g_ev.get('judge_result')!r}; "
            f"am-b: judged={b_ev.get('judged')}, judge_result={b_ev.get('judge_result')!r}",
        )

        u_loops = [e for e in closed + failed if e.get("session") == "am-u"]
        s.check(
            "unjudged-worker",
            u_ev.get("judged") is False and u_ev.get("judge_result") is None and not u_loops,
            f"am-u: judged={u_ev.get('judged')}, judge_result={u_ev.get('judge_result')!r}, "
            f"loop:* events for am-u={len(u_loops)}",
        )

        # 6. Notification items: finish_line (am-g) + finish_line_failed (am-b).
        def poll_notify() -> list[dict[str, Any]] | None:
            result = s.ex.run(cmd_cat(notify_path), label="notify-fetch")
            if result.rc != 0:
                return None
            batches, bad = _parse_jsonl(result.stdout)
            items = [p for b in batches if isinstance(b.get("packets"), list) for p in b["packets"]]
            has_line = any(i.get("kind") == "finish_line" and i.get("id") == "am-g" for i in items)
            has_failed = any(i.get("kind") == "finish_line_failed" and i.get("id") == "am-b" for i in items)
            return items if bad == 0 and has_line and has_failed else None

        notify_items = _poll(s, S6_NOTIFY_WAIT_S, poll_notify)
        if notify_items is None:
            sink_raw = s.ex.run(cmd_cat(notify_path), label="notify-final-fetch")
            s.check(
                "notify-finish-line-items",
                False,
                f"finish_line(am-g) + finish_line_failed(am-b) items not found within {S6_NOTIFY_WAIT_S:.0f}s; "
                f"sink content: {sink_raw.stdout.strip()[:400]!r}",
            )
        else:
            kinds = [(i.get("kind"), i.get("id")) for i in notify_items]
            s.check("notify-finish-line-items", True, f"items={kinds}")

        # 7. Ledger counts.
        ledger_records, ledger_malformed = _parse_jsonl(s.ex.run(cmd_ledger_cat(home), label="ledger-fetch").stdout)
        kinds_count: dict[str, int] = {}
        for record in ledger_records:
            kind = str(record.get("kind"))
            kinds_count[kind] = kinds_count.get(kind, 0) + 1
        expected = {"loop_closed": 1, "loop_failed": 1, "worker_finished": 3, "dispatched": 3}
        mismatches = {k: (kinds_count.get(k, 0), v) for k, v in expected.items() if kinds_count.get(k, 0) != v}
        s.check(
            "ledger-counts",
            not mismatches and ledger_malformed == 0,
            f"kinds={kinds_count}, mismatches={mismatches or 'none'}, malformed={ledger_malformed}",
        )

        # 8. ledger --summary --json renders both loops by name.
        summary = s.ex.run(cmd_ledger_summary(cfg, s.queue_dir, home), label="ledger-summary")
        (s.out_dir / "ledger-summary.json").write_text(summary.stdout, encoding="utf-8")
        summary_ok = False
        summary_detail = f"rc={summary.rc}"
        if summary.rc == 0:
            try:
                data = json.loads(summary.stdout)
                closed_sessions = [e.get("session") for e in data.get("loops_closed", [])]
                failed_sessions = [e.get("session") for e in data.get("loops_failed", [])]
                summary_ok = "am-g" in closed_sessions and "am-b" in failed_sessions
                summary_detail = f"rc=0, loops_closed={closed_sessions}, loops_failed={failed_sessions}"
            except json.JSONDecodeError as e:
                summary_detail = f"rc=0 but output not JSON: {e}"
        s.check("ledger-summary-renders", summary_ok, summary_detail)
        return _finish(s, started)
    finally:
        _collect_s6_artifacts(s, home, notify_path, sup_log)
        for sup_pid in sup_pids:
            s.ex.run(cmd_kill_group(sup_pid), label="cleanup-kill-supervisor")
        for session in S6_SESSIONS:
            s.ex.run(cmd_tmux_kill(session), label=f"cleanup-kill-{session}")


# -- scenario 7: attractor gate (step 5) ----------------------------------------------


def _s7_error_excerpt(log_text: str) -> str:
    """Extract the missing-extra error lines from the workunit log (capped)."""
    lines = [ln.strip() for ln in log_text.splitlines() if any(m in ln for m in S7_MISSING_EXTRA_MARKERS)]
    return " | ".join(lines)[:500] if lines else log_text.strip()[-400:]


def _collect_s7_artifacts(s: Scenario, home: str, dtu_sdir: str, workdir: str) -> None:
    """Best-effort artifact collection — runs even when the scenario bails early."""
    pipeline_logs_cmd = (
        f"find {shlex.quote(home + '/workunits')} -type f 2>/dev/null | sort | "
        f'while read -r f; do echo "=== $f ==="; cat "$f"; echo; done'
    )
    workdir_state_cmd = (
        f"ls -la {shlex.quote(workdir)} 2>/dev/null; echo ---; "
        f"echo A.txt:; cat {shlex.quote(workdir)}/A.txt 2>/dev/null; "
        f"echo R.txt:; cat {shlex.quote(workdir)}/R.txt 2>/dev/null; true"
    )
    artifacts = [
        ("workunit-log", cmd_cat(f"{dtu_sdir}/wu.log"), "workunit.log"),
        ("workunit-exit", cmd_cat(f"{dtu_sdir}/wu.exit"), "workunit.exit"),
        ("events", cmd_cat(f"{home}/events.jsonl"), "events.jsonl"),
        ("ledger", cmd_ledger_cat(home), "ledger.jsonl"),
        ("pipeline-logs", pipeline_logs_cmd, "pipeline-logs.txt"),
        ("workdir-state", workdir_state_cmd, "workdir-state.txt"),
    ]
    for label, cmd, filename in artifacts:
        try:
            result = s.ex.run(cmd, label=f"artifact-{label}")
            if result.rc == 0:
                (s.out_dir / filename).write_text(result.stdout, encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — artifact collection must never mask the verdict
            print(f"    (artifact {label} not collected: {e})")


def run_scenario_7(cfg: Config) -> ScenarioResult:
    spec = SPECS[7]
    s = Scenario(cfg, spec)
    started = time.monotonic()
    home = f"{s.dtu_sdir}/home"
    workdir = f"{s.dtu_sdir}/work"
    wu_exit = f"{s.dtu_sdir}/wu.exit"
    wu_log = f"{s.dtu_sdir}/wu.log"
    pipeline = f"{cfg.repo_dir}/{spec.bundle_rel}"
    wu_pgid: str | None = None
    try:
        # 1. Launch the workunit in the background (cwd = workdir; pgid captured).
        s.ex.run(
            cmd_workunit_launch(cfg, s.queue_dir, home, s.dtu_sdir, workdir, pipeline, S7_NAME),
            label="workunit-launch",
        )
        wu_pgid = _poll_pgid_file(s.ex, f"{s.dtu_sdir}/wu.pgid", "wu-pgid-read")
        if wu_pgid is None:
            s.cne("workunit-launched", "wu.pgid never appeared")
            _cne_rest(s, S7_ASSERTION_NAMES, "workunit never launched")
            return _finish(s, started)

        # 2. Poll for the attractor-gate packet, detecting early death (e.g. the
        #    [attractor] extra missing — an honest env failure, captured loud).
        dead = {"flag": False}

        def poll_gate() -> dict[str, Any] | None:
            probe = s.ex.run(cmd_file_exists(wu_exit), label="wu-exit-probe")
            if probe.stdout.strip() == "yes":
                dead["flag"] = True
                return {}  # sentinel: stop polling, handle below
            result = s.ex.run(cmd_queue_list(cfg, s.queue_dir), label="queue-list-poll")
            s.snapshot("queue-list-poll", result)
            if result.rc != 0:
                return None
            try:
                listed = json.loads(result.stdout)
            except json.JSONDecodeError:
                return None
            for p in listed:
                if isinstance(p, dict) and (p.get("source") or {}).get("kind") == spec.kind:
                    return p
            return None

        found = _poll(s, S7_GATE_WAIT_S, poll_gate)
        if dead["flag"]:
            exit_code = s.ex.run(cmd_cat(wu_exit), label="wu-exit-read").stdout.strip()
            log_text = s.ex.run(cmd_cat(wu_log), label="wu-log-read").stdout
            (s.out_dir / "workunit.log").write_text(log_text, encoding="utf-8")
            if any(m in log_text for m in S7_MISSING_EXTRA_MARKERS):
                s.cne(
                    "workunit-launched",
                    f"the [attractor] extra is NOT installed in this environment — honest env failure "
                    f"(workunit exited {exit_code} before publishing a gate). Captured error: "
                    f"{_s7_error_excerpt(log_text)}",
                )
            else:
                s.check(
                    "workunit-launched",
                    False,
                    f"workunit died (exit {exit_code}) before publishing a gate; log tail: {log_text[-400:]!r}",
                )
            _cne_rest(s, S7_ASSERTION_NAMES, "workunit died before publishing a gate")
            return _finish(s, started)
        if found is None:
            s.check("workunit-launched", True, f"pgid {wu_pgid} (process still running)")
            s.check("gate-packet-shape", False, f"no kind={spec.kind} packet within {S7_GATE_WAIT_S:.0f}s")
            _cne_rest(s, S7_ASSERTION_NAMES, "gate packet never appeared")
            return _finish(s, started)

        packet = found
        packet_id = str(packet.get("id", ""))
        s.check("workunit-launched", True, f"pgid {wu_pgid}; gate packet {packet_id} published")
        (s.out_dir / "packet-pending.json").write_text(json.dumps(packet, indent=2), encoding="utf-8")

        # 3. Packet shape.
        option_ids = [o.get("id") for o in packet.get("options", []) if isinstance(o, dict)]
        option_labels = [str(o.get("label", "")) for o in packet.get("options", []) if isinstance(o, dict)]
        work_unit = (packet.get("source") or {}).get("work_unit")
        shape_ok = (
            packet.get("question") == S7_QUESTION
            and option_ids == ["A", "R"]
            and len(option_labels) == 2
            and "Approve" in option_labels[0]
            and "Reject" in option_labels[1]
            and work_unit == S7_NAME
            and "stage: gate" in str(packet.get("context", ""))
        )
        s.check(
            "gate-packet-shape",
            shape_ok,
            f"question={packet.get('question')!r}, options={list(zip(option_ids, option_labels))}, "
            f"work_unit={work_unit!r}, context={str(packet.get('context', ''))[:80]!r}",
        )

        # 4. gate:packet_created event (emitted alongside the queue write).
        def poll_created() -> list[dict[str, Any]] | None:
            events_now, _ = _fetch_events(s, home)
            created = [e for e in _events_named(events_now, "gate:packet_created") if e.get("packet_id") == packet_id]
            return created or None

        created = _poll(s, S7_EVENT_WAIT_S, poll_created)
        if created is None:
            s.check(
                "events-gate-created",
                False,
                f"no gate:packet_created event for {packet_id} within {S7_EVENT_WAIT_S:.0f}s",
            )
        else:
            ev = created[0]
            s.check(
                "events-gate-created",
                ev.get("work_unit") == S7_NAME and bool(ev.get("packet_id")),
                f"{{work_unit: {ev.get('work_unit')!r}, stage: {ev.get('stage')!r}, packet_id: {ev.get('packet_id')!r}}}",
            )

        # 5. Answer A.
        answer = s.answer(packet_id, spec.answer_option)
        s.check("answer-accepted", answer.rc == 0, f"answer rc={answer.rc} stderr={answer.stderr.strip()[:150]!r}")

        # 6. Workunit completes exit 0.
        exit_content: str | None = None
        deadline = time.monotonic() + min(S7_COMPLETE_WAIT_S, max(0.0, s.remaining()))
        while time.monotonic() < deadline:
            probe = s.ex.run(cmd_file_exists(wu_exit), label="wu-exit-poll")
            if probe.stdout.strip() == "yes":
                exit_content = s.ex.run(cmd_cat(wu_exit), label="wu-exit-read").stdout.strip()
                break
            time.sleep(POLL_INTERVAL_S)
        if exit_content is None:
            s.cne("workunit-completed", f"wu.exit never appeared within {S7_COMPLETE_WAIT_S:.0f}s after the answer")
        else:
            s.check("workunit-completed", exit_content == "0", f"wu.exit={exit_content!r}")

        # 7. Route: A.txt (content "A") exists, R.txt absent.
        a_txt = s.ex.run(cmd_cat(f"{workdir}/A.txt"), label="a-txt-read")
        r_absent = s.ex.run(cmd_file_absent(f"{workdir}/R.txt"), label="r-txt-absent")
        s.check(
            "route-A-taken",
            a_txt.rc == 0 and a_txt.stdout.strip() == "A" and r_absent.stdout.strip() == "absent",
            f"A.txt rc={a_txt.rc} content={a_txt.stdout.strip()!r}; R.txt: {r_absent.stdout.strip()}",
        )

        # 8. Events: gate:answered {answer: A} + workunit:finished {status: success}.
        events, malformed = _fetch_events(s, home)
        answered_evs = [e for e in _events_named(events, "gate:answered") if e.get("packet_id") == packet_id]
        finished_evs = [e for e in _events_named(events, "workunit:finished") if e.get("name") == S7_NAME]
        s.check(
            "events-answered-finished",
            len(answered_evs) == 1
            and answered_evs[0].get("answer") == spec.answer_option
            and len(finished_evs) == 1
            and finished_evs[0].get("status") == "success",
            f"gate:answered answers={[e.get('answer') for e in answered_evs]}, "
            f"workunit:finished statuses={[e.get('status') for e in finished_evs]} (malformed lines: {malformed})",
        )

        # 9. Ledger: workunit_finished.
        ledger_records, bad = _parse_jsonl(s.ex.run(cmd_ledger_cat(home), label="ledger-fetch").stdout)
        wu_entries = [r for r in ledger_records if r.get("kind") == "workunit_finished" and r.get("name") == S7_NAME]
        s.check(
            "ledger-workunit-finished",
            len(wu_entries) == 1 and wu_entries[0].get("status") == "success",
            f"entries={wu_entries}, malformed={bad}",
        )
        return _finish(s, started)
    finally:
        _collect_s7_artifacts(s, home, s.dtu_sdir, workdir)
        if wu_pgid:
            s.ex.run(cmd_kill_group(wu_pgid), label="cleanup-kill-workunit")


# -- scenario 8: graduated trust auto-answer (step 6) ---------------------------------


def run_scenario_8(cfg: Config) -> ScenarioResult:
    spec = SPECS[8]
    s = Scenario(cfg, spec)
    started = time.monotonic()
    home = f"{s.dtu_sdir}/home"
    bundle_uri = f"file://{cfg.repo_dir}/{spec.bundle_rel}"
    try:
        # 1. Seed the PRE-PROMOTED rulebook.
        seeded = s.ex.run(cmd_seed_rulebook(home, S8_RULEBOOK_CONTENT), label="seed-rulebook")
        (s.out_dir / "rulebook-before.md").write_text(seeded.stdout, encoding="utf-8")
        headings_ok = all(f"## {sec}" in seeded.stdout for sec in S5_RULEBOOK_SECTIONS)
        if not s.check(
            "rulebook-seeded-prepromoted",
            seeded.rc == 0 and S8_PROMOTED_HEADING in seeded.stdout and S5_SEED_RULE in seeded.stdout and headings_ok,
            f"rc={seeded.rc}, promoted heading={S8_PROMOTED_HEADING in seeded.stdout}, "
            f"seed rule={S5_SEED_RULE in seeded.stdout}, all 5 headings={headings_ok}",
        ):
            _cne_rest(s, S8_ASSERTION_NAMES, "rulebook seeding failed")
            return _finish(s, started)

        # 2. Seed P1 (rule-covered) + P2 (control).
        seeded_p = s.ex.run(cmd_seed_packets(cfg, s.queue_dir, S8_SEED_PACKETS_SCRIPT), label="seed-packets")
        ids = [ln.strip() for ln in seeded_p.stdout.splitlines() if ln.strip().startswith("pkt-")]
        if not s.check(
            "packets-seeded",
            seeded_p.rc == 0 and len(ids) == 2 and ids[0] != ids[1],
            f"rc={seeded_p.rc}, ids={ids}, stderr={seeded_p.stderr.strip()[:200]!r}",
        ):
            _cne_rest(s, S8_ASSERTION_NAMES, "packet seeding failed")
            return _finish(s, started)
        p1, p2 = ids
        s.snapshot("post-seed", s.ex.run(cmd_queue_list(cfg, s.queue_dir), label="queue-list-post-seed"))

        # 3. One real-LLM triage pass over both packets.
        pass1 = _s5_triage_pass(s, cfg, home, bundle_uri, 1)
        s.check("triage-pass-ok", pass1.rc == 0, f"rc={pass1.rc}, stdout tail: {pass1.stdout.strip()[-300:]!r}")

        # 4. P1 auto-answered (all conservative bounds cleared).
        p1_answered = _read_packet_file(s, f"{s.queue_dir}/answered/{p1}.json", "p1-answered-read")
        actual_answer: str | None = None
        if p1_answered is None:
            # Honest bound note: high-confidence is required — capture the verdict.
            p1_pending = _read_packet_file(s, f"{s.queue_dir}/pending/{p1}.json", "p1-pending-read")
            if p1_pending is not None:
                (s.out_dir / "p1-pending.json").write_text(json.dumps(p1_pending, indent=2), encoding="utf-8")
                triage = p1_pending.get("triage") or {}
                rec = p1_pending.get("recommendation") or {}
                s.check(
                    "p1-auto-answered",
                    False,
                    f"P1 NOT auto-answered — still pending with triage={{handled_by: {triage.get('handled_by')!r}, "
                    f"why: {str(triage.get('why', ''))[:120]!r}, rule_refs: {triage.get('rule_refs')!r}}}, "
                    f"recommendation={{option: {rec.get('option')!r}, confidence: {rec.get('confidence')!r}}}. "
                    f"Auto-answer requires confidence==high + rule_refs resolving to phase-2 sections; if the "
                    f"model returned lower confidence, the bound legitimately blocked it — tune the scenario, "
                    f"not the code.",
                )
            else:
                locate = s.ex.run(cmd_queue_show(cfg, s.queue_dir, p1), label="p1-locate")
                s.check(
                    "p1-auto-answered",
                    False,
                    f"P1 in neither answered/ nor pending/ — queue show says: {locate.stdout.strip()[:300]!r}",
                )
        else:
            (s.out_dir / "p1-answered.json").write_text(json.dumps(p1_answered, indent=2), encoding="utf-8")
            resolution = p1_answered.get("resolution") or {}
            actual_answer = resolution.get("answer")
            pending_gone = s.ex.run(cmd_file_absent(f"{s.queue_dir}/pending/{p1}.json"), label="p1-pending-absent")
            s.check(
                "p1-auto-answered",
                resolution.get("answered_by") == "manager-auto"
                and actual_answer == S8_RULE_IMPLIED_OPTION
                and pending_gone.stdout.strip() == "absent",
                f"answered_by={resolution.get('answered_by')!r}, answer={actual_answer!r} "
                f"(rule-implied: {S8_RULE_IMPLIED_OPTION!r}), pending entry: {pending_gone.stdout.strip()}",
            )

        # 5. Auto review record (unreviewed) in queue/auto/.
        record = _read_packet_file(s, f"{s.queue_dir}/auto/{p1}.json", "p1-auto-record-read")
        if record is None:
            s.cne("p1-auto-record", f"auto/{p1}.json missing or unparseable")
        else:
            (s.out_dir / "p1-auto-record.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            s.check(
                "p1-auto-record",
                record.get("reviewed") is False
                and "Auto-answer rules" in (record.get("sections") or [])
                and record.get("answer") == (actual_answer or S8_RULE_IMPLIED_OPTION),
                f"reviewed={record.get('reviewed')}, sections={record.get('sections')}, "
                f"answer={record.get('answer')!r}",
            )

        # 6. Events + ledger.
        events, malformed = _fetch_events(s, home)
        auto_events = [e for e in _events_named(events, "triage:auto_answered") if e.get("packet_id") == p1]
        ledger_records, _ = _parse_jsonl(s.ex.run(cmd_ledger_cat(home), label="ledger-fetch-1").stdout)
        ledger_auto = [r for r in ledger_records if r.get("kind") == "triage_auto_answered"]
        s.check(
            "events-ledger-auto",
            len(auto_events) == 1 and len(ledger_auto) >= 1,
            f"triage:auto_answered(P1)={len(auto_events)}, ledger triage_auto_answered={len(ledger_auto)} "
            f"(malformed lines: {malformed})",
        )

        # 7. P2 stays Phase-1 recommend-only (the bounds hold in the control direction).
        p2_pending = _read_packet_file(s, f"{s.queue_dir}/pending/{p2}.json", "p2-pending-read")
        if p2_pending is None:
            locate = s.ex.run(cmd_queue_show(cfg, s.queue_dir, p2), label="p2-locate")
            s.check(
                "p2-not-auto-answered",
                False,
                f"P2 not in pending/ — queue show says: {locate.stdout.strip()[:300]!r} "
                "(auto-answered or bounced: either is a bounds failure for the control packet)",
            )
        else:
            (s.out_dir / "p2-pending.json").write_text(json.dumps(p2_pending, indent=2), encoding="utf-8")
            p2_triage = p2_pending.get("triage") or {}
            s.check(
                "p2-not-auto-answered",
                p2_triage.get("handled_by") == "manager-recommend",
                f"triage={{handled_by: {p2_triage.get('handled_by')!r}, why: {str(p2_triage.get('why', ''))[:120]!r}}}",
            )

        # 8. auto reject with the OTHER option — requires an auto-answer to review.
        if actual_answer is None or record is None:
            _cne_rest(s, S8_ASSERTION_NAMES, "no auto-answer to review (see p1-auto-answered)")
            return _finish(s, started)
        opposite = "A" if actual_answer == "B" else "B"
        reject = s.ex.run(cmd_auto_reject(cfg, s.queue_dir, home, p1, opposite), label="auto-reject")
        s.check(
            "auto-reject-accepted",
            reject.rc == 0,
            f"auto answer {actual_answer!r} -> correction {opposite!r} (rc={reject.rc}, "
            f"stderr={reject.stderr.strip()[:150]!r})",
        )

        # 9. Record reviewed with the correction.
        reviewed = _read_packet_file(s, f"{s.queue_dir}/auto/{p1}.json", "p1-auto-record-reviewed-read")
        if reviewed is None:
            s.cne("auto-record-reviewed", f"auto/{p1}.json missing after review")
        else:
            review = reviewed.get("review") or {}
            s.check(
                "auto-record-reviewed",
                reviewed.get("reviewed") is True
                and review.get("action") == "rejected"
                and review.get("correct_option") == opposite,
                f"reviewed={reviewed.get('reviewed')}, review={review}",
            )

        # 10. Section demoted, visibly, in the heading annotation.
        rulebook_after = s.ex.run(cmd_cat(f"{home}/rulebook.md"), label="rulebook-after-read")
        s.check(
            "section-demoted",
            S8_DEMOTED_HEADING in rulebook_after.stdout,
            f"{S8_DEMOTED_HEADING!r} present={S8_DEMOTED_HEADING in rulebook_after.stdout}",
        )

        # 11. trust:demoted event.
        events_after, _ = _fetch_events(s, home)
        demoted = [e for e in _events_named(events_after, "trust:demoted") if e.get("packet_id") == p1]
        s.check(
            "event-trust-demoted",
            len(demoted) == 1
            and demoted[0].get("section") == "Auto-answer rules"
            and demoted[0].get("from_phase") == 2,
            f"trust:demoted count={len(demoted)}, "
            f"details={[{k: e.get(k) for k in ('section', 'from_phase', 'phase', 'streak')} for e in demoted]}",
        )
        return _finish(s, started)
    finally:
        _collect_s5_artifacts(s, home)


# -- scenario 9: recipe-gate bridge (step 6) -------------------------------------------


def _parse_tool_invoke(stdout: str) -> dict[str, Any] | None:
    """Parse `amplifier tool invoke ... -o json` output into the tool's result dict.

    Mirrors recipe_gates.parse_invoke_output, hardened for noise lines that may
    themselves contain braces: candidate parse positions are lines starting
    with '{', tried LAST first (the envelope is printed last). The envelope's
    'result' field is the string repr of a Python dict (observed shape).
    """
    import ast

    lines = stdout.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("{")]
    for i in reversed(starts):
        try:
            envelope = json.loads("\n".join(lines[i:]))
        except json.JSONDecodeError:
            continue
        if not isinstance(envelope, dict) or envelope.get("status") != "success":
            continue
        result = envelope.get("result")
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                parsed = ast.literal_eval(result)
            except (ValueError, SyntaxError):
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def _s9_exec(s: Scenario, cmd: str, label: str, artifact: str) -> ExecResult:
    result = s.ex.run(cmd, label=label, timeout=min(S9_INVOKE_TIMEOUT_S, max(30.0, s.remaining())))
    (s.out_dir / artifact).write_text(result.stdout + result.stderr, encoding="utf-8")
    return result


def _collect_s9_artifacts(s: Scenario, home: str) -> None:
    resume_logs_cmd = (
        f"for f in {shlex.quote(home + '/recipe-gates')}/*.resume.log; do "
        f'echo "=== $f ==="; cat "$f"; echo; done 2>/dev/null || true'
    )
    artifacts = [
        ("events", cmd_cat(f"{home}/events.jsonl"), "events.jsonl"),
        ("ledger", cmd_ledger_cat(home), "ledger.jsonl"),
        ("gates-state", cmd_cat(f"{home}/recipe-gates.json"), "recipe-gates.json"),
        ("resume-logs", resume_logs_cmd, "resume.log"),
    ]
    for label, cmd, filename in artifacts:
        try:
            result = s.ex.run(cmd, label=f"artifact-{label}")
            if result.rc == 0:
                (s.out_dir / filename).write_text(result.stdout, encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — artifact collection must never mask the verdict
            print(f"    (artifact {label} not collected: {e})")


def run_scenario_9(cfg: Config) -> ScenarioResult:
    spec = SPECS[9]
    s = Scenario(cfg, spec)
    started = time.monotonic()
    home = f"{s.dtu_sdir}/home"
    workdir = f"{s.dtu_sdir}/work"
    recipe_path = f"{cfg.repo_dir}/{spec.bundle_rel}"
    try:
        # 1. Execute the staged recipe (synchronous; returns paused_for_approval).
        execute = _s9_exec(
            s,
            cmd_recipes_invoke(
                s.queue_dir,
                home,
                workdir,
                f"operation=execute recipe_path={shlex.quote(recipe_path)}",
                f"{s.dtu_sdir}/execute.out",
            ),
            "recipes-execute",
            "execute.out",
        )
        result = _parse_tool_invoke(execute.stdout) if execute.rc == 0 else None
        session_id = str(result.get("session_id", "")) if result else ""
        if result is None or result.get("status") != "paused_for_approval" or not session_id:
            if any(m in execute.stdout + execute.stderr for m in S9_TOOL_MISSING_MARKERS):
                s.cne(
                    "recipe-executed-paused",
                    f"the recipes tool is NOT available in this DTU's default bundle — honest env failure "
                    f"(rc={execute.rc}). Captured output tail: {(execute.stdout + execute.stderr)[-400:]!r}",
                )
            else:
                s.check(
                    "recipe-executed-paused",
                    False,
                    f"rc={execute.rc}, parsed={result!r} (expected status=paused_for_approval with a session_id); "
                    f"output tail: {execute.stdout.strip()[-300:]!r}",
                )
            _cne_rest(s, S9_ASSERTION_NAMES, "recipe never reached the approval gate")
            return _finish(s, started)
        s.check("recipe-executed-paused", True, f"status=paused_for_approval, session_id={session_id}")

        # 2. Poll #1: packetize the gate.
        _s9_exec(
            s, cmd_recipes_poll(cfg, s.queue_dir, home, workdir, f"{s.dtu_sdir}/poll-1.out"), "poll-1", "poll-1.out"
        )
        listing = s.ex.run(cmd_queue_list(cfg, s.queue_dir), label="queue-list-post-poll1")
        s.snapshot("post-poll1", listing)
        gate_packets: list[dict[str, Any]] = []
        if listing.rc == 0:
            try:
                gate_packets = [
                    p
                    for p in json.loads(listing.stdout)
                    if isinstance(p, dict) and (p.get("source") or {}).get("kind") == spec.kind
                ]
            except json.JSONDecodeError:
                gate_packets = []
        if len(gate_packets) != 1:
            s.check(
                "gate-packetized", False, f"expected exactly 1 pending kind={spec.kind} packet, got {len(gate_packets)}"
            )
            _cne_rest(s, S9_ASSERTION_NAMES, "gate never packetized")
            return _finish(s, started)
        packet = gate_packets[0]
        packet_id = str(packet.get("id", ""))
        (s.out_dir / "packet-pending.json").write_text(json.dumps(packet, indent=2), encoding="utf-8")
        option_ids = [o.get("id") for o in packet.get("options", []) if isinstance(o, dict)]
        s.check(
            "gate-packetized",
            option_ids == ["approve", "deny"]
            and (packet.get("source") or {}).get("work_unit") == session_id
            and f"stage: {S9_STAGE_NAME}" in str(packet.get("context", "")),
            f"packet {packet_id}: options={option_ids}, work_unit={(packet.get('source') or {}).get('work_unit')!r}, "
            f"context={str(packet.get('context', ''))[:100]!r}",
        )

        # 3. recipe_gates:packetized event.
        events, _ = _fetch_events(s, home)
        packetized = [e for e in _events_named(events, "recipe_gates:packetized") if e.get("packet_id") == packet_id]
        s.check(
            "events-packetized",
            len(packetized) == 1
            and packetized[0].get("session_id") == session_id
            and packetized[0].get("stage_name") == S9_STAGE_NAME,
            f"count={len(packetized)}, details={packetized}",
        )

        # 4. Answer approve.
        answer = s.answer(packet_id, spec.answer_option)
        s.check("answer-approve-accepted", answer.rc == 0, f"rc={answer.rc} stderr={answer.stderr.strip()[:150]!r}")

        # 5. Poll #2: forward the approve to the recipes tool (+ auto-resume launch).
        poll2 = _s9_exec(
            s, cmd_recipes_poll(cfg, s.queue_dir, home, workdir, f"{s.dtu_sdir}/poll-2.out"), "poll-2", "poll-2.out"
        )
        events2, _ = _fetch_events(s, home)
        resolved = [e for e in _events_named(events2, "recipe_gates:resolved") if e.get("packet_id") == packet_id]
        s.check(
            "forward-approve",
            poll2.rc == 0 and len(resolved) == 1 and resolved[0].get("answer") == "approve",
            f"poll2 rc={poll2.rc}, recipe_gates:resolved count={len(resolved)}, "
            f"details={[{k: e.get(k) for k in ('answer', 'session_id', 'stage_name')} for e in resolved]}",
        )

        # 6. Auto-resume launched by the poller (step-6 decision: _launch_resume —
        #    detached background subprocess; event + ledger + log are its only surfaces).
        resume_log_path = f"{home}/recipe-gates/{session_id}.resume.log"
        launched = [
            e for e in _events_named(events2, "recipe_gates:resume_launched") if e.get("session_id") == session_id
        ]
        ledger_records6, _ = _parse_jsonl(s.ex.run(cmd_ledger_cat(home), label="ledger-fetch-resume").stdout)
        ledger_launched = [r for r in ledger_records6 if r.get("kind") == "recipe_gate_resume_launched"]
        log_exists = s.ex.run(cmd_file_exists(resume_log_path), label="resume-log-exists")
        s.check(
            "resume-launched",
            len(launched) == 1 and len(ledger_launched) == 1 and log_exists.stdout.strip() == "yes",
            f"recipe_gates:resume_launched events={len(launched)}, "
            f"ledger recipe_gate_resume_launched={len(ledger_launched)}, "
            f"resume log exists: {log_exists.stdout.strip()} ({resume_log_path})",
        )

        # 7. Recipe completes — poll operation=list until the after-gate step shows
        #    in completed_steps (verified surface: list has NO status field; for this
        #    recipe, completion == "step-two" in completed_steps).
        def poll_completed() -> list[str] | None:
            listing = s.ex.run(
                cmd_recipes_invoke(s.queue_dir, home, workdir, "operation=list", f"{s.dtu_sdir}/list-final.out"),
                label="recipes-list-poll",
                timeout=min(S9_INVOKE_TIMEOUT_S, max(30.0, s.remaining())),
            )
            if listing.rc != 0:
                return None
            payload = _parse_tool_invoke(listing.stdout)
            if not payload:
                return None
            for sess in payload.get("sessions") or []:
                if isinstance(sess, dict) and sess.get("session_id") == session_id:
                    steps = [str(x) for x in (sess.get("completed_steps") or [])]
                    (s.out_dir / "list-final.out").write_text(listing.stdout, encoding="utf-8")
                    return steps if S9_FINAL_STEP in steps else None
            return None

        completed_steps = _poll(s, S9_COMPLETE_WAIT_S, poll_completed)
        resume_log = s.ex.run(cmd_cat(resume_log_path), label="resume-log-fetch")
        if resume_log.rc == 0:
            (s.out_dir / "resume.log").write_text(resume_log.stdout, encoding="utf-8")
        if completed_steps is None:
            s.check(
                "recipe-completes",
                False,
                f"session {session_id} never showed {S9_FINAL_STEP!r} in completed_steps within "
                f"{S9_COMPLETE_WAIT_S:.0f}s; resume log tail: {resume_log.stdout.strip()[-400:]!r}",
            )
        else:
            s.check(
                "recipe-completes",
                True,
                f"completed_steps={completed_steps}; resume log tail: {resume_log.stdout.strip()[-200:]!r}",
            )

        # 8. Poll #3: dedupe — no re-packetize AND no second resume launch
        #    (resume_launched_at idempotency: event count stays exactly 1).
        _s9_exec(
            s, cmd_recipes_poll(cfg, s.queue_dir, home, workdir, f"{s.dtu_sdir}/poll-3.out"), "poll-3", "poll-3.out"
        )
        listing3 = s.ex.run(cmd_queue_list(cfg, s.queue_dir), label="queue-list-post-poll3")
        s.snapshot("post-poll3", listing3)
        remaining_gates = 0
        if listing3.rc == 0:
            try:
                remaining_gates = sum(
                    1
                    for p in json.loads(listing3.stdout)
                    if isinstance(p, dict) and (p.get("source") or {}).get("kind") == spec.kind
                )
            except json.JSONDecodeError:
                remaining_gates = -1
        events3, _ = _fetch_events(s, home)
        launched_after = [
            e for e in _events_named(events3, "recipe_gates:resume_launched") if e.get("session_id") == session_id
        ]
        s.check(
            "dedupe-no-second-packet-or-resume",
            remaining_gates == 0 and len(launched_after) == 1,
            f"pending kind={spec.kind} packets after poll #3: {remaining_gates}; "
            f"recipe_gates:resume_launched count: {len(launched_after)} "
            f"(must stay 1 — resume_launched_at idempotency)",
        )

        # 9. Ledger.
        ledger_records, bad = _parse_jsonl(s.ex.run(cmd_ledger_cat(home), label="ledger-fetch").stdout)
        kinds: dict[str, int] = {}
        for r in ledger_records:
            kind = str(r.get("kind"))
            kinds[kind] = kinds.get(kind, 0) + 1
        s.check(
            "ledger-recipe-gates",
            kinds.get("recipe_gate_packetized", 0) == 1
            and kinds.get("recipe_gate_resolved", 0) == 1
            and kinds.get("recipe_gate_resume_launched", 0) == 1,
            f"kinds={kinds}, malformed={bad}",
        )
        return _finish(s, started)
    finally:
        _collect_s9_artifacts(s, home)


def _grade_decision_schema(show: ExecResult) -> tuple[bool, str]:
    if show.rc != 0:
        return False, f"queue show rc={show.rc} stderr={show.stderr.strip()!r}"
    try:
        packet = json.loads(show.stdout)
    except json.JSONDecodeError as e:
        return False, f"queue show output not JSON: {e}"
    option_ids = [o.get("id") for o in packet.get("options", []) if isinstance(o, dict)]
    checks = {
        "id-prefix": str(packet.get("id", "")).startswith("pkt-"),
        "schema-version-1": packet.get("schema_version") == 1,
        "question-non-empty": bool(str(packet.get("question", "")).strip()),
        "two-options-A-B": len(option_ids) == 2 and set(option_ids) == {"A", "B"},
    }
    failed = [k for k, ok in checks.items() if not ok]
    return (not failed), (f"failed sub-checks: {failed}" if failed else f"all sub-checks passed; options={option_ids}")


def _finish(s: Scenario, started: float) -> ScenarioResult:
    result = ScenarioResult(scenario=s.spec.slug, assertions=s.assertions, duration_s=time.monotonic() - started)
    (s.out_dir / "grader.json").write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    return result


RUNNERS = {
    1: run_scenario_1,
    2: run_scenario_2,
    3: run_scenario_3,
    4: run_scenario_4,
    5: run_scenario_5,
    6: run_scenario_6,
    7: run_scenario_7,
    8: run_scenario_8,
    9: run_scenario_9,
}


# -- dry-run planner --------------------------------------------------------------------


def _plan_scenario_4(cfg: Config, dtu_sdir: str, queue_dir: str) -> list[tuple[str, str]]:
    home = f"{dtu_sdir}/home"
    notify_path = f"{dtu_sdir}/notify.jsonl"
    sup_log = f"{dtu_sdir}/supervisor.log"
    bundle_uri = f"file://{cfg.repo_dir}/{SPECS[4].bundle_rel}"
    pkt_w1, pkt_w2, sup_pid = "<W1_PACKET_ID>", "<W2_PACKET_ID>", "<SUP_PID>"
    steps: list[tuple[str, str]] = []
    for w in S4_WORKERS.values():
        steps.append((f"pre-clean leftover {w['session']} session (best-effort)", cmd_tmux_kill(str(w["session"]))))
    sup_pgid_file = f"{dtu_sdir}/supervisor.pgid"
    steps.append(
        (
            (
                "start supervisor in background (own process group; stdout+stderr APPEND to supervisor.log; "
                "the setsid'd session writes its REAL pgid to supervisor.pgid — `$!` is only the wrapping subshell)"
            ),
            cmd_supervise_launch(cfg, queue_dir, home, dtu_sdir, notify_path, sup_log, sup_pgid_file),
        )
    )
    steps.append(("read the supervisor's real pgid (poll until non-empty)", cmd_read_pgid(sup_pgid_file)))
    for name, w in S4_WORKERS.items():
        steps.append(
            (
                f"dispatch worker {name} into tmux session {w['session']}",
                cmd_dispatch(cfg, queue_dir, home, name, str(w["prompt"]), bundle_uri),
            )
        )
    for w in S4_WORKERS.values():
        steps.append((f"assert tmux session {w['session']} exists", cmd_tmux_has(str(w["session"]))))
    steps += [
        (
            f"poll every {POLL_INTERVAL_S:.0f}s (<= {PACKET_WAIT_S:.0f}s) until exactly 2 kind=decision packets appear",
            cmd_queue_list(cfg, queue_dir),
        ),
        (
            "poll events.jsonl until exactly 2 packet:created events",
            cmd_cat(f"{home}/events.jsonl"),
        ),
        (
            (
                f"poll notify sink (<= {S4_NOTIFY_WAIT_S:.0f}s): every created packet id covered by well-formed "
                "batch records (soft detail: ONE batch covering both)"
            ),
            cmd_cat(notify_path),
        ),
        ("SIGKILL the supervisor process group mid-run (pgid from supervisor.pgid)", cmd_kill_group(sup_pid)),
        (
            (
                "VERIFY the supervisor is dead (a surviving one would run concurrently with the restart "
                "and duplicate events — hard FAIL)"
            ),
            cmd_worker_alive(sup_pid),
        ),
        (
            "restart supervisor with the same flags (new pgid written to supervisor.pgid)",
            cmd_supervise_launch(cfg, queue_dir, home, dtu_sdir, notify_path, sup_log, sup_pgid_file),
        ),
        (
            f"wait ~2 ticks ({S4_RESTART_SETTLE_S:.0f}s), then assert packet:created count is STILL exactly 2 (D5)",
            cmd_cat(f"{home}/events.jsonl"),
        ),
        (
            "map packets to workers via [w1]/[w2] tags (keyword fallback), then answer w1 -> A",
            cmd_answer(cfg, queue_dir, pkt_w1, "A", "eval"),
        ),
        ("answer w2 -> B", cmd_answer(cfg, queue_dir, pkt_w2, "B", "eval")),
        (
            f"poll (<= {S4_WORKER_WAIT_S:.0f}s) am-w1 worker.log for 'DECISION RECEIVED: A' (ANSI-stripped)",
            cmd_cat(f"{home}/workers/am-w1/worker.log"),
        ),
        (
            f"poll (<= {S4_WORKER_WAIT_S:.0f}s) am-w2 worker.log for 'DECISION RECEIVED: B' (ANSI-stripped)",
            cmd_cat(f"{home}/workers/am-w2/worker.log"),
        ),
        (
            "poll events for 2 packet:answered (non-null latency_s) + 2 worker:finished (judged:false, exit_code 0)",
            cmd_cat(f"{home}/events.jsonl"),
        ),
        (
            "verify ledger: dispatched x2, packet_created x2, packet_answered x2, worker_finished x2, notified_batch >=1",
            cmd_ledger_cat(home),
        ),
        ("cleanup: kill supervisor process group(s)", cmd_kill_group(sup_pid)),
    ]
    for w in S4_WORKERS.values():
        steps.append((f"cleanup: kill tmux session {w['session']}", cmd_tmux_kill(str(w["session"]))))
    return steps


def _plan_scenario_5(cfg: Config, dtu_sdir: str, queue_dir: str) -> list[tuple[str, str]]:
    home = f"{dtu_sdir}/home"
    bundle_uri = f"file://{cfg.repo_dir}/{SPECS[5].bundle_rel}"
    p1, prop = "<P1_PACKET_ID>", "<PROPOSAL_ID>"
    return [
        (
            "seed rulebook: 5-section template + seed rule under '## Auto-answer rules' (echoed back for grading)",
            cmd_seed_rulebook(home),
        ),
        (
            "seed P1 (cold-decidable, no producer recommendation) + P2 (cold-undecidable) via the ROOT queue lib; prints both ids",
            cmd_seed_packets(cfg, queue_dir),
        ),
        (
            (
                f"pass 1: real-LLM cold triage on both packets (synchronous; per-session --timeout {S5_SESSION_TIMEOUT_S:g}s; "
                f"exec budget {S5_TRIAGE_EXEC_TIMEOUT_S:.0f}s)"
            ),
            cmd_triage_once(cfg, queue_dir, home, bundle_uri, f"{dtu_sdir}/triage-pass-1.out"),
        ),
        (
            (
                "grade P1: still pending, triage.handled_by=manager-recommend, why non-empty, rule_refs list, "
                "recommendation.option in {A,B}"
            ),
            cmd_cat(f"{queue_dir}/pending/{p1}.json"),
        ),
        (
            "grade P2: in bounced/ with non-empty 'bounce:' reason in triage.why (+ pending entry gone)",
            cmd_cat(f"{queue_dir}/bounced/<P2_PACKET_ID>.json"),
        ),
        ("grade events: exactly 1 triage:recommended(P1) + 1 triage:bounced(P2)", cmd_cat(f"{home}/events.jsonl")),
        ("grade ledger: triage_recommended x1 + triage_bounced x1", cmd_ledger_cat(home)),
        (
            "answer P1 with the OPPOSITE of triage's recommendation (grader records which way)",
            cmd_answer(cfg, queue_dir, p1, "<OPPOSITE_OPTION>", "eval"),
        ),
        (
            "pass 2: rule_delta for the answered P1",
            cmd_triage_once(cfg, queue_dir, home, bundle_uri, f"{dtu_sdir}/triage-pass-2.out"),
        ),
        (
            (
                "grade proposals: exactly ONE record for P1 — proposal {sentence, section in the 5} OR explicit "
                "none {reason}; branch recorded"
            ),
            cmd_cat(f"{home}/rulebook-proposals.jsonl"),
        ),
        (
            "pass 3: idempotency — P1's record count must be unchanged",
            cmd_triage_once(cfg, queue_dir, home, bundle_uri, f"{dtu_sdir}/triage-pass-3.out"),
        ),
        ("re-read proposals for the idempotency count", cmd_cat(f"{home}/rulebook-proposals.jsonl")),
        (
            "apply branch (proposal only): apply the rule, then assert the sentence sits under the target section",
            cmd_rulebook_apply(cfg, queue_dir, home, prop),
        ),
        ("read rulebook after apply (section-scoped sentence match)", cmd_cat(f"{home}/rulebook.md")),
    ]


def _plan_scenario_6(cfg: Config, dtu_sdir: str, queue_dir: str) -> list[tuple[str, str]]:
    home = f"{dtu_sdir}/home"
    notify_path = f"{dtu_sdir}/notify.jsonl"
    sup_log = f"{dtu_sdir}/supervisor.log"
    sup_pgid_file = f"{dtu_sdir}/supervisor.pgid"
    good_art = f"{dtu_sdir}/good-artifact.txt"
    broken_art = f"{dtu_sdir}/broken-artifact.txt"
    sup_pid = "<SUP_PGID>"
    steps: list[tuple[str, str]] = []
    for session in S6_SESSIONS:
        steps.append((f"pre-clean leftover {session} session (best-effort)", cmd_tmux_kill(session)))
    steps += [
        ("seed judge-verify artifacts: good (marker) + broken (no marker)", cmd_make_verify_artifacts(dtu_sdir)),
        (
            "judge verify: working grep-judge must PASS both directions (exit 0 + 'VERDICT: PASS')",
            cmd_judge_verify(cfg, queue_dir, home, S6_VERIFY_JUDGE_CMD, good_art, broken_art),
        ),
        (
            "judge verify: decoration judge ('true', never fails) must be REJECTED (nonzero exit)",
            cmd_judge_verify(cfg, queue_dir, home, "true", good_art, broken_art),
        ),
        (
            f"start supervisor (batch-window {S6_BATCH_WINDOW_S:g}s; pgid written to supervisor.pgid)",
            cmd_supervise_launch(
                cfg,
                queue_dir,
                home,
                dtu_sdir,
                notify_path,
                sup_log,
                sup_pgid_file,
                batch_window_s=S6_BATCH_WINDOW_S,
            ),
        ),
        ("read the supervisor's real pgid (poll until non-empty)", cmd_read_pgid(sup_pgid_file)),
        (
            (
                "dispatch G: writes artifact WITH marker into its worker dir, exits 0; grep-judge on RELATIVE "
                "artifact.txt (judge-cwd contract)"
            ),
            cmd_dispatch_fake(
                cfg,
                queue_dir,
                home,
                "g",
                "finish-line g worker (fake)",
                f"echo {S6_MARKER} > {home}/workers/am-g/artifact.txt",
                S6_WORKER_JUDGE_CMD,
            ),
        ),
        (
            "dispatch B: writes artifact WITHOUT marker, exits 0; SAME judge",
            cmd_dispatch_fake(
                cfg,
                queue_dir,
                home,
                "b",
                "finish-line b worker (fake)",
                f"echo marker deliberately omitted > {home}/workers/am-b/artifact.txt",
                S6_WORKER_JUDGE_CMD,
            ),
        ),
        (
            "dispatch U: echo unjudged, exits 0; NO judge",
            cmd_dispatch_fake(cfg, queue_dir, home, "u", "finish-line u worker (fake)", "echo unjudged", None),
        ),
        (
            (
                f"poll every {POLL_INTERVAL_S:.0f}s (<= {S6_FINISH_WAIT_S:.0f}s) until 3 worker:finished events; then grade "
                "loop:closed(am-g, judge_output) / loop:failed(am-b, reason) / judged fields / no loop:* for am-u"
            ),
            cmd_cat(f"{home}/events.jsonl"),
        ),
        ("grade judge.log for am-g exists non-empty", cmd_cat(f"{home}/workers/am-g/judge.log")),
        (
            f"poll notify sink (<= {S6_NOTIFY_WAIT_S:.0f}s) for finish_line(am-g) + finish_line_failed(am-b) items",
            cmd_cat(notify_path),
        ),
        (
            "grade ledger counts: loop_closed x1, loop_failed x1, worker_finished x3, dispatched x3",
            cmd_ledger_cat(home),
        ),
        (
            "ledger --summary --json renders: am-g under loops_closed, am-b under loops_failed",
            cmd_ledger_summary(cfg, queue_dir, home),
        ),
        ("cleanup: kill supervisor process group", cmd_kill_group(sup_pid)),
    ]
    for session in S6_SESSIONS:
        steps.append((f"cleanup: kill tmux session {session}", cmd_tmux_kill(session)))
    return steps


def _plan_scenario_7(cfg: Config, dtu_sdir: str, queue_dir: str) -> list[tuple[str, str]]:
    home = f"{dtu_sdir}/home"
    workdir = f"{dtu_sdir}/work"
    pipeline = f"{cfg.repo_dir}/{SPECS[7].bundle_rel}"
    pkt, wu_pgid = "<PACKET_ID>", "<WU_PGID>"
    return [
        (
            (
                "launch workunit in background (own process group; cwd = workdir so tool nodes write relative "
                "A.txt/R.txt there; stdout+stderr -> wu.log; exit -> wu.exit; pgid -> wu.pgid)"
            ),
            cmd_workunit_launch(cfg, queue_dir, home, dtu_sdir, workdir, pipeline, S7_NAME),
        ),
        ("read the workunit's real pgid (poll until non-empty)", cmd_read_pgid(f"{dtu_sdir}/wu.pgid")),
        (
            (
                f"poll every {POLL_INTERVAL_S:.0f}s (<= {S7_GATE_WAIT_S:.0f}s) for a kind=attractor-gate packet, "
                "checking wu.exit each iteration (early death + missing-[attractor]-extra marker in wu.log => "
                "assertion 1 could-not-evaluate with the error text captured — honest env failure)"
            ),
            cmd_queue_list(cfg, queue_dir),
        ),
        (
            (
                f"grade packet shape: question == {S7_QUESTION!r}, option ids exactly [A, R] with labels containing "
                f"Approve/Reject, source.work_unit == {S7_NAME!r}, 'stage: gate' in context"
            ),
            cmd_queue_show(cfg, queue_dir, pkt),
        ),
        (
            f"grade events (<= {S7_EVENT_WAIT_S:.0f}s): gate:packet_created with work_unit + packet_id",
            cmd_cat(f"{home}/events.jsonl"),
        ),
        ("answer A via the CLI", cmd_answer(cfg, queue_dir, pkt, "A", "eval")),
        (
            f"poll (<= {S7_COMPLETE_WAIT_S:.0f}s) for workunit completion; grade exit 0",
            cmd_file_exists(f"{dtu_sdir}/wu.exit"),
        ),
        ("grade route: A.txt exists with content 'A'", cmd_cat(f"{workdir}/A.txt")),
        ("grade route: R.txt does NOT exist", cmd_file_absent(f"{workdir}/R.txt")),
        (
            "grade events: gate:answered {answer: A} + workunit:finished {name: wu-eval, status: success}",
            cmd_cat(f"{home}/events.jsonl"),
        ),
        ("grade ledger: one workunit_finished {name: wu-eval, status: success}", cmd_ledger_cat(home)),
        ("cleanup: kill workunit process group (best-effort)", cmd_kill_group(wu_pgid)),
    ]


def _plan_scenario_8(cfg: Config, dtu_sdir: str, queue_dir: str) -> list[tuple[str, str]]:
    home = f"{dtu_sdir}/home"
    bundle_uri = f"file://{cfg.repo_dir}/{SPECS[8].bundle_rel}"
    p1 = "<P1_PACKET_ID>"
    return [
        (
            f"seed rulebook with '## Auto-answer rules' PRE-PROMOTED ({S8_PROMOTED_HEADING!r}) + the seed rule",
            cmd_seed_rulebook(home, S8_RULEBOOK_CONTENT),
        ),
        (
            "seed P1 (rule-covered rollout decision) + P2 (control: decidable but NOT rule-covered); prints both ids",
            cmd_seed_packets(cfg, queue_dir, S8_SEED_PACKETS_SCRIPT),
        ),
        (
            "one real-LLM triage pass over both packets",
            cmd_triage_once(cfg, queue_dir, home, bundle_uri, f"{dtu_sdir}/triage-pass-1.out"),
        ),
        (
            (
                "grade P1 AUTO-ANSWERED: answered/<P1>.json with resolution.answered_by=manager-auto, "
                f"answer == rule-implied {S8_RULE_IMPLIED_OPTION!r} (honest bound: confidence must be high — "
                "a medium verdict blocks auto-answer and is graded FAIL with the verdict captured)"
            ),
            cmd_cat(f"{queue_dir}/answered/{p1}.json"),
        ),
        (
            "grade auto review record: queue/auto/<P1>.json {reviewed: false, sections includes Auto-answer rules}",
            cmd_cat(f"{queue_dir}/auto/{p1}.json"),
        ),
        ("grade events: 1 triage:auto_answered(P1); ledger: triage_auto_answered", cmd_cat(f"{home}/events.jsonl")),
        (
            "grade P2 NOT auto-answered: still pending with triage.handled_by=manager-recommend",
            cmd_cat(f"{queue_dir}/pending/<P2_PACKET_ID>.json"),
        ),
        (
            "auto reject P1 with the OTHER option (opposite of the actual auto answer)",
            cmd_auto_reject(cfg, queue_dir, home, p1, "<OPPOSITE_OPTION>"),
        ),
        (
            "grade record reviewed: {reviewed: true, review.action: rejected, correct_option recorded}",
            cmd_cat(f"{queue_dir}/auto/{p1}.json"),
        ),
        (
            f"grade demotion: rulebook.md contains {S8_DEMOTED_HEADING!r}",
            cmd_cat(f"{home}/rulebook.md"),
        ),
        (
            "grade events: exactly 1 trust:demoted (section Auto-answer rules, from_phase 2)",
            cmd_cat(f"{home}/events.jsonl"),
        ),
    ]


def _plan_scenario_9(cfg: Config, dtu_sdir: str, queue_dir: str) -> list[tuple[str, str]]:
    home = f"{dtu_sdir}/home"
    workdir = f"{dtu_sdir}/work"
    recipe_path = f"{cfg.repo_dir}/{SPECS[9].bundle_rel}"
    pkt, sid = "<PACKET_ID>", "<SESSION_ID>"
    return [
        (
            (
                "execute the staged recipe (SYNCHRONOUS — returns status=paused_for_approval + session_id; "
                "recipes tool unavailable => assertion 1 could-not-evaluate with output captured). "
                "cwd = workdir: recipe sessions are project-scoped by working directory"
            ),
            cmd_recipes_invoke(
                queue_dir,
                home,
                workdir,
                f"operation=execute recipe_path={shlex.quote(recipe_path)}",
                f"{dtu_sdir}/execute.out",
            ),
        ),
        (
            "poll #1 (packetize): the pending gate becomes ONE kind=recipe-gate packet",
            cmd_recipes_poll(cfg, queue_dir, home, workdir, f"{dtu_sdir}/poll-1.out"),
        ),
        (
            (
                "grade the packet: options exactly [approve, deny], source.work_unit == session_id, "
                f"'stage: {S9_STAGE_NAME}' in context; + recipe_gates:packetized event"
            ),
            cmd_queue_list(cfg, queue_dir),
        ),
        ("answer approve via the CLI", cmd_answer(cfg, queue_dir, pkt, "approve", "eval")),
        (
            (
                "poll #2 (forward + AUTO-RESUME): operation=approve is forwarded with the rationale as message "
                "(recipe_gates:resolved), then the poller launches operation=resume as a DETACHED background "
                "subprocess (recipe_gates:resume_launched + ledger + resume log; idempotent via resume_launched_at)"
            ),
            cmd_recipes_poll(cfg, queue_dir, home, workdir, f"{dtu_sdir}/poll-2.out"),
        ),
        (
            "grade resume-launched: 1 recipe_gates:resume_launched event + ledger entry + resume log file exists",
            cmd_file_exists(f"{home}/recipe-gates/{sid}.resume.log"),
        ),
        (
            (
                f"poll operation=list (<= {S9_COMPLETE_WAIT_S:.0f}s) until the session's completed_steps includes "
                f"{S9_FINAL_STEP!r} (verified surface: list has NO status field); capture the resume log tail"
            ),
            cmd_recipes_invoke(queue_dir, home, workdir, "operation=list", f"{dtu_sdir}/list-final.out"),
        ),
        (
            (
                "poll #3 (dedupe): no second recipe-gate packet AND recipe_gates:resume_launched count stays "
                "exactly 1 (resume_launched_at idempotency)"
            ),
            cmd_recipes_poll(cfg, queue_dir, home, workdir, f"{dtu_sdir}/poll-3.out"),
        ),
        (
            "grade ledger: recipe_gate_packetized x1 + recipe_gate_resolved x1 + recipe_gate_resume_launched x1",
            cmd_ledger_cat(home),
        ),
    ]


def plan_scenario(cfg: Config, spec: ScenarioSpec) -> list[tuple[str, str]]:
    dtu_sdir = f"{cfg.work_dir}/{cfg.run_id}/{spec.slug}"
    queue_dir = f"{dtu_sdir}/queue"
    bundle = f"{cfg.repo_dir}/{spec.bundle_rel}"
    pkt, pid = "<PACKET_ID>", "<PID>"
    if spec.number == 4:
        return _plan_scenario_4(cfg, dtu_sdir, queue_dir)
    if spec.number == 5:
        return _plan_scenario_5(cfg, dtu_sdir, queue_dir)
    if spec.number == 6:
        return _plan_scenario_6(cfg, dtu_sdir, queue_dir)
    if spec.number == 7:
        return _plan_scenario_7(cfg, dtu_sdir, queue_dir)
    if spec.number == 8:
        return _plan_scenario_8(cfg, dtu_sdir, queue_dir)
    if spec.number == 9:
        return _plan_scenario_9(cfg, dtu_sdir, queue_dir)
    steps: list[tuple[str, str]] = [
        (
            (
                "launch worker in background (own process group; stdout+stderr -> worker.log; "
                "exit code -> worker.exit; prints PID)"
            ),
            cmd_launch_worker(cfg, queue_dir, dtu_sdir, bundle, spec.prompt),
        ),
        (
            f"poll every {POLL_INTERVAL_S:.0f}s (<= {PACKET_WAIT_S:.0f}s) until a kind={spec.kind} packet appears",
            cmd_queue_list(cfg, queue_dir),
        ),
    ]
    if spec.number == 1:
        steps += [
            ("read packet through the queue lib and grade schema", cmd_queue_show(cfg, queue_dir, pkt)),
            (f"answer option {spec.answer_option}", cmd_answer(cfg, queue_dir, pkt, spec.answer_option, "eval")),
            ("poll for worker completion", cmd_file_exists(f"{dtu_sdir}/worker.exit")),
            ("read worker exit code", cmd_cat(f"{dtu_sdir}/worker.exit")),
            ("fetch worker output (grade: contains 'DECISION RECEIVED: B')", cmd_cat(f"{dtu_sdir}/worker.log")),
            ("verify answered/ is authoritative (resolution fields)", cmd_cat(f"{queue_dir}/answered/{pkt}.json")),
            ("verify pending/ entry removed", cmd_file_absent(f"{queue_dir}/pending/{pkt}.json")),
        ]
    elif spec.number == 2:
        steps += [
            ("grade options == exactly {allow, deny} (from the listed packet)", cmd_queue_show(cfg, queue_dir, pkt)),
            ("answer allow", cmd_answer(cfg, queue_dir, pkt, "allow", "eval")),
            ("poll for worker completion", cmd_file_exists(f"{dtu_sdir}/worker.exit")),
            ("read worker exit code", cmd_cat(f"{dtu_sdir}/worker.exit")),
            ("fetch worker output (grade: contains 'eval-gate-ok')", cmd_cat(f"{dtu_sdir}/worker.log")),
            ("verify answered/ resolution answer == allow", cmd_cat(f"{queue_dir}/answered/{pkt}.json")),
        ]
    else:
        steps += [
            ("SIGKILL the worker's whole process group mid-block", cmd_kill_group(pid)),
            ("confirm the worker is dead", cmd_worker_alive(pid)),
            (
                f"wait {POST_KILL_SETTLE_S:.0f}s, then assert packet STILL pending (list + file)",
                cmd_queue_list(cfg, queue_dir),
            ),
            ("assert pending file still exists", cmd_file_exists(f"{queue_dir}/pending/{pkt}.json")),
            (
                f"answer option {spec.answer_option} with no producer alive",
                cmd_answer(cfg, queue_dir, pkt, spec.answer_option, "eval"),
            ),
            (
                "verify answered/ intact + re-entry data present (links.resume OR source.session_id)",
                cmd_cat(f"{queue_dir}/answered/{pkt}.json"),
            ),
        ]
    steps.append(("cleanup: kill worker process group (best-effort)", cmd_kill_group(pid)))
    return steps


def print_dry_run(cfg: Config, scenario_numbers: list[int]) -> None:
    prefix = cfg.dtu_exec or "<DTU_EXEC_PREFIX>"
    print("DRY RUN — planned in-DTU command sequences (no DTU contacted).")
    print(f"Every command below is dispatched as: {prefix} -- bash -c '<command>'")
    print(f"run id: {cfg.run_id} | repo dir: {cfg.repo_dir} | am CLI: {cfg.am_cli!r}")
    print(f"packet wait budget: {PACKET_WAIT_S:.0f}s\n")
    for n in scenario_numbers:
        spec = SPECS[n]
        print(f"=== {spec.title} ({spec.slug}) [hard timeout: {spec.timeout_s:.0f}s] ===")
        for i, (desc, cmd) in enumerate(plan_scenario(cfg, spec), 1):
            print(f"  {i}. {desc}")
            print(f"     $ {cmd}")
        print()


# -- results -------------------------------------------------------------------------


def write_results(cfg: Config, results: list[ScenarioResult]) -> None:
    payload = [r.to_dict() for r in results]
    (cfg.output_dir / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# attention-manager eval results",
        "",
        f"- run id: `{cfg.run_id}`",
        f"- generated: {datetime.now(UTC).isoformat()}",
        "",
        "| scenario | verdict | assertions | duration |",
        "|---|---|---|---|",
    ]
    for r in results:
        n_pass = sum(1 for a in r.assertions if a.passed)
        verdict = "PASS" if r.passed else "FAIL"
        lines.append(f"| {r.scenario} | **{verdict}** | {n_pass}/{len(r.assertions)} | {r.duration_s:.1f}s |")
    for r in results:
        lines += ["", f"## {r.scenario}", ""]
        for a in r.assertions:
            mark = "PASS" if a.passed else "FAIL"
            lines.append(f"- [{mark}] `{a.name}` — {a.detail}")
    lines.append("")
    (cfg.output_dir / "results.md").write_text("\n".join(lines), encoding="utf-8")


# -- main ----------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="attention-manager evaluation harness (step 1)")
    parser.add_argument(
        "--dtu-exec",
        default=None,
        help="command prefix that executes inside the DTU, e.g. "
        "'amplifier-digital-twin exec --stream dtu-a1b2c3d4' (commands run as '<prefix> -- bash -c ...')",
    )
    parser.add_argument(
        "--output-dir", default=None, help=f"results directory (default: {DEFAULT_OUTPUT_ROOT}/<UTC ts>)"
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=["1", "2", "3", "4", "5", "6", "7", "8", "9"],
        help="run only this scenario (repeatable)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print planned command sequences; no DTU required")
    parser.add_argument(
        "--repo-dir", default=DEFAULT_REPO_DIR, help="in-DTU path of the amplifier-attention-manager checkout"
    )
    parser.add_argument("--work-dir", default=DEFAULT_WORK_DIR, help="in-DTU scratch root for queues/logs")
    parser.add_argument("--am-cli", default=DEFAULT_AM_CLI, help="in-DTU attention-manager CLI command prefix")
    parser.add_argument(
        "--amplifier-args", default="", help="extra args appended to 'amplifier run' (e.g. provider flags)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    scenario_numbers = sorted({int(n) for n in (args.scenario or [str(n) for n in RUNNERS])})
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else Path(DEFAULT_OUTPUT_ROOT) / run_id
    cfg = Config(
        dtu_exec=args.dtu_exec,
        output_dir=output_dir,
        run_id=run_id,
        am_cli=args.am_cli,
        repo_dir=args.repo_dir,
        work_dir=args.work_dir,
        amplifier_args=args.amplifier_args,
    )

    if args.dry_run:
        print_dry_run(cfg, scenario_numbers)
        return 0

    if not cfg.dtu_exec:
        print("error: --dtu-exec is required unless --dry-run is set", file=sys.stderr)
        return 2

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    print(f"output: {cfg.output_dir}")
    results: list[ScenarioResult] = []
    for n in scenario_numbers:
        spec = SPECS[n]
        print(f"\n== {spec.title} ==")
        results.append(RUNNERS[n](cfg))

    write_results(cfg, results)
    all_pass = all(r.passed for r in results)
    print(f"\n{'ALL PASS' if all_pass else 'FAILURES PRESENT'} — results in {cfg.output_dir}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
