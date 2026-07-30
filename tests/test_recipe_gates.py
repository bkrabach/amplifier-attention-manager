"""Recipe-gate poller tests — NO real amplifier: a FAKE amplifier stub script
emits the REAL observed `amplifier tool invoke -o json` output shape (noise
line + JSON envelope whose `result` field is the Python-repr STRING of the
tool output) and records every invocation to a log file.

Gate DISCOVERY never touches the stub: it is a direct disk read of the
recipes tool's persisted session layout (faked here in tmp, byte-shape
verified against the real tool's SessionManager). The stub serves only the
forwarding invokes (approve/deny/resume).

Covers: strict output parsing, disk discovery (including the ZERO-subprocess
idle guarantee — the production defect was 1,820 junk amplifier sessions
from invoke-based discovery), packet creation, dedupe across polls AND
poller instances (disk-tracked), answer forwarding (approve with message /
deny with reason), the loud error paths, and supervisor wiring.
"""

import json
import stat
import subprocess
import time
from pathlib import Path

import pytest

from attention_manager.packet import Packet
from attention_manager.queue import PacketQueue
from attention_manager.recipe_gates import (
    RecipeGateError,
    RecipeGatePoller,
    parse_invoke_output,
    recipes_project_slug,
)
from attention_manager.state import SupervisorState

# The fake amplifier CLI. Handles `tool invoke recipes <k=v...> -o json`:
# appends the invocation args to $FAKE_INVOKE_LOG, then answers. Output
# mirrors the REAL observed shape: a non-JSON noise line, then a JSON
# envelope whose "result" is str() of the tool's output dict (Python repr).
# There is deliberately NO approvals handling — discovery must never invoke.
FAKE_AMPLIFIER = r"""#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
log = os.environ.get("FAKE_INVOKE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\n")

kv = dict(a.split("=", 1) for a in args if "=" in a)
op = kv.get("operation")
fail_ops = [o for o in os.environ.get("FAKE_FAIL_OPS", "").split(",") if o]
if op in fail_ops:
    print("simulated recipes tool failure", file=sys.stderr)
    sys.exit(1)

result = {"session_id": kv.get("session_id"), "stage_name": kv.get("stage_name"), "status": "ok"}
print("Bundle 'amplifier-dev' prepared successfully")
print(json.dumps({"status": "success", "tool": "recipes", "result": str(result)}, indent=2))
"""

# Session id in the REAL generator's shape: {16 hex}-{YYYYMMDD-HHMMSS}_recipe
# (recipes tool session.py generate_session_id).
APPROVAL = {
    "session_id": "ab12cd34ef567890-20260728-060000_recipe",
    "recipe_name": "dependency-upgrade",
    "stage_name": "deploy",
    "approval_prompt": "Deploy the upgraded dependencies to staging?",
    "approval_timeout": 0,
    "approval_requested_at": "2026-07-28T06:00:00",
    "approval_default": "deny",
}


def seed_recipe_session(
    projects_dir: Path,
    project_dir: Path,
    approval: dict = APPROVAL,
    pending: bool = True,
    raw: str | None = None,
) -> Path:
    """Write a state.json in the recipes tool's REAL persisted layout:
    <projects>/<slug>/recipe-sessions/<session_id>/state.json (see
    recipe_gates.py module docstring for the verified format). Returns the
    state file path."""
    session_dir = projects_dir / recipes_project_slug(project_dir) / "recipe-sessions" / approval["session_id"]
    session_dir.mkdir(parents=True, exist_ok=True)
    state_file = session_dir / "state.json"
    if raw is not None:
        state_file.write_text(raw, encoding="utf-8")
        return state_file
    state = {
        "session_id": approval["session_id"],
        "recipe_name": approval["recipe_name"],
        "started": "2026-07-28T06:00:00",
        "current_step_index": 0,
        "completed_steps": [],
        "project_path": str(project_dir.resolve()),
    }
    if pending:
        state.update(
            {
                "pending_approval_stage": approval["stage_name"],
                "pending_approval_prompt": approval["approval_prompt"],
                "pending_approval_timeout": approval["approval_timeout"],
                "pending_approval_requested_at": approval["approval_requested_at"],
                "pending_approval_default": approval["approval_default"],
                "stage_approvals": {approval["stage_name"]: "pending"},
            }
        )
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state_file


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("ATTENTION_HOME", str(path))
    return path


@pytest.fixture
def fake_amplifier(tmp_path, monkeypatch) -> Path:
    stub = tmp_path / "bin" / "fake-amplifier"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(FAKE_AMPLIFIER, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_INVOKE_LOG", str(tmp_path / "invoke.log"))
    return stub


@pytest.fixture
def project_dir(tmp_path) -> Path:
    """The cwd the poller polls for — the recipes tool scopes sessions per-project."""
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
def recipes_projects_dir(tmp_path, monkeypatch) -> Path:
    """Faked recipes tool session store base (real default: ~/.amplifier/projects)."""
    path = tmp_path / "recipes-projects"
    monkeypatch.setenv("ATTENTION_RECIPES_PROJECTS_DIR", str(path))
    return path


@pytest.fixture
def pending_gate(recipes_projects_dir, project_dir) -> Path:
    """One pending approval gate on disk; returns its state.json path."""
    return seed_recipe_session(recipes_projects_dir, project_dir)


@pytest.fixture
def poller(home, queue_root, fake_amplifier, pending_gate, project_dir) -> RecipeGatePoller:
    return RecipeGatePoller(
        home=home, queue=PacketQueue(queue_root), amplifier_bin=str(fake_amplifier), timeout_s=30, cwd=project_dir
    )


def invocations(tmp_path) -> list[list[str]]:
    log = tmp_path / "invoke.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def resume_calls(tmp_path) -> list[list[str]]:
    return [c for c in invocations(tmp_path) if "operation=resume" in c]


def wait_for_resume_calls(tmp_path, count: int, timeout_s: float = 10.0) -> list[list[str]]:
    """Poll the stub invoke log until `count` resume calls appear (resume is
    a fire-and-forget BACKGROUND child — the log write is asynchronous)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        calls = resume_calls(tmp_path)
        if len(calls) >= count:
            return calls
        time.sleep(0.05)
    raise TimeoutError(f"expected {count} resume invocation(s), saw {resume_calls(tmp_path)}")


def events_named(home: Path, name: str) -> list[dict]:
    return [e for e in SupervisorState(home).read_events() if e["event"] == name]


# -- output parsing (the observed real shape is the contract) ----------------------


class TestParseInvokeOutput:
    # Verbatim from a real run on this machine (2026-07-28):
    REAL_OUTPUT = (
        "Bundle 'amplifier-dev' prepared successfully\n"
        "{\n"
        '  "status": "success",\n'
        '  "tool": "recipes",\n'
        "  \"result\": \"{'pending_approvals': [], 'count': 0}\"\n"
        "}\n"
    )

    def test_parses_the_real_observed_shape(self):
        payload = parse_invoke_output(self.REAL_OUTPUT)
        assert payload == {"pending_approvals": [], "count": 0}

    def test_accepts_a_dict_result_for_forward_compat(self):
        out = json.dumps({"status": "success", "tool": "recipes", "result": {"count": 1}})
        assert parse_invoke_output(out) == {"count": 1}

    def test_rejects_failure_status(self):
        out = json.dumps({"status": "error", "tool": "recipes", "result": None})
        with pytest.raises(RecipeGateError, match="reported failure"):
            parse_invoke_output(out)

    def test_rejects_no_json_and_garbage_result(self):
        with pytest.raises(RecipeGateError, match="no JSON envelope"):
            parse_invoke_output("nothing here")
        with pytest.raises(RecipeGateError, match="not valid JSON"):
            parse_invoke_output("{not json")
        out = json.dumps({"status": "success", "result": "not a { literal"})
        with pytest.raises(RecipeGateError, match="not a Python literal"):
            parse_invoke_output(out)
        out = json.dumps({"status": "success", "result": 42})
        with pytest.raises(RecipeGateError, match="unexpected type"):
            parse_invoke_output(out)


# -- packetizing pending gates ------------------------------------------------------


class TestPacketize:
    def test_pending_approval_becomes_recipe_gate_packet(self, poller, home):
        results = poller.poll_once()
        assert [r["action"] for r in results] == ["packetized"]

        queue = poller.queue
        pending = queue.list_pending()
        assert len(pending) == 1
        packet: Packet = pending[0]
        assert packet.source.kind == "recipe-gate"
        assert packet.source.work_unit == APPROVAL["session_id"]
        assert packet.question == APPROVAL["approval_prompt"]
        assert packet.option_ids() == ["approve", "deny"]
        assert "stage: deploy" in packet.context
        assert "dependency-upgrade" in packet.context

        assert len(events_named(home, "recipe_gates:packetized")) == 1
        ledger = SupervisorState(home).ledger_read()
        assert any(e["kind"] == "recipe_gate_packetized" for e in ledger)

    def test_dedupe_across_polls_and_restarts(self, poller, home, queue_root, fake_amplifier, project_dir):
        poller.poll_once()
        poller.poll_once()
        assert len(poller.queue.list_pending()) == 1  # same gate never packetized twice

        # A FRESH poller instance (restart) reads the disk-tracked state.
        fresh = RecipeGatePoller(
            home=home, queue=PacketQueue(queue_root), amplifier_bin=str(fake_amplifier), timeout_s=30, cwd=project_dir
        )
        fresh.poll_once()
        assert len(fresh.queue.list_pending()) == 1

    def test_question_synthesized_when_prompt_empty(self, poller, recipes_projects_dir, project_dir):
        seed_recipe_session(recipes_projects_dir, project_dir, approval=dict(APPROVAL, approval_prompt=""))
        poller.poll_once()
        packet = poller.queue.list_pending()[0]
        assert "deploy" in packet.question and "dependency-upgrade" in packet.question

    def test_malformed_approval_record_is_a_loud_error(self, poller, home, monkeypatch):
        # Defense-in-depth: a record missing session_id/stage_name (format
        # drift in the discovered state) is a loud error, never a bad packet.
        monkeypatch.setattr(poller, "_pending_approvals_from_disk", lambda: [{"nope": True}])
        results = poller.poll_once()
        assert [r["action"] for r in results] == ["error"]
        assert events_named(home, "recipe_gates:error")


# -- disk discovery (the fix for the session-flood defect) ----------------------------


class TestDiskDiscovery:
    def test_default_base_matches_the_shipped_bundle_config(self):
        # behaviors/recipes.yaml in the shipped recipes bundle sets session_dir
        # to this path with a LITERAL un-substituted {project} — host-verified
        # by executing a real gate recipe and locating its state.json.
        from attention_manager.recipe_gates import DEFAULT_RECIPES_PROJECTS_DIR

        assert DEFAULT_RECIPES_PROJECTS_DIR == "~/.amplifier/projects/{project}/recipe-sessions"

    def test_slug_matches_the_recipes_tool_format(self):
        # Mirrors session.py get_project_slug: separators -> '-', leading '-' stripped.
        assert recipes_project_slug(Path("/home/bkrabach/dev/better-attention")) == "home-bkrabach-dev-better-attention"

    def test_idle_poll_makes_zero_subprocess_invocations(
        self, home, queue_root, fake_amplifier, recipes_projects_dir, project_dir, tmp_path, monkeypatch
    ):
        # THE defect: invoke-based discovery created one amplifier session per
        # poll (1,820 in 5.75h on the host). Idle polling must cost ZERO
        # subprocesses — spy on BOTH spawn paths in the module.
        from attention_manager import recipe_gates as rg

        def no_subprocess(*args, **kwargs):
            raise AssertionError(f"idle poll spawned a subprocess: {args!r}")

        monkeypatch.setattr(rg.subprocess, "run", no_subprocess)
        monkeypatch.setattr(rg.subprocess, "Popen", no_subprocess)
        idle = RecipeGatePoller(
            home=home, queue=PacketQueue(queue_root), amplifier_bin=str(fake_amplifier), timeout_s=30, cwd=project_dir
        )
        assert idle.poll_once() == []
        assert invocations(tmp_path) == []

    def test_pending_gate_is_discovered_from_disk_without_any_invoke(self, poller, tmp_path, monkeypatch):
        # Discovery of a REAL pending gate is also invoke-free: packetizing
        # spawns nothing (only forwarding a human's answer invokes amplifier).
        from attention_manager import recipe_gates as rg

        def no_subprocess(*args, **kwargs):
            raise AssertionError(f"discovery spawned a subprocess: {args!r}")

        monkeypatch.setattr(rg.subprocess, "run", no_subprocess)
        monkeypatch.setattr(rg.subprocess, "Popen", no_subprocess)
        results = poller.poll_once()
        assert [r["action"] for r in results] == ["packetized"]
        assert len(poller.queue.list_pending()) == 1
        assert invocations(tmp_path) == []

    def test_session_without_pending_approval_is_not_packetized(
        self, home, queue_root, fake_amplifier, recipes_projects_dir, project_dir
    ):
        seed_recipe_session(recipes_projects_dir, project_dir, pending=False)
        poller = RecipeGatePoller(
            home=home, queue=PacketQueue(queue_root), amplifier_bin=str(fake_amplifier), timeout_s=30, cwd=project_dir
        )
        assert poller.poll_once() == []
        assert poller.queue.list_pending() == []

    def test_other_projects_sessions_are_out_of_scope(
        self, home, queue_root, fake_amplifier, recipes_projects_dir, project_dir, tmp_path
    ):
        # The recipes tool scopes sessions per-project (working dir); the
        # poller must read the scope for ITS cwd only.
        other_project = tmp_path / "other-project"
        other_project.mkdir()
        seed_recipe_session(recipes_projects_dir, other_project)
        poller = RecipeGatePoller(
            home=home, queue=PacketQueue(queue_root), amplifier_bin=str(fake_amplifier), timeout_s=30, cwd=project_dir
        )
        assert poller.poll_once() == []

    def test_corrupt_state_json_is_loud_skipped_and_reported_once(
        self, poller, home, recipes_projects_dir, project_dir
    ):
        # The tool writes state.json NON-atomically — a torn read must not
        # kill the poll (the valid gate still packetizes), must be loud
        # (recipe_gates:error phase=disk_scan), and must not spam (once per
        # file per poller instance).
        other = dict(APPROVAL, session_id="ffffffffffffffff-20260728-070000_recipe")
        seed_recipe_session(recipes_projects_dir, project_dir, approval=other, raw='{"torn writ')

        results = poller.poll_once()
        assert [r["action"] for r in results] == ["packetized"]  # the valid gate landed
        errors = [e for e in events_named(home, "recipe_gates:error") if e.get("phase") == "disk_scan"]
        assert len(errors) == 1 and other["session_id"] in errors[0]["file"]

        poller.poll_once()  # same corrupt file: no new event from this instance
        errors = [e for e in events_named(home, "recipe_gates:error") if e.get("phase") == "disk_scan"]
        assert len(errors) == 1

    def test_torn_write_self_heals_next_poll(self, poller, home, recipes_projects_dir, project_dir):
        other = dict(APPROVAL, session_id="ffffffffffffffff-20260728-070000_recipe")
        seed_recipe_session(recipes_projects_dir, project_dir, approval=other, raw='{"torn writ')
        poller.poll_once()
        assert len(poller.queue.list_pending()) == 1  # only the valid gate so far

        seed_recipe_session(recipes_projects_dir, project_dir, approval=other)  # the write completes
        poller.poll_once()
        assert len(poller.queue.list_pending()) == 2  # recovered without intervention


# -- forwarding answers back to the recipes tool --------------------------------------


class TestForwardAnswers:
    def test_approve_forwarded_with_rationale_as_message(self, poller, home, tmp_path):
        poller.poll_once()
        packet_id = poller.queue.list_pending()[0].id
        poller.queue.answer(packet_id, "approve", rationale="ship it")

        results = poller.poll_once()
        assert any(r["action"] == "resolved" and r["answer"] == "approve" for r in results)

        approve_calls = [c for c in invocations(tmp_path) if "operation=approve" in c]
        assert len(approve_calls) == 1
        call = approve_calls[0]
        assert call[:3] == ["tool", "invoke", "recipes"]
        assert f"session_id={APPROVAL['session_id']}" in call
        assert f"stage_name={APPROVAL['stage_name']}" in call
        assert "message=ship it" in call
        assert len(events_named(home, "recipe_gates:resolved")) == 1

        # Resolved gates are never forwarded twice.
        poller.poll_once()
        assert len([c for c in invocations(tmp_path) if "operation=approve" in c]) == 1

    def test_deny_forwarded_with_reason(self, poller, tmp_path):
        poller.poll_once()
        packet_id = poller.queue.list_pending()[0].id
        poller.queue.answer(packet_id, "deny", rationale="not this week")
        poller.poll_once()
        deny_calls = [c for c in invocations(tmp_path) if "operation=deny" in c]
        assert len(deny_calls) == 1
        assert "reason=not this week" in deny_calls[0]

    def test_forward_failure_is_loud_retry_once_then_error(self, poller, home, tmp_path, monkeypatch):
        poller.poll_once()
        packet_id = poller.queue.list_pending()[0].id
        poller.queue.answer(packet_id, "approve")

        monkeypatch.setenv("FAKE_FAIL_OPS", "approve")
        results = poller.poll_once()  # attempt 1 — retrying
        assert any(r["action"] == "error" for r in results)
        results = poller.poll_once()  # attempt 2 — giving up
        assert any(r["action"] == "error" for r in results)

        errors = events_named(home, "recipe_gates:error")
        forward_errors = [e for e in errors if e.get("packet_id") == packet_id]
        assert [e["attempt"] for e in forward_errors] == [1, 2]
        assert [e["retrying"] for e in forward_errors] == [True, False]

        # Gate marked error on disk: a third poll does NOT invoke again.
        attempts_before = len([c for c in invocations(tmp_path) if "operation=approve" in c])
        poller.poll_once()
        assert len([c for c in invocations(tmp_path) if "operation=approve" in c]) == attempts_before

    def test_discovery_failure_does_not_block_forwarding(self, poller, home, tmp_path, monkeypatch):
        poller.poll_once()
        packet_id = poller.queue.list_pending()[0].id
        poller.queue.answer(packet_id, "approve")

        def boom():
            raise RecipeGateError("disk discovery exploded")

        monkeypatch.setattr(poller, "_pending_approvals_from_disk", boom)
        results = poller.poll_once()
        assert any(r["action"] == "error" and r.get("phase") == "discovery" for r in results)
        assert any(r["action"] == "resolved" for r in results)  # forwarding still ran


# -- auto-resume after approve (fire-and-forget) ---------------------------------------


class TestAutoResume:
    def _packetize_and_answer(self, poller, option: str, rationale: str | None = None) -> str:
        poller.poll_once()
        packet_id = poller.queue.list_pending()[0].id
        poller.queue.answer(packet_id, option, rationale=rationale)
        return packet_id

    def _gate_record(self, home) -> dict:
        gates = json.loads((home / "recipe-gates.json").read_text(encoding="utf-8"))["gates"]
        assert len(gates) == 1
        return next(iter(gates.values()))

    def test_approve_launches_resume_in_background_exactly_once(self, poller, home, tmp_path):
        self._packetize_and_answer(poller, "approve", rationale="ship it")
        poller.poll_once()  # forwards approve + launches resume (background)

        calls = wait_for_resume_calls(tmp_path, 1)
        assert len(calls) == 1
        call = calls[0]
        assert call[:3] == ["tool", "invoke", "recipes"]
        assert f"session_id={APPROVAL['session_id']}" in call
        assert not any(a.startswith("stage_name=") for a in call)  # resume is session-scoped

        # Event + ledger + idempotency field + log file.
        launched = events_named(home, "recipe_gates:resume_launched")
        assert len(launched) == 1
        gate = self._gate_record(home)
        assert gate["resume_launched_at"]
        log_path = home / "recipe-gates" / f"{APPROVAL['session_id']}.resume.log"
        assert log_path.exists()

        # Idempotent: another poll never launches resume again.
        poller.poll_once()
        time.sleep(0.3)  # would be long enough for a second background launch to log
        assert len(resume_calls(tmp_path)) == 1
        assert len(events_named(home, "recipe_gates:resume_launched")) == 1

    def test_launch_resume_guard_is_direct_too(self, poller, home, tmp_path):
        # Belt-and-suspenders: even a DIRECT second call is a no-op once
        # resume_launched_at is set (the field, not the loop, is the guard).
        self._packetize_and_answer(poller, "approve")
        poller.poll_once()
        wait_for_resume_calls(tmp_path, 1)
        gates = poller._load_gates()
        key, gate = next(iter(gates.items()))
        assert gate["resume_launched_at"]
        poller._launch_resume(key, gate)
        time.sleep(0.3)
        assert len(resume_calls(tmp_path)) == 1

    def test_deny_does_not_resume(self, poller, home, tmp_path):
        self._packetize_and_answer(poller, "deny", rationale="not this week")
        poller.poll_once()
        assert len([c for c in invocations(tmp_path) if "operation=deny" in c]) == 1
        time.sleep(0.3)  # would be long enough for a wrongly-launched resume to log
        assert resume_calls(tmp_path) == []
        assert events_named(home, "recipe_gates:resume_launched") == []
        gate = self._gate_record(home)
        assert gate["status"] == "resolved" and "resume_launched_at" not in gate

    def test_resume_launch_failure_is_loud_approve_stays_resolved(self, poller, home, tmp_path, monkeypatch):
        from attention_manager import recipe_gates as rg

        self._packetize_and_answer(poller, "approve")

        real_popen = subprocess.Popen

        def popen_boom(cmd, *args, **kwargs):
            if any("operation=resume" in str(part) for part in cmd):
                raise OSError("cannot spawn resume child")
            return real_popen(cmd, *args, **kwargs)  # pragma: no cover — approve uses run(), not Popen

        monkeypatch.setattr(rg.subprocess, "Popen", popen_boom)
        results = poller.poll_once()

        # The approve itself succeeded and stays recorded — distinctly.
        assert any(r["action"] == "resolved" and r["answer"] == "approve" for r in results)
        assert len(events_named(home, "recipe_gates:resolved")) == 1
        gate = self._gate_record(home)
        assert gate["status"] == "resolved"
        assert "resume_launched_at" not in gate  # launch never happened

        # The resume failure is loud and names the manual recovery path.
        errors = [e for e in events_named(home, "recipe_gates:error") if e.get("phase") == "resume_launch"]
        assert len(errors) == 1
        assert "approve was forwarded successfully" in errors[0]["error"]
        assert "operation=resume" in errors[0]["error"]
        assert events_named(home, "recipe_gates:resume_launched") == []
        assert resume_calls(tmp_path) == []


# -- preflight + supervisor wiring ------------------------------------------------------


def _no_observation(session: str, log: Path):
    from attention_manager.workers import Observation

    return Observation(alive=False, sentinel_seen=False, exit_code=None, session_id=None)


class TestWiring:
    def test_preflight_missing_binary_fails_loud(self, home, queue_root):
        poller = RecipeGatePoller(home=home, queue=PacketQueue(queue_root), amplifier_bin="definitely-not-a-binary")
        with pytest.raises(RuntimeError, match="not found on PATH"):
            poller.preflight()

    def test_supervisor_runs_poller_every_n_ticks(self, home, queue_root, poller):
        from attention_manager.supervisor import Supervisor

        supervisor = Supervisor(
            home=home,
            queue=poller.queue,
            recipes_every=2,
            recipe_poller=poller,
            list_sessions=list,
            observe=_no_observation,
        )
        supervisor.tick()  # tick 0 -> poll runs (0 % 2 == 0)
        assert len(poller.queue.list_pending()) == 1

    def test_recipes_off_by_default(self, home, queue_root):
        from attention_manager.supervisor import Supervisor

        supervisor = Supervisor(home=home, queue=PacketQueue(queue_root))
        assert supervisor.recipes_every is None
        assert supervisor.recipe_poller is None

    def test_poller_crash_does_not_kill_tick(self, home, queue_root, poller):
        from attention_manager.supervisor import Supervisor

        class Boom(RecipeGatePoller):
            def poll_once(self):
                raise OSError("recipes fell off")

        boom = Boom(home=home, queue=PacketQueue(queue_root), amplifier_bin="x")
        supervisor = Supervisor(
            home=home,
            queue=PacketQueue(queue_root),
            recipes_every=1,
            recipe_poller=boom,
            list_sessions=list,
            observe=_no_observation,
        )
        supervisor.tick()  # must not raise
        errors = [e for e in supervisor.state.read_events() if e["event"] == "recipe_gates:error"]
        assert errors and "recipes fell off" in errors[0]["error"]


class TestCli:
    def test_recipes_poll_once_via_cli(
        self, home, queue_root, fake_amplifier, pending_gate, project_dir, monkeypatch, capsys
    ):
        from attention_manager.cli import main

        monkeypatch.setenv("ATTENTION_AMPLIFIER_BIN", str(fake_amplifier))
        monkeypatch.chdir(project_dir)  # the CLI poller scopes discovery to its cwd
        assert main(["recipes", "poll", "--once"]) == 0
        out = capsys.readouterr().out
        assert "packetized" in out
        assert len(PacketQueue(queue_root).list_pending()) == 1

    def test_recipes_poll_requires_once_flag(self, capsys):
        from attention_manager.cli import main

        with pytest.raises(SystemExit):
            main(["recipes", "poll"])
