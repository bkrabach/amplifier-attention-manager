"""Tests for worker launch/observation.

Pure-logic tests always run. tmux-dependent tests use REAL tmux with trivial
commands (echo/sleep — no LLM). If tmux is absent they are skipped with a
LOUD explicit reason (per step-2 instructions: a pytest skip is acceptable
ONLY when `which tmux` is empty, and the reason must be unmissable).
"""

import shutil
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
