"""CLI tests — invoke main() directly with ATTENTION_QUEUE_DIR isolation."""

import json

from attention_manager.cli import main
from attention_manager.packet import Option, Packet, Source
from attention_manager.queue import PacketQueue


def make_pending(queue_root) -> Packet:
    queue = PacketQueue(queue_root)
    packet = Packet(
        question="A or B?",
        options=[Option(id="A", label="Option A"), Option(id="B", label="Option B")],
        source=Source(kind="decision"),
    )
    queue.write(packet)
    return packet


class TestQueueCommands:
    def test_list_empty(self, queue_root, capsys):
        assert main(["queue", "list"]) == 0
        assert "queue empty" in capsys.readouterr().out

    def test_list_empty_json(self, queue_root, capsys):
        assert main(["--json", "queue", "list"]) == 0
        assert json.loads(capsys.readouterr().out) == []

    def test_list_shows_pending(self, queue_root, capsys):
        packet = make_pending(queue_root)
        assert main(["queue", "list"]) == 0
        out = capsys.readouterr().out
        assert packet.id in out
        assert "decision" in out
        assert "batch" in out
        assert "A or B?" in out

    def test_list_json_roundtrips(self, queue_root, capsys):
        packet = make_pending(queue_root)
        assert main(["--json", "queue", "list"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1 and data[0]["id"] == packet.id

    def test_show(self, queue_root, capsys):
        packet = make_pending(queue_root)
        assert main(["queue", "show", packet.id]) == 0
        out = capsys.readouterr().out
        assert packet.id in out and "Option A" in out

    def test_show_unknown_id_errors(self, queue_root, capsys):
        assert main(["queue", "show", "pkt-nope"]) == 1
        assert "error:" in capsys.readouterr().err

    def test_path(self, queue_root, capsys):
        assert main(["queue", "path"]) == 0
        assert str(queue_root) in capsys.readouterr().out


class TestAnswerCommand:
    def test_answer_happy_path(self, queue_root, capsys):
        packet = make_pending(queue_root)
        assert main(["answer", packet.id, "B", "--rationale", "safer"]) == 0
        assert "answered" in capsys.readouterr().out
        answered = PacketQueue(queue_root).get(packet.id)
        assert answered.resolution is not None
        assert answered.resolution.answer == "B"
        assert answered.resolution.rationale == "safer"

    def test_answer_json_output(self, queue_root, capsys):
        packet = make_pending(queue_root)
        assert main(["--json", "answer", packet.id, "A"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["resolution"]["answer"] == "A"

    def test_answer_bad_option_exits_1(self, queue_root, capsys):
        packet = make_pending(queue_root)
        assert main(["answer", packet.id, "Z"]) == 1
        err = capsys.readouterr().err
        assert "error:" in err and "Z" in err

    def test_answer_unknown_packet_exits_1(self, queue_root, capsys):
        assert main(["answer", "pkt-nope", "A"]) == 1
        assert "error:" in capsys.readouterr().err
