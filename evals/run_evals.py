#!/usr/bin/env python3
"""attention-manager evaluation harness (build step 1).

Runs the three scenarios in evals/scenarios/ INSIDE an already-running DTU
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
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
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

CNE = "could-not-evaluate"


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
}


# -- in-DTU command builders (shared by executor and --dry-run planner) -----------


def _env_prefix(queue_dir: str) -> str:
    return f"export ATTENTION_QUEUE_DIR={shlex.quote(queue_dir)};"


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
    """Background-launch a worker in its own process group; print its PID.

    setsid puts the worker in a new session (PGID == PID) so scenario 3 can
    SIGKILL the whole group. stdout+stderr -> worker.log; exit -> worker.exit.
    """
    log = f"{dtu_sdir}/worker.log"
    exit_file = f"{dtu_sdir}/worker.exit"
    # `amplifier run` rejects bare filesystem paths for -B — only registered
    # names or URIs (file://, git+https://) are accepted (verified in DTU).
    bundle_uri = bundle_path if "://" in bundle_path else f"file://{bundle_path}"
    inner = (
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


def cmd_cat(path: str) -> str:
    return f"cat {shlex.quote(path)}"


def cmd_file_exists(path: str) -> str:
    return f"test -f {shlex.quote(path)} && echo yes || echo no"


def cmd_file_absent(path: str) -> str:
    return f"test ! -f {shlex.quote(path)} && echo absent || echo present"


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
        stamp = datetime.now(timezone.utc).isoformat()
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


class Scenario:
    """Shared per-scenario runtime: paths, deadline, assertions, snapshots."""

    def __init__(self, cfg: Config, spec: ScenarioSpec):
        self.cfg = cfg
        self.spec = spec
        self.out_dir = cfg.output_dir / spec.slug
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.dtu_sdir = f"{cfg.work_dir}/{cfg.run_id}/{spec.slug}"
        self.queue_dir = f"{self.dtu_sdir}/queue"
        self.deadline = time.monotonic() + SCENARIO_TIMEOUT_S
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
            "ts": datetime.now(timezone.utc).isoformat(),
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
        pid = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        if result.rc != 0 or not pid.isdigit():
            self.cne("worker-launched", f"launch rc={result.rc}, pid output {pid!r}, stderr={result.stderr.strip()!r}")
            return None
        self.worker_pid = pid
        return pid

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


RUNNERS = {1: run_scenario_1, 2: run_scenario_2, 3: run_scenario_3}


# -- dry-run planner --------------------------------------------------------------------


def plan_scenario(cfg: Config, spec: ScenarioSpec) -> list[tuple[str, str]]:
    dtu_sdir = f"{cfg.work_dir}/{cfg.run_id}/{spec.slug}"
    queue_dir = f"{dtu_sdir}/queue"
    bundle = f"{cfg.repo_dir}/{spec.bundle_rel}"
    pkt, pid = "<PACKET_ID>", "<PID>"
    steps: list[tuple[str, str]] = [
        (
            "launch worker in background (own process group; stdout+stderr -> worker.log; "
            "exit code -> worker.exit; prints PID)",
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
    print(f"per-scenario hard timeout: {SCENARIO_TIMEOUT_S:.0f}s | packet wait: {PACKET_WAIT_S:.0f}s\n")
    for n in scenario_numbers:
        spec = SPECS[n]
        print(f"=== {spec.title} ({spec.slug}) ===")
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
        f"- generated: {datetime.now(timezone.utc).isoformat()}",
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
        "--scenario", action="append", choices=["1", "2", "3"], help="run only this scenario (repeatable)"
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
    scenario_numbers = sorted({int(n) for n in (args.scenario or ["1", "2", "3"])})
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
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
