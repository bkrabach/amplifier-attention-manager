"""PacketQueue behavior: atomic writes, answer flow, awaiting."""

import threading
import time
from typing import Any

import pytest

from attention_manager.packet import Option
from attention_manager.packet import Packet
from attention_manager.packet import Source
from attention_manager.queue import PacketQueue


def make_packet(**overrides) -> Packet:
    kwargs: dict[str, Any] = dict(
        question="A or B?",
        options=[Option(id="A", label="Option A"), Option(id="B", label="Option B")],
        source=Source(kind="decision"),
    )
    kwargs.update(overrides)
    return Packet(**kwargs)


class TestWrite:
    def test_write_creates_pending_file_and_no_tmp_leftover(self, queue_root):
        queue = PacketQueue(queue_root)
        packet = make_packet()
        path = queue.write(packet)
        assert path == queue_root / "pending" / f"{packet.id}.json"
        assert path.exists()
        assert list(queue_root.rglob("*.tmp")) == []

    def test_write_rejects_invalid_packet(self, queue_root):
        queue = PacketQueue(queue_root)
        with pytest.raises(ValueError, match="question"):
            queue.write(make_packet(question=""))
        assert list((queue_root / "pending").glob("*.json")) == []

    def test_env_var_controls_root(self, queue_root):
        queue = PacketQueue()  # no explicit root -> ATTENTION_QUEUE_DIR from fixture
        assert queue.root == queue_root

    def test_list_pending_sorted_and_skips_tmp(self, queue_root):
        queue = PacketQueue(queue_root)
        ids = []
        for hexpart in ("bbbb", "aaaa"):
            packet = make_packet(id=f"pkt-20260726-12000{len(ids)}-{hexpart}")
            queue.write(packet)
            ids.append(packet.id)
        (queue_root / "pending" / "pkt-x.json.tmp").write_text("{", encoding="utf-8")
        listed = [p.id for p in queue.list_pending()]
        assert listed == sorted(ids)

    def test_list_pending_fails_loud_on_corrupt_file(self, queue_root):
        queue = PacketQueue(queue_root)
        queue.dir("pending")
        (queue_root / "pending" / "pkt-corrupt.json").write_text("{nope", encoding="utf-8")
        with pytest.raises(ValueError, match="malformed packet file"):
            queue.list_pending()


class TestAnswer:
    def test_answer_moves_to_answered_with_resolution(self, queue_root):
        queue = PacketQueue(queue_root)
        packet = make_packet()
        queue.write(packet)

        answered = queue.answer(packet.id, "B", rationale="safer")
        assert answered.resolution is not None
        assert answered.resolution.answer == "B"
        assert answered.resolution.rationale == "safer"
        assert answered.resolution.answered_by == "human"
        assert answered.resolution.answered_at

        assert not (queue_root / "pending" / f"{packet.id}.json").exists()
        on_disk = queue.get(packet.id)
        assert on_disk.resolution is not None and on_disk.resolution.answer == "B"
        assert queue.locate(packet.id)[0] == "answered"

    def test_answer_rejects_option_not_in_packet(self, queue_root):
        queue = PacketQueue(queue_root)
        packet = make_packet()
        queue.write(packet)
        with pytest.raises(ValueError, match="not one of packet"):
            queue.answer(packet.id, "Z")
        # packet untouched, still pending
        assert queue.locate(packet.id)[0] == "pending"

    def test_answer_rejects_non_pending_packet(self, queue_root):
        queue = PacketQueue(queue_root)
        packet = make_packet()
        queue.write(packet)
        queue.answer(packet.id, "A")
        with pytest.raises(ValueError, match="not pending"):
            queue.answer(packet.id, "B")

    def test_answer_unknown_id_raises_keyerror(self, queue_root):
        queue = PacketQueue(queue_root)
        with pytest.raises(KeyError, match="pkt-nope"):
            queue.answer("pkt-nope", "A")


class TestAwaitResolution:
    def test_await_returns_after_background_answer(self, queue_root, answer_when_pending):
        queue = PacketQueue(queue_root)
        packet = make_packet()
        queue.write(packet)

        thread = answer_when_pending(queue_root, "A", rationale="go")
        resolution = queue.await_resolution(packet.id, poll_s=0.05, timeout_s=5)
        thread.join(timeout=5)
        assert resolution.answer == "A"
        assert resolution.rationale == "go"

    def test_await_times_out_loudly(self, queue_root):
        queue = PacketQueue(queue_root)
        packet = make_packet()
        queue.write(packet)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match=packet.id):
            queue.await_resolution(packet.id, poll_s=0.05, timeout_s=0.2)
        assert time.monotonic() - started < 2

    async def test_await_async_returns_after_background_answer(self, queue_root):
        queue = PacketQueue(queue_root)
        packet = make_packet()
        queue.write(packet)

        def _answer_later():
            time.sleep(0.2)
            PacketQueue(queue_root).answer(packet.id, "B")

        thread = threading.Thread(target=_answer_later, daemon=True)
        thread.start()
        resolution = await queue.await_resolution_async(packet.id, poll_s=0.05, timeout_s=5)
        thread.join(timeout=5)
        assert resolution.answer == "B"

    async def test_await_async_times_out_loudly(self, queue_root):
        queue = PacketQueue(queue_root)
        packet = make_packet()
        queue.write(packet)
        with pytest.raises(TimeoutError, match=packet.id):
            await queue.await_resolution_async(packet.id, poll_s=0.05, timeout_s=0.2)
