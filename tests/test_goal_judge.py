"""Unit tests for ``judges/goal-judge.sh`` non-agent mechanics.

A FAKE ``amplifier`` on PATH stands in for the agent evaluation, so these
exercise only the script's own contract: task-file validation, verdict parsing
(file-first, stdout fallback), fabrication handling, the missing-list output,
and the loud-fail path for unparseable verdicts. Contract: exit 0 = pass,
1 = fail (never a silent pass), 2 = usage error.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JUDGE = ROOT / "judges" / "goal-judge.sh"

MET_TRUE = (
    '{"met": true, "score_hint": 0.9, "missing": [], "fabrication": false, "reason": "all derived criteria evidenced"}'
)
MET_FALSE = (
    '{"met": false, "score_hint": 0.2, "missing": ["real render output", "producing command trace"],'
    ' "fabrication": false, "reason": "artifacts absent"}'
)
FABRICATED = (
    '{"met": true, "score_hint": 0.8, "missing": [], "fabrication": true, "reason": "artifact has no producing trace"}'
)


def _run_judge(tmp_path: Path, fake_body: str, task_file: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the judge with a fake ``amplifier`` prepended to PATH, cwd = a fake worker dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "amplifier"
    fake.write_text("#!/usr/bin/env bash\n" + fake_body + "\n", encoding="utf-8")
    fake.chmod(0o755)
    workdir = tmp_path / "worker"
    workdir.mkdir(exist_ok=True)
    if task_file is None:
        task_file = tmp_path / "task.txt"
        task_file.write_text("Build the widget; prove it renders.", encoding="utf-8")
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "GOAL_JUDGE_TIMEOUT": "30"}
    return subprocess.run(
        ["bash", str(JUDGE), str(task_file)],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_met_true_exits_zero_with_pass_reason(tmp_path):
    proc = _run_judge(tmp_path, f"printf '%s\\n' '{MET_TRUE}'")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS: goal-judge:" in proc.stdout


def test_met_false_exits_one_and_prints_missing_list(tmp_path):
    proc = _run_judge(tmp_path, f"printf '%s\\n' '{MET_FALSE}'")
    assert proc.returncode == 1
    assert "FAIL: goal-judge:" in proc.stdout
    assert "MISSING: real render output" in proc.stdout
    assert "MISSING: producing command trace" in proc.stdout


def test_fabrication_true_fails_even_when_met(tmp_path):
    proc = _run_judge(tmp_path, f"printf '%s\\n' '{FABRICATED}'")
    assert proc.returncode == 1
    assert "fabrication detected" in proc.stdout


def test_garbage_output_is_loud_fail(tmp_path):
    proc = _run_judge(tmp_path, "echo 'I feel great about this work, ship it!'")
    assert proc.returncode == 1
    assert "no parseable verdict" in proc.stdout


def test_agent_spawn_failure_is_loud_fail_not_silent_pass(tmp_path):
    proc = _run_judge(tmp_path, "echo boom >&2; exit 7")
    assert proc.returncode == 1
    assert "no parseable verdict" in proc.stdout
    assert "agent exit 7" in proc.stdout


def test_missing_task_file_exits_two(tmp_path):
    proc = _run_judge(tmp_path, f"printf '%s\\n' '{MET_TRUE}'", task_file=tmp_path / "nope.txt")
    assert proc.returncode == 2
    assert "task file missing or empty" in proc.stderr


def test_empty_task_file_exits_two(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    proc = _run_judge(tmp_path, f"printf '%s\\n' '{MET_TRUE}'", task_file=empty)
    assert proc.returncode == 2


def test_verdict_file_takes_priority_over_stdout(tmp_path):
    # Agent writes a valid verdict to $GOAL_JUDGE_VERDICT_FILE but prints garbage:
    # the file-first channel must still produce a pass.
    body = f"printf '%s' '{MET_TRUE}' > \"$GOAL_JUDGE_VERDICT_FILE\"; echo 'chatty non-JSON output'"
    proc = _run_judge(tmp_path, body)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASS: goal-judge:" in proc.stdout
