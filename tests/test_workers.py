"""Tests for worker launch/observation.

Pure-logic tests always run. tmux-dependent tests use REAL tmux with trivial
commands (echo/sleep — no LLM). If tmux is absent they are skipped with a
LOUD explicit reason (per step-2 instructions: a pytest skip is acceptable
ONLY when `which tmux` is empty, and the reason must be unmissable).
"""

import shutil
import subprocess
import time
import uuid

import pytest

from attention_manager import workers

TMUX_PRESENT = shutil.which("tmux") is not None
requires_tmux = pytest.mark.skipif(
    not TMUX_PRESENT,
    reason=(
        "LOUD SKIP: tmux is NOT installed on this machine — tmux-dependent worker tests "
        "did NOT run. Install tmux to exercise launch/observe for real."
    ),
)


class TestPureLogic:
    def test_session_name_adds_prefix(self):
        assert workers.session_name("portfix") == "am-portfix"

    def test_session_name_keeps_existing_prefix(self):
        assert workers.session_name("am-portfix") == "am-portfix"

    def test_parse_exit_sentinel_found(self):
        assert workers.parse_exit_sentinel("blah\n__AM_WORKER_EXIT:0__\n") == 0
        assert workers.parse_exit_sentinel("__AM_WORKER_EXIT:17__") == 17

    def test_parse_exit_sentinel_last_match_wins(self):
        text = "__AM_WORKER_EXIT:1__\nretry\n__AM_WORKER_EXIT:0__\n"
        assert workers.parse_exit_sentinel(text) == 0

    def test_parse_exit_sentinel_absent(self):
        assert workers.parse_exit_sentinel("no sentinel here") is None
        assert workers.parse_exit_sentinel("") is None

    def test_extract_session_id(self):
        text = "starting...\nSession ID: 8d54a572-953f-4d8d-aee4-1ef92b8b90f9\nrunning"
        assert workers.extract_session_id(text) == "8d54a572-953f-4d8d-aee4-1ef92b8b90f9"

    def test_extract_session_id_absent(self):
        assert workers.extract_session_id("no id") is None

    # Regression: DTU eval S4 — the REAL amplifier CLI styles the line, so
    # pipe-pane captures ANSI codes BETWEEN "Session ID: " and the uuid
    # (verbatim bytes from the eval artifact, worker-am-w1.log line 9).
    REAL_ANSI_LINE = "\x1b[2mSession ID: \x1b[0m\x1b[2;93mcb818d5e-5cfa-42ac-87b0-8a14abe053b9\x1b[0m"

    def test_extract_session_id_real_ansi_styled_line(self):
        text = f"starting...\n{self.REAL_ANSI_LINE}\nrunning"
        assert workers.extract_session_id(text) == "cb818d5e-5cfa-42ac-87b0-8a14abe053b9"

    def test_observe_extracts_session_id_from_ansi_log(self, tmp_path, monkeypatch):
        """Through the REAL observe() extraction path (only liveness stubbed)."""
        monkeypatch.setattr(workers, "session_alive", lambda session: True)
        log = tmp_path / "worker.log"
        log.write_bytes(f"boot\r\n{self.REAL_ANSI_LINE}\r\nworking\r\n".encode())
        obs = workers.observe("am-ansi", log)
        assert obs.session_id == "cb818d5e-5cfa-42ac-87b0-8a14abe053b9"

    def test_sentinel_parses_amid_ansi_output_without_stripping(self):
        """Documented decision: parse_exit_sentinel does NOT strip ANSI — the
        sentinel is our own plain bash echo (contiguous bytes) plus a raw
        direct-append copy. Escape codes AROUND it must not matter."""
        text = "\x1b[2;93mstyled tail\x1b[0m\r\n__AM_WORKER_EXIT:0__\r\n"
        assert workers.parse_exit_sentinel(text) == 0

    def test_strip_ansi_removes_csi_and_osc(self):
        assert workers.strip_ansi("\x1b[2mdim\x1b[0m \x1b]0;title\x07plain") == "dim plain"

    def test_default_worker_cmd_with_bundle(self):
        cmd = workers.default_worker_cmd("do the thing", "git+https://example/bundle@main")
        assert cmd == "amplifier run -B git+https://example/bundle@main 'do the thing'"

    def test_default_worker_cmd_without_bundle(self):
        assert workers.default_worker_cmd("task") == "amplifier run task"

    def test_require_tmux_missing_fails_loud(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _: None)
        with pytest.raises(RuntimeError, match="tmux is not installed"):
            workers.require_tmux()


@requires_tmux
class TestWithRealTmux:
    @pytest.fixture
    def home(self, tmp_path):
        return tmp_path

    @pytest.fixture
    def unique_name(self):
        name = f"testwk-{uuid.uuid4().hex[:8]}"
        yield name
        workers.kill_session(workers.session_name(name))  # cleanup, best-effort

    def _wait_for(self, predicate, timeout_s=15.0, interval_s=0.2):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval_s)
        return False

    def test_launch_observe_exit0(self, home, unique_name):
        meta = workers.launch(unique_name, "echo hello-from-worker", home, task="say hello")
        session = meta["session"]
        assert session == f"am-{unique_name}"
        log_path = home / "workers" / session / "worker.log"
        meta_path = home / "workers" / session / "meta.json"
        assert meta_path.exists()

        assert self._wait_for(lambda: workers.observe(session, log_path).sentinel_seen), (
            f"exit sentinel never appeared in {log_path}: {log_path.read_text(encoding='utf-8', errors='replace')!r}"
        )
        obs = workers.observe(session, log_path)
        assert obs.exit_code == 0
        # pipe-pane captured the command's own output too
        assert "hello-from-worker" in log_path.read_text(encoding="utf-8", errors="replace")

    def test_launch_nonzero_exit_captured(self, home, unique_name):
        workers.launch(unique_name, "bash -c 'exit 7'", home, task="fail on purpose")
        session = f"am-{unique_name}"
        log_path = home / "workers" / session / "worker.log"
        assert self._wait_for(lambda: workers.observe(session, log_path).sentinel_seen)
        assert workers.observe(session, log_path).exit_code == 7

    def test_exit_style_command_still_writes_sentinel(self, home, unique_name):
        """Regression (UX round 2, Sam STRIKE #1): a bare `exit 3` worker
        command used to terminate the wrapper shell itself, skipping the
        sentinel — the session died with no exit line and dispatch's
        early-death check had nothing to warn on. The subshell in the launch
        wrapper contains the exit; the sentinel must always land."""
        workers.launch(unique_name, "exit 3", home, task="instant fail probe")
        session = f"am-{unique_name}"
        log_path = home / "workers" / session / "worker.log"
        assert self._wait_for(lambda: workers.observe(session, log_path).sentinel_seen), (
            f"exit sentinel never appeared for an exit-style command: "
            f"{log_path.read_text(encoding='utf-8', errors='replace')!r}"
        )
        assert workers.observe(session, log_path).exit_code == 3

    def test_launch_exports_work_unit_env(self, home, unique_name):
        """Worker↔packet linkage: launch() exports ATTENTION_WORK_UNIT=<name>
        into the tmux session so packet producers can stamp source.work_unit."""
        workers.launch(unique_name, "sleep 30", home)
        session = f"am-{unique_name}"
        tmux = workers.require_tmux()
        proc = subprocess.run(
            [tmux, "show-environment", "-t", f"={session}", workers.ENV_WORK_UNIT],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"show-environment failed: {proc.stderr}"
        assert proc.stdout.strip() == f"{workers.ENV_WORK_UNIT}={unique_name}"

    def test_session_dies_after_sleep_window(self, home, unique_name):
        workers.launch(unique_name, "true", home)
        session = f"am-{unique_name}"
        log_path = home / "workers" / session / "worker.log"
        assert self._wait_for(lambda: not workers.observe(session, log_path).alive, timeout_s=20)

    def test_launch_duplicate_session_fails_loud(self, home, unique_name):
        workers.launch(unique_name, "sleep 30", home)
        with pytest.raises(RuntimeError, match="already exists"):
            workers.launch(unique_name, "sleep 30", home)

    def test_observe_never_launched_session_is_dead(self, tmp_path):
        obs = workers.observe("am-never-launched-xyz", tmp_path / "nolog.log")
        assert obs.alive is False
        assert obs.sentinel_seen is False
        assert obs.exit_code is None
