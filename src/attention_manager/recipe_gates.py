"""Recipe-gate poller — producer #4 of the escalation bus (design D9).

Recipes' staged approval gates are bridged by an EXTERNAL ADAPTER: the poller
shells out to the installed ``amplifier`` CLI's tool-invoke path (never patches
the recipes tool), same rationale family as D8 (stdlib-only root package,
reuse the environment's amplifier install, all state on disk):

* discover gates:  ``amplifier tool invoke recipes operation=approvals -o json``
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

Observed output shape (verified against the real CLI on this machine):

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
        # dir determines the project); invoke from a stable, explicit cwd.
        self.cwd = Path(cwd).expanduser() if cwd is not None else Path.cwd()
        self.timeout_s = timeout_s
        self._err = err or sys.stderr
        self.gates_path = self.state.home / STATE_FILENAME

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
        payload = self._invoke(["operation=approvals"])
        approvals = payload.get("pending_approvals")
        if not isinstance(approvals, list):
            raise RecipeGateError(f"approvals payload missing 'pending_approvals' list: {payload!r}")
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

        Discovery failure (approvals invoke) is loud but non-fatal to the
        forwarding half — answered gates are still forwarded even when the
        recipes tool cannot list approvals this tick.
        """
        results: list[dict[str, Any]] = []
        gates = self._load_gates()

        try:
            self._packetize_new_gates(gates, results)
        except RecipeGateError as e:
            self.state.append_event("recipe_gates:error", error=str(e), phase="approvals")
            print(f"ERROR: recipes approvals poll failed: {e}", file=self._err)
            results.append({"action": "error", "phase": "approvals", "error": str(e)})

        for key, gate in sorted(gates.items()):
            if gate.get("status") != "pending":
                continue
            self._forward_answer(key, gate, results)
            self._save_gates(gates)

        return results
