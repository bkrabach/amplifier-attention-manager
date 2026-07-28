"""Tests for notification sinks and the batching policy (window + max +
retry-on-sink-failure)."""

import json

import pytest

from attention_manager.notify import BatchItem, ConsoleSink, FileSink, NotificationBatcher, NtfySink, parse_sink


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FailingSink:
    name = "failing"

    def __init__(self):
        self.calls = 0
        self.fail = True

    def deliver(self, items: list[BatchItem]) -> None:
        self.calls += 1
        if self.fail:
            raise ConnectionError("sink down")


class RecordingSink:
    name = "recording"

    def __init__(self):
        self.batches: list[list[BatchItem]] = []

    def deliver(self, items: list[BatchItem]) -> None:
        self.batches.append(list(items))


class TestBatchingPolicy:
    def test_not_due_before_window(self):
        clock = FakeClock()
        batcher = NotificationBatcher(sink=RecordingSink(), batch_window_s=20, batch_max=10, clock=clock)
        batcher.enqueue("pkt-1", "q1")
        clock.advance(19.9)
        assert batcher.due() is False
        assert batcher.flush_if_due() is None

    def test_due_on_window_from_oldest(self):
        clock = FakeClock()
        sink = RecordingSink()
        batcher = NotificationBatcher(sink=sink, batch_window_s=20, batch_max=10, clock=clock)
        batcher.enqueue("pkt-1", "q1")
        clock.advance(15)
        batcher.enqueue("pkt-2", "q2")  # newer item does not reset the window
        clock.advance(5)
        assert batcher.due() is True
        outcome = batcher.flush_if_due()
        assert outcome is not None and outcome.delivered
        assert outcome.packet_ids == ["pkt-1", "pkt-2"]
        assert len(sink.batches) == 1  # ONE batch announcing both packets
        assert batcher.items == []

    def test_due_on_batch_max(self):
        clock = FakeClock()
        batcher = NotificationBatcher(sink=RecordingSink(), batch_window_s=1000, batch_max=3, clock=clock)
        for i in range(3):
            batcher.enqueue(f"pkt-{i}", "q")
        assert batcher.due() is True

    def test_empty_never_due(self):
        batcher = NotificationBatcher(sink=RecordingSink(), clock=FakeClock())
        assert batcher.due() is False

    def test_failure_keeps_items_and_retries(self):
        clock = FakeClock()
        sink = FailingSink()
        batcher = NotificationBatcher(sink=sink, batch_window_s=5, batch_max=10, clock=clock)
        batcher.enqueue("pkt-1", "q1")
        clock.advance(6)
        outcome = batcher.flush_if_due()
        assert outcome is not None and outcome.delivered is False
        assert "sink down" in (outcome.error or "")
        assert len(batcher.items) == 1  # retained, never dropped silently

        sink.fail = False
        outcome2 = batcher.flush_if_due()  # still due (oldest item still old)
        assert outcome2 is not None and outcome2.delivered is True
        assert outcome2.packet_ids == ["pkt-1"]
        assert batcher.items == []
        assert sink.calls == 2


class TestSinks:
    def test_file_sink_one_line_per_batch(self, tmp_path):
        path = tmp_path / "notify.jsonl"
        sink = FileSink(path)
        sink.deliver([BatchItem("pkt-1", "q1", "t", 0.0), BatchItem("pkt-2", "q2", "t", 0.0)])
        sink.deliver([BatchItem("pkt-3", "q3", "t", 0.0)])
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["count"] == 2
        assert [p["id"] for p in first["packets"]] == ["pkt-1", "pkt-2"]

    def test_console_sink_prints(self, capsys):
        ConsoleSink().deliver([BatchItem("pkt-1", "a question", "t", 0.0)])
        out = capsys.readouterr().out
        assert "pkt-1" in out and "1 packet(s) need you" in out

    def test_ntfy_sink_posts_title_and_body(self, monkeypatch):
        captured = {}

        class FakeResponse:
            def read(self):
                return b"ok"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["title"] = request.get_header("Title")
            captured["body"] = request.data.decode("utf-8")
            return FakeResponse()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        sink = NtfySink("https://ntfy.example/topic")
        sink.deliver([BatchItem("pkt-1", "x" * 200, "t", 0.0), BatchItem("pkt-2", "short", "t", 0.0)])
        assert captured["url"] == "https://ntfy.example/topic"
        assert captured["title"] == "2 packets need you"
        assert "pkt-1" in captured["body"] and "pkt-2" in captured["body"]
        assert "x" * 100 not in captured["body"]  # questions truncated


class TestParseSink:
    def test_console(self):
        assert isinstance(parse_sink("console"), ConsoleSink)

    def test_file(self, tmp_path):
        sink = parse_sink(f"file:{tmp_path}/n.jsonl")
        assert isinstance(sink, FileSink)

    def test_ntfy(self):
        assert isinstance(parse_sink("ntfy:https://ntfy.sh/topic"), NtfySink)

    def test_unknown_fails_loud(self):
        with pytest.raises(ValueError, match="unknown notification sink"):
            parse_sink("slack:whatever")

    def test_file_without_path_fails_loud(self):
        with pytest.raises(ValueError, match="requires a path"):
            parse_sink("file:")

    def test_ntfy_without_url_fails_loud(self):
        with pytest.raises(ValueError, match="http"):
            parse_sink("ntfy:not-a-url")
