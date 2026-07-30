"""Recipe-gate poller — producer #4 of the escalation bus (design D9).

Recipes' staged approval gates are bridged by an EXTERNAL ADAPTER (never
patches the recipes tool), same rationale family as D8 (stdlib-only root
package, reuse the environment's amplifier install, all state on disk):

* discover gates:  DIRECT DISK READ of the recipes tool's persisted session
  state (observation-only — this poller never writes those files). See
  "Discovery: the recipes tool's storage layout" below.
* answer gates:    ``amplifier tool invoke recipes operation=approve
  session_id=... stage_name=... message=...`` (or ``operation=deny`` with
  ``reason=...``)
* resume:          ``amplifier tool invoke recipes operation=resume
  session_id=...`` — REQUIRED after approve: the real tool's ``approve`` only
  MARKS the stage approved, it does not continue execution. Launched
  fire-and-forget in the background (see :meth:`RecipeGatePoller._launch_resume`)
  so the design promise "recipe resumes without the human being interrupted"
  holds. Deny needs no resume — the recipe stops at the gate per the tool's
  semantics.

COST MODEL (why discovery is a disk read, not a tool invoke): every
``amplifier tool invoke`` prepares a full bundle and creates ONE amplifier
session in the invoking project's session store
(``~/.amplifier/projects/<slug>/sessions/``) even for pure tool code with no
LLM. Dogfooding measured the damage: ``supervise --recipes`` at the old
defaults (~1 poll / 10s) created 1,820 junk sessions in 5.75 hours, polluted
session-list/resume, fed 1,820 no-op sessions to the context-intelligence
hook, and the invoke itself repeatedly hit its 120s timeout. Discovery via
``operation=approvals`` bought ZERO authority for that price — the tool's
approvals op is itself a pure read of the same on-disk state this poller now
reads directly. Invokes remain only for approve/deny/resume: rare,
human-triggered, and worth a real session. Idle polling costs zero
subprocesses.

Discovery: the recipes tool's storage layout (verified against the installed
tool source, amplifier-bundle-recipes ``modules/tool-recipes/
amplifier_module_tool_recipes/`` — ``__init__.py`` mount()/_get_working_dir,
``session.py`` SessionManager):

* base dir:   the tool's ``session_dir`` config. Code default is
  ``~/.amplifier/projects``, but the SHIPPED recipes bundle
  (behaviors/recipes.yaml) sets ``~/.amplifier/projects/{project}/
  recipe-sessions`` with a LITERAL un-substituted ``{project}`` — the real
  base on any standard install (host-verified; see
  ``DEFAULT_RECIPES_PROJECTS_DIR``).
* scope:      per-project — the tool slugs the invoking session's working
  dir: absolute path with ``/`` and ``\\`` replaced by ``-``, leading ``-``
  stripped (session.py get_project_slug). NOTE this differs from the
  amplifier CLI's own project slug, which KEEPS the leading dash.
* sessions:   ``<base>/<slug>/recipe-sessions/<session_id>/state.json``
  where session ids look like ``<16 hex>-<YYYYMMDD-HHMMSS>_recipe``
* pending:    a session has a pending approval gate iff its ``state.json``
  has a non-empty ``pending_approval_stage``; the sibling
  ``pending_approval_prompt`` / ``pending_approval_timeout`` /
  ``pending_approval_requested_at`` / ``pending_approval_default`` +
  ``recipe_name`` / ``session_id`` keys are exactly the fields the tool's
  ``approvals`` operation returns (session.py get_pending_approval) — the
  disk IS the authority; there is no live-process state and the approvals
  op applies no extra logic (not even timeout defaults).
* caveat:     the tool writes ``state.json`` NON-atomically (plain open+dump),
  so a reader can catch a torn write. A state.json that fails to parse is
  reported LOUD (``recipe_gates:error`` phase=disk_scan, once per file per
  poller instance to avoid event spam) and retried naturally next poll.

The poller reads the scope for ITS OWN ``cwd`` (the same cwd it passes to
forwarding invokes), so discovery and forwarding always agree on the project.
Base dir is overridable via ``$ATTENTION_RECIPES_PROJECTS_DIR`` (tests/smoke
point it at a faked layout; the real default matches the tool's default).

Observed output shape for the REMAINING invokes (verified against the real
CLI on this machine):

    Bundle 'amplifier-dev' prepared successfully
    {
      "status": "success",
      "tool": "recipes",
      "result": "{'pending_approvals': [], 'count': 0}"
    }

i.e. optional non-JSON noise lines, then a JSON envelope whose ``result``
field is a STRING holding the Python repr of the tool's output dict. Parsing:
locate the envelope's opening brace, ``json.loads`` it, then
``ast.literal_eval`` the result string. Anything else fails loud.

Each pending approval gate is packetized ONCE (dedupe key: ``session_id`` +
``stage_name``, tracked on disk in ``<home>/recipe-gates.json`` — idempotent
across restarts, D5). When the packet is answered (the poller polls
``answered/`` like every consumer), the answer is forwarded via
``operation=approve`` / ``operation=deny`` with the human's rationale.

Fail loud (D7): a missing amplifier binary or a failed invoke emits a
``recipe_gates:error`` event + stderr, never a silent skip. Forwarding an
answer gets one retry max (mirroring the triage runner), then the gate is
marked ``error`` — visibly — so the loop never spins on a dead gate.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, TextIO

from .packet import Option, Packet, Source, utc_now_iso
from .queue import PacketQueue
from .state import SupervisorState, _write_atomic
from .triage import ENV_AMPLIFIER_BIN, default_amplifier_bin

STATE_FILENAME = "recipe-gates.json"
DEFAULT_TIMEOUT_S = 120.0
MAX_INVOKE_ATTEMPTS = 2  # one retry max on answer forwarding, each logged loudly

ENV_RECIPES_BUNDLE = "ATTENTION_RECIPES_BUNDLE"
ENV_RECIPES_PROJECTS_DIR = "ATTENTION_RECIPES_PROJECTS_DIR"
# The recipes tool's session store base. The tool's CODE default is
# ~/.amplifier/projects, but the SHIPPED recipes bundle overrides it:
# behaviors/recipes.yaml sets `session_dir: ~/.amplifier/projects/{project}/
# recipe-sessions` where `{project}` is a LITERAL directory name (no
# substitution happens) — host-verified by executing a real gate recipe and
# finding its state.json under this base. Non-standard mounts override via
# $ATTENTION_RECIPES_PROJECTS_DIR.
DEFAULT_RECIPES_PROJECTS_DIR = "~/.amplifier/projects/{project}/recipe-sessions"


class RecipeGateError(ValueError):
    """An amplifier invoke failed, or its output violated the observed shape."""


def parse_invoke_output(stdout: str) -> dict[str, Any]:
    """Parse ``amplifier tool invoke ... -o json`` stdout into the tool's output dict.

    Tolerates leading non-JSON noise (e.g. "Bundle '...' prepared successfully").
    The envelope's ``result`` field is the STRING repr of a Python dict
    (observed shape) — ``ast.literal_eval`` recovers it; a dict is also
    accepted for forward compatibility. Raises RecipeGateError on anything else.
    """
    brace = stdout.find("{")
    if brace < 0:
        raise RecipeGateError(f"no JSON envelope in amplifier output: {stdout!r}")
    try:
        envelope = json.loads(stdout[brace:])
    except json.JSONDecodeError as e:
        raise RecipeGateError(f"amplifier output envelope is not valid JSON: {e}; raw: {stdout!r}") from e
    if not isinstance(envelope, dict):
        raise RecipeGateError(f"amplifier output envelope is not an object: {envelope!r}")
    if envelope.get("status") != "success":
        raise RecipeGateError(f"amplifier tool invoke reported failure: {envelope!r}")
    result = envelope.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = ast.literal_eval(result)
        except (ValueError, SyntaxError) as e:
            raise RecipeGateError(f"envelope 'result' is not a Python literal: {e}; raw: {result!r}") from e
        if not isinstance(parsed, dict):
            raise RecipeGateError(f"envelope 'result' did not evaluate to a dict: {parsed!r}")
        return parsed
    raise RecipeGateError(f"envelope 'result' has unexpected type {type(result).__name__}: {result!r}")


def gate_key(session_id: str, stage_name: str) -> str:
    return f"{session_id}::{stage_name}"


def recipes_project_slug(project_path: Path) -> str:
    """The recipes tool's project slug for ``project_path``.

    Mirrors the tool's ``get_project_slug`` (session.py): absolute path with
    path separators replaced by ``-`` and ONE leading ``-`` stripped. This is
    NOT the amplifier CLI's own project slug (which keeps the leading dash).
    """
    slug = str(project_path.resolve()).replace("/", "-").replace("\\", "-")
    return slug.removeprefix("-")


class RecipeGatePoller:
    """Polls recipes approval gates into packets, forwards answers back."""

    def __init__(
        self,
        home: str | Path | None = None,
        queue: PacketQueue | None = None,
        state: SupervisorState | None = None,
        amplifier_bin: str | None = None,
        bundle: str | None = None,
        cwd: str | Path | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        projects_dir: str | Path | None = None,
        err: TextIO | None = None,
    ):
        # Same single-writer discipline as the triage runner: append-only
        # event/ledger writes only — never state.save(). The poller's OWN
        # durable state is <home>/recipe-gates.json, written atomically here.
        self.state = state or SupervisorState(home)
        self.queue = queue or PacketQueue()
        self.amplifier_bin = amplifier_bin or default_amplifier_bin()
        self.bundle = bundle if bundle is not None else os.environ.get(ENV_RECIPES_BUNDLE)
        # Recipe sessions are project-scoped inside the recipes tool (working
        # dir determines the project); discovery reads and forwarding invokes
        # both use this cwd so they always agree on the project.
        self.cwd = Path(cwd).expanduser() if cwd is not None else Path.cwd()
        self.timeout_s = timeout_s
        # The recipes tool's session store base dir (observation-only).
        self.projects_dir = Path(
            projects_dir or os.environ.get(ENV_RECIPES_PROJECTS_DIR) or DEFAULT_RECIPES_PROJECTS_DIR
        ).expanduser()
        self._err = err or sys.stderr
        self.gates_path = self.state.home / STATE_FILENAME
        # Torn/corrupt state.json reports: once per file per poller instance
        # (same precedent as Supervisor._reported_bad_files) — loud, not spammy.
        self._reported_bad_state_files: set[str] = set()

    # -- disk-tracked gate state (idempotent across restarts, D5) ---------------

    def _load_gates(self) -> dict[str, dict[str, Any]]:
        if not self.gates_path.exists():
            return {}
        try:
            data = json.loads(self.gates_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"corrupt recipe-gates state {self.gates_path}: {e}. "
                f"Delete it to re-packetize still-pending gates (dedupe restarts from the recipes tool)."
            ) from e
        return dict(data.get("gates", {}))

    def _save_gates(self, gates: dict[str, dict[str, Any]]) -> None:
        self.state.home.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.gates_path, json.dumps({"gates": gates}, indent=2, sort_keys=False) + "\n")

    # -- amplifier invocation ----------------------------------------------------

    def preflight(self) -> None:
        """Fail loud upfront if the amplifier binary is not available at all."""
        if shutil.which(self.amplifier_bin) is None:
            raise RuntimeError(
                f"amplifier binary {self.amplifier_bin!r} not found on PATH — the recipe-gate poller "
                f"shells out to the installed amplifier CLI (D9). Install amplifier or set ${ENV_AMPLIFIER_BIN}."
            )

    def _invoke(self, args: list[str]) -> dict[str, Any]:
        """Run ``amplifier tool invoke recipes <args> -o json``; parse strictly."""
        cmd = [self.amplifier_bin, "tool", "invoke", "recipes", *args, "-o", "json"]
        if self.bundle:
            cmd.extend(["-b", self.bundle])
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError as e:
            raise RecipeGateError(
                f"amplifier binary {self.amplifier_bin!r} not found — install amplifier or set ${ENV_AMPLIFIER_BIN}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise RecipeGateError(f"amplifier tool invoke timed out after {self.timeout_s}s: {cmd}") from e
        if proc.returncode != 0:
            raise RecipeGateError(
                f"amplifier tool invoke exited {proc.returncode}: {cmd}; "
                f"stdout: {proc.stdout.strip()!r}; stderr: {proc.stderr.strip()!r}"
            )
        return parse_invoke_output(proc.stdout)

    # -- discovery: direct disk read of the recipes tool's session store ----------

    def recipes_sessions_dir(self) -> Path:
        """The recipes tool's session dir for this poller's cwd (see module docstring)."""
        return self.projects_dir / recipes_project_slug(self.cwd) / "recipe-sessions"

    def _report_bad_state_file(self, state_file: Path, error: Exception) -> None:
        """Loud (event + stderr), once per file per poller instance.

        Not fatal: the recipes tool writes state.json non-atomically, so a
        torn read is EXPECTED under concurrency and self-heals next poll. A
        permanently corrupt file keeps being skipped but was reported loudly.
        """
        key = str(state_file)
        if key in self._reported_bad_state_files:
            return
        self._reported_bad_state_files.add(key)
        message = f"unreadable recipe session state {state_file}: {error} — skipped this poll, will retry next poll"
        self.state.append_event("recipe_gates:error", phase="disk_scan", file=key, error=str(error))
        print(f"ERROR: {message}", file=self._err)

    def _pending_approvals_from_disk(self) -> list[dict[str, Any]]:
        """Read pending approval gates straight from the recipes tool's state files.

        Observation-only (D9: external adapter — never writes the tool's
        files). Mirrors the tool's own ``approvals`` operation field-for-field
        (SessionManager.list_pending_approvals / get_pending_approval): a
        session is pending iff its state.json has a non-empty
        ``pending_approval_stage``. Zero subprocesses — this is what makes
        idle polling free (see COST MODEL in the module docstring).
        """
        sessions_dir = self.recipes_sessions_dir()
        if not sessions_dir.is_dir():
            return []
        approvals: list[dict[str, Any]] = []
        for session_dir in sorted(sessions_dir.iterdir()):
            state_file = session_dir / "state.json"
            if not session_dir.is_dir() or not state_file.exists():
                continue
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:  # OSError: unreadable; ValueError: torn/corrupt JSON
                self._report_bad_state_file(state_file, e)
                continue
            if not isinstance(state, dict):
                self._report_bad_state_file(state_file, TypeError(f"state is {type(state).__name__}, expected object"))
                continue
            if not state.get("pending_approval_stage"):
                continue  # no gate pending (running, completed, or already answered)
            approvals.append(
                {
                    "session_id": state.get("session_id") or session_dir.name,
                    "recipe_name": state.get("recipe_name", "unknown"),
                    "stage_name": state["pending_approval_stage"],
                    "approval_prompt": state.get("pending_approval_prompt", ""),
                    "approval_timeout": state.get("pending_approval_timeout", 0),
                    "approval_requested_at": state.get("pending_approval_requested_at"),
                    "approval_default": state.get("pending_approval_default", "deny"),
                }
            )
        return approvals

    # -- packetizing pending gates -----------------------------------------------

    def _packetize(self, approval: dict[str, Any]) -> Packet:
        session_id = str(approval["session_id"])
        stage_name = str(approval["stage_name"])
        recipe_name = str(approval.get("recipe_name") or "unknown")
        prompt = str(approval.get("approval_prompt") or "").strip()

        question = prompt or f"Approve recipe stage '{stage_name}' of '{recipe_name}' (session {session_id})?"
        context_lines = [
            f"Recipe approval gate (recipes tool, session {session_id}).",
            f"recipe: {recipe_name}",
            f"stage: {stage_name}",
        ]
        if prompt:
            context_lines.append(f"prompt: {prompt}")
        if approval.get("approval_requested_at"):
            context_lines.append(f"requested_at: {approval['approval_requested_at']}")
        timeout = approval.get("approval_timeout") or 0
        if timeout:
            context_lines.append(
                f"NOTE: the recipes tool applies its own default '{approval.get('approval_default', 'deny')}' "
                f"after {timeout}s — answer before then."
            )
        context_lines.append("Your answer rationale is forwarded to the recipe as the approval message / deny reason.")

        return Packet(
            question=question,
            options=[
                Option(id="approve", label="Approve stage", consequence="recipe continues to the next stage"),
                Option(id="deny", label="Deny stage", consequence="recipe execution stops at this gate (terminal)"),
            ],
            source=Source(kind="recipe-gate", work_unit=session_id),
            context="\n".join(context_lines),
        )

    def _packetize_new_gates(self, gates: dict[str, dict[str, Any]], results: list[dict[str, Any]]) -> None:
        approvals = self._pending_approvals_from_disk()
        for approval in approvals:
            if not isinstance(approval, dict) or not approval.get("session_id") or not approval.get("stage_name"):
                raise RecipeGateError(f"malformed pending approval record: {approval!r}")
            key = gate_key(str(approval["session_id"]), str(approval["stage_name"]))
            if key in gates:
                continue  # already packetized (disk-tracked dedupe)
            packet = self._packetize(approval)
            self.queue.write(packet, subdir="pending")
            gates[key] = {
                "packet_id": packet.id,
                "session_id": str(approval["session_id"]),
                "stage_name": str(approval["stage_name"]),
                "recipe_name": str(approval.get("recipe_name") or "unknown"),
                "status": "pending",
                "attempts": 0,
            }
            self._save_gates(gates)
            self.state.append_event(
                "recipe_gates:packetized",
                packet_id=packet.id,
                session_id=approval["session_id"],
                stage_name=approval["stage_name"],
                recipe_name=approval.get("recipe_name"),
            )
            self.state.ledger_append(
                "recipe_gate_packetized",
                packet_id=packet.id,
                session_id=approval["session_id"],
                stage_name=approval["stage_name"],
            )
            results.append({"action": "packetized", "packet_id": packet.id, "gate": key})

    # -- forwarding answers back to the recipes tool ------------------------------

    def _forward_answer(self, key: str, gate: dict[str, Any], results: list[dict[str, Any]]) -> None:
        packet_id = gate["packet_id"]
        answered_path = self.queue.path_for(packet_id, "answered")
        if not answered_path.exists():
            return  # still awaiting a human answer
        packet = self.queue.get(packet_id)
        if packet.resolution is None:
            raise ValueError(f"packet {packet_id!r} is in answered/ but has no resolution — corrupt queue state")
        answer = packet.resolution.answer
        rationale = (packet.resolution.rationale or "").strip()

        if answer == "approve":
            message = rationale or f"Approved via attention-manager packet {packet_id}"
            args = [
                "operation=approve",
                f"session_id={gate['session_id']}",
                f"stage_name={gate['stage_name']}",
                f"message={message}",
            ]
        else:  # deny — the packet's only other declared option
            reason = rationale or f"Denied via attention-manager packet {packet_id}"
            args = [
                "operation=deny",
                f"session_id={gate['session_id']}",
                f"stage_name={gate['stage_name']}",
                f"reason={reason}",
            ]

        try:
            self._invoke(args)
        except RecipeGateError as e:
            gate["attempts"] = int(gate.get("attempts", 0)) + 1
            retrying = gate["attempts"] < MAX_INVOKE_ATTEMPTS
            gate["last_error"] = str(e)
            if not retrying:
                gate["status"] = "error"
            self.state.append_event(
                "recipe_gates:error",
                packet_id=packet_id,
                session_id=gate["session_id"],
                stage_name=gate["stage_name"],
                error=str(e),
                attempt=gate["attempts"],
                retrying=retrying,
            )
            print(
                f"ERROR: forwarding {answer} for recipe gate {key} (packet {packet_id}) failed "
                f"(attempt {gate['attempts']}/{MAX_INVOKE_ATTEMPTS}): {e}"
                + (" — will retry next poll" if retrying else " — giving up (gate marked error)"),
                file=self._err,
            )
            results.append({"action": "error", "packet_id": packet_id, "gate": key, "error": str(e)})
            return

        gate["status"] = "resolved"
        gate["answer"] = answer
        self.state.append_event(
            "recipe_gates:resolved",
            packet_id=packet_id,
            session_id=gate["session_id"],
            stage_name=gate["stage_name"],
            answer=answer,
            rationale=rationale or None,
        )
        self.state.ledger_append(
            "recipe_gate_resolved",
            packet_id=packet_id,
            session_id=gate["session_id"],
            stage_name=gate["stage_name"],
            answer=answer,
        )
        results.append({"action": "resolved", "packet_id": packet_id, "gate": key, "answer": answer})

        if answer == "approve":
            # The real tool's `approve` only MARKS the stage approved — a
            # separate `resume` continues execution. Behavioral-model
            # scenario 3 promises the recipe resumes without the human, so
            # the poller launches it itself. Deny needs no resume.
            self._launch_resume(key, gate)

    # -- auto-resume after approve (fire-and-forget) --------------------------------

    def _launch_resume(self, key: str, gate: dict[str, Any]) -> None:
        """Launch ``operation=resume`` for an approved gate in the BACKGROUND.

        `resume` runs the remaining recipe stages SYNCHRONOUSLY (minutes,
        with LLM stages), so it must never block the poll / supervisor tick:
        it is spawned fire-and-forget (own session, no handle kept), with
        stdout/stderr captured to ``<home>/recipe-gates/<session_id>.resume.log``.

        v1 HONESTY NOTE: there is deliberately NO completion tracking — nothing
        waits on or polls the child. The resume log + the
        ``recipe_gates:resume_launched`` event are the only observability;
        recipe completion is observable from the log (or a later `approvals`
        poll seeing the next gate). A launch failure is loud
        (``recipe_gates:error``, phase ``resume_launch``) but the approve
        itself already succeeded and is never rolled back.

        Idempotent via the gate's ``resume_launched_at`` field (disk-tracked):
        resume is never launched twice for one gate.
        """
        if gate.get("resume_launched_at"):
            return  # already launched — never resume twice for one gate
        session_id = gate["session_id"]
        log_dir = self.state.home / "recipe-gates"
        log_path = log_dir / f"{session_id}.resume.log"
        cmd = [self.amplifier_bin, "tool", "invoke", "recipes", "operation=resume", f"session_id={session_id}"]
        cmd.extend(["-o", "json"])
        if self.bundle:
            cmd.extend(["-b", self.bundle])
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_path, "w", encoding="utf-8") as log:
                subprocess.Popen(  # our own amplifier CLI, detached fire-and-forget
                    cmd,
                    cwd=self.cwd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # survives the poller/supervisor exiting
                )
        except OSError as e:
            self.state.append_event(
                "recipe_gates:error",
                packet_id=gate["packet_id"],
                session_id=session_id,
                stage_name=gate["stage_name"],
                phase="resume_launch",
                error=(
                    f"approve was forwarded successfully but the resume launch failed: {e}. "
                    f"Resume manually: amplifier tool invoke recipes operation=resume session_id={session_id}"
                ),
            )
            print(
                f"ERROR: resume launch failed for recipe gate {key} (approve WAS forwarded — the stage is "
                f"approved but the recipe is NOT running): {e}. "
                f"Resume manually: amplifier tool invoke recipes operation=resume session_id={session_id}",
                file=self._err,
            )
            return
        gate["resume_launched_at"] = utc_now_iso()
        self.state.append_event(
            "recipe_gates:resume_launched",
            packet_id=gate["packet_id"],
            session_id=session_id,
            stage_name=gate["stage_name"],
            log=str(log_path),
        )
        self.state.ledger_append(
            "recipe_gate_resume_launched",
            packet_id=gate["packet_id"],
            session_id=session_id,
            stage_name=gate["stage_name"],
        )

    # -- one poll ------------------------------------------------------------------

    def poll_once(self) -> list[dict[str, Any]]:
        """One full poll: packetize new pending gates, forward answered ones.

        Discovery is a direct disk read (zero subprocesses — see COST MODEL
        in the module docstring); amplifier is invoked only to forward
        answers (approve/deny) and launch resume. Discovery failure is loud
        but non-fatal to the forwarding half — answered gates are still
        forwarded even when the recipes tool's state cannot be read this tick.
        """
        results: list[dict[str, Any]] = []
        gates = self._load_gates()

        try:
            self._packetize_new_gates(gates, results)
        except RecipeGateError as e:
            self.state.append_event("recipe_gates:error", error=str(e), phase="discovery")
            print(f"ERROR: recipe-gate discovery failed: {e}", file=self._err)
            results.append({"action": "error", "phase": "discovery", "error": str(e)})

        for key, gate in sorted(gates.items()):
            if gate.get("status") != "pending":
                continue
            self._forward_answer(key, gate, results)
            self._save_gates(gates)

        return results
