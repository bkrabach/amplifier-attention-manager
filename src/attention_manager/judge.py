"""Judge execution — judge-gated finish lines (design §The Judge Requirement, step 4).

Contract (context/judge-contract.md): a judge is a COMMAND. Exit 0 = pass,
nonzero = fail, and it prints its reason to stdout/stderr either way.

Two entry points:

* :func:`run_judge` — the supervisor's finish-line evaluation. Runs the judge
  via ``bash -c`` with cwd = the worker's dir, exporting ``ATTENTION_HOME``,
  ``ATTENTION_QUEUE_DIR``, ``WORKER_LOG`` (abs path to worker.log) and
  ``WORKER_EXIT`` (the worker's exit code; empty string when the session died
  without an exit sentinel). Combined stdout+stderr is captured and returned
  (the supervisor persists it to ``workers/<session>/judge.log``).
* :func:`verify` — the broken-test protocol from the judge contract: run the
  judge against a known-good artifact (must exit 0) AND a deliberately broken
  one (must exit nonzero). The artifact path is exported as ``$ARTIFACT``.
  "A judge that never fails is decoration."

Fail loud (D7): a judge that cannot be run (timeout, spawn failure) is a
FAILED judge, never a skipped one — the caller must treat it as loop:failed.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .queue import ENV_QUEUE_DIR
from .state import ENV_HOME

DEFAULT_JUDGE_TIMEOUT_S = 60.0
OUTPUT_TAIL_CHARS = 400

ENV_WORKER_LOG = "WORKER_LOG"
ENV_WORKER_EXIT = "WORKER_EXIT"
ENV_ARTIFACT = "ARTIFACT"


def _tail(text: str, chars: int = OUTPUT_TAIL_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= chars else text[-chars:]


@dataclass
class JudgeResult:
    """Outcome of one judge run. ``passed`` is True ONLY on exit 0."""

    passed: bool
    reason: str  # "" on pass; why it failed otherwise (exit code / timeout / spawn)
    output: str  # combined stdout+stderr (best effort; may be partial on timeout)
    exit_code: int | None  # None when the judge never produced one (timeout/spawn)

    @property
    def output_tail(self) -> str:
        return _tail(self.output)


def run_judge(
    judge_cmd: str,
    cwd: Path,
    home: Path,
    queue_root: Path,
    worker_log: Path,
    worker_exit: int | None,
    timeout_s: float = DEFAULT_JUDGE_TIMEOUT_S,
) -> JudgeResult:
    """Run one judge command (``bash -c``) and classify the outcome.

    Never raises for judge-side problems: timeout, spawn failure, and nonzero
    exits all come back as ``passed=False`` with a specific reason — the
    caller MUST surface them loudly (loop:failed), never skip them.
    """
    env = {
        **os.environ,
        ENV_HOME: str(home),
        ENV_QUEUE_DIR: str(queue_root),
        ENV_WORKER_LOG: str(worker_log),
        ENV_WORKER_EXIT: "" if worker_exit is None else str(worker_exit),
    }
    try:
        proc = subprocess.run(
            ["bash", "-c", judge_cmd],
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        output = e.output or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return JudgeResult(
            passed=False,
            reason=f"judge timed out after {timeout_s:g}s",
            output=output,
            exit_code=None,
        )
    except OSError as e:
        return JudgeResult(passed=False, reason=f"judge spawn failed: {e}", output="", exit_code=None)

    output = proc.stdout or ""
    if proc.returncode == 0:
        return JudgeResult(passed=True, reason="", output=output, exit_code=0)
    return JudgeResult(
        passed=False,
        reason=f"judge exited {proc.returncode}",
        output=output,
        exit_code=proc.returncode,
    )


# -- broken-test protocol (judge verify) ----------------------------------------


@dataclass
class VerifyDirection:
    """One direction of the broken-test protocol."""

    direction: str  # "good" | "broken"
    artifact: str
    exit_code: int | None
    output: str
    ok: bool  # good: exit 0; broken: nonzero (incl. timeout/spawn = "failed", which counts)

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "artifact": self.artifact,
            "exit_code": self.exit_code,
            "output": _tail(self.output),
            "ok": self.ok,
        }


@dataclass
class VerifyResult:
    good: VerifyDirection
    broken: VerifyDirection

    @property
    def passed(self) -> bool:
        return self.good.ok and self.broken.ok

    def to_dict(self) -> dict:
        return {
            "good": self.good.to_dict(),
            "broken": self.broken.to_dict(),
            "verdict": "PASS" if self.passed else "FAIL",
        }


def _run_against_artifact(cmd: str, artifact: Path, timeout_s: float) -> tuple[int | None, str]:
    """Run the judge with $ARTIFACT set. Returns (exit_code, combined output)."""
    env = {**os.environ, ENV_ARTIFACT: str(artifact)}
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        output = e.output or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return None, output + f"\n[judge verify] timed out after {timeout_s:g}s"
    except OSError as e:
        return None, f"[judge verify] spawn failed: {e}"
    return proc.returncode, proc.stdout or ""


def verify(cmd: str, good: str | Path, broken: str | Path, timeout_s: float = DEFAULT_JUDGE_TIMEOUT_S) -> VerifyResult:
    """The broken-test protocol: judge must PASS the good artifact AND FAIL the broken one.

    The judge command receives the artifact path as ``$ARTIFACT``. Both paths
    must exist — verify is artifact-based by construction; a missing artifact
    is a caller error, reported loud (ValueError).
    """
    good_path = Path(good).expanduser()
    broken_path = Path(broken).expanduser()
    for label, path in ((" --good", good_path), (" --broken", broken_path)):
        if not path.exists():
            raise ValueError(f"judge verify:{label} artifact does not exist: {path}")

    good_code, good_out = _run_against_artifact(cmd, good_path, timeout_s)
    broken_code, broken_out = _run_against_artifact(cmd, broken_path, timeout_s)

    return VerifyResult(
        good=VerifyDirection(
            direction="good", artifact=str(good_path), exit_code=good_code, output=good_out, ok=good_code == 0
        ),
        broken=VerifyDirection(
            direction="broken",
            artifact=str(broken_path),
            exit_code=broken_code,
            output=broken_out,
            # A broken artifact must make the judge fail. Timeout/spawn failure
            # (exit_code None) is NOT a legitimate fail signal — the judge never
            # judged, so the direction is not verified.
            ok=broken_code is not None and broken_code != 0,
        ),
    )
