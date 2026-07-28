"""workunit CLI tests: arg handling, missing-extra loud error, and (when the
real loop-pipeline package is importable) a full engine integration roundtrip.

The integration test uses the REAL amplifier-module-loop-pipeline engine with
the verified minimal gate.dot and a background thread answering via the root
queue library. It is skipped (not failed) when the optional dependency is not
installed — CI must not require it; scripts/local_attractor_smoke.sh and the
DTU are the loud verifiers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from attention_manager import cli
from attention_manager.queue import PacketQueue

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_DOT = REPO_ROOT / "evals" / "pipelines" / "gate.dot"


@pytest.fixture
def attention_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("ATTENTION_HOME", str(home))
    return home


# -- arg handling ---------------------------------------------------------------


def test_parser_workunit_run_defaults():
    args = cli.build_parser().parse_args(["workunit", "run", "pipeline.dot"])
    assert args.command == "workunit"
    assert args.workunit_command == "run"
    assert args.pipeline == "pipeline.dot"
    assert args.name is None
    assert args.logs_dir is None


def test_parser_workunit_run_flags():
    args = cli.build_parser().parse_args(["workunit", "run", "p.dot", "--name", "portfix", "--logs-dir", "/tmp/logs"])
    assert args.name == "portfix"
    assert args.logs_dir == "/tmp/logs"


def test_missing_pipeline_file_fails_loud(attention_home, queue_root, capsys):
    rc = cli.main(["workunit", "run", "/nonexistent/pipeline.dot"])
    assert rc == 1
    assert "pipeline file not found" in capsys.readouterr().err


# -- missing optional dependency --------------------------------------------------


def test_missing_extra_loud_actionable_error(attention_home, queue_root, monkeypatch, capsys):
    """Simulated ImportError: the CLI must name the [attractor] extra, not traceback."""
    for key in [
        k
        for k in sys.modules
        if k == "amplifier_module_loop_pipeline" or k.startswith("amplifier_module_loop_pipeline.")
    ]:
        monkeypatch.delitem(sys.modules, key)
    monkeypatch.setitem(sys.modules, "amplifier_module_loop_pipeline", None)
    monkeypatch.setitem(sys.modules, "amplifier_module_loop_pipeline.dot_parser", None)

    rc = cli.main(["workunit", "run", str(GATE_DOT), "--name", "wu-missing"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "amplifier-attention-manager[attractor]" in err


# -- real-engine integration (skipped when the extra isn't installed) -------------


def test_gate_dot_roundtrip_real_engine(attention_home, queue_root, answer_when_pending, tmp_path, monkeypatch, capsys):
    pytest.importorskip("amplifier_module_loop_pipeline")

    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)  # tool nodes write A.txt/R.txt into process cwd

    answer_when_pending(queue_root, "A", rationale="integration test", timeout=60.0)
    rc = cli.main(["workunit", "run", str(GATE_DOT), "--name", "itest-gate"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "itest-gate" in out and "success" in out

    # the answer routed the [A] Approve edge
    assert (workdir / "A.txt").is_file()
    assert not (workdir / "R.txt").exists()

    # the packet on disk is a well-formed attractor-gate packet
    queue = PacketQueue(queue_root)
    answered = list((queue_root / "answered").glob("pkt-*.json"))
    assert len(answered) == 1
    packet = queue.get(answered[0].stem)
    assert packet.source.kind == "attractor-gate"
    assert packet.source.work_unit == "itest-gate"
    assert packet.option_ids() == ["A", "R"]
    assert "stage: gate" in packet.context
    assert packet.resolution is not None and packet.resolution.answer == "A"

    # events + ledger record the gate and the finish
    events_lines = (attention_home / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in events_lines]
    names = [e["event"] for e in events]
    assert "gate:packet_created" in names
    assert "gate:answered" in names
    finished = [e for e in events if e["event"] == "workunit:finished"]
    assert finished and finished[0]["name"] == "itest-gate" and finished[0]["status"] == "success"

    ledger_files = list((attention_home / "ledger").glob("*.jsonl"))
    assert len(ledger_files) == 1
    ledger = [json.loads(line) for line in ledger_files[0].read_text(encoding="utf-8").splitlines()]
    assert any(e["kind"] == "workunit_finished" and e["name"] == "itest-gate" for e in ledger)

    # default logs root: $ATTENTION_HOME/workunits/<name>/
    assert (attention_home / "workunits" / "itest-gate").is_dir()


def test_gate_dot_reject_path_exits_nonzero_free_of_files(
    attention_home, queue_root, answer_when_pending, tmp_path, monkeypatch, capsys
):
    """Answering R routes the reject edge — still a SUCCESS outcome (the pipeline
    completed), proving option routing is answer-driven, not first-edge fallback."""
    pytest.importorskip("amplifier_module_loop_pipeline")

    workdir = tmp_path / "work-r"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    answer_when_pending(queue_root, "R", timeout=60.0)
    rc = cli.main(["workunit", "run", str(GATE_DOT), "--name", "itest-reject"])
    assert rc == 0  # the pipeline itself completes on either path
    assert (workdir / "R.txt").is_file()
    assert not (workdir / "A.txt").exists()
