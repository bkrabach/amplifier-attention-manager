"""Test setup: make src/ and both standalone modules importable, isolate the queue."""

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for entry in (
    ROOT / "src",
    ROOT / "modules" / "tool-request-decision",
    ROOT / "modules" / "hooks-packet-approval",
):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from attention_manager.queue import PacketQueue  # noqa: E402


@pytest.fixture
def queue_root(tmp_path, monkeypatch) -> Path:
    """Isolated queue root, exported via ATTENTION_QUEUE_DIR for module IO."""
    root = tmp_path / "queue"
    monkeypatch.setenv("ATTENTION_QUEUE_DIR", str(root))
    return root


@pytest.fixture
def answer_when_pending():
    """Factory: background thread that answers the first pending packet using
    the ROOT queue library.

    This is the cross-implementation contract check: the worker-side modules
    write packets with their own minimal IO, and the root library answers the
    same files. If the two implementations diverge from the documented file
    contract, these tests break.
    """

    threads: list[threading.Thread] = []

    def _start(
        root: Path,
        option: str,
        rationale: str | None = None,
        answered_by: str = "human",
        timeout: float = 10.0,
    ) -> threading.Thread:
        def _run() -> None:
            queue = PacketQueue(root)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                pending = queue.list_pending()
                if pending:
                    queue.answer(pending[0].id, option, rationale=rationale, answered_by=answered_by)
                    return
                time.sleep(0.05)
            raise TimeoutError("no pending packet appeared to answer")

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        threads.append(thread)
        return thread

    yield _start

    for thread in threads:
        thread.join(timeout=10)
