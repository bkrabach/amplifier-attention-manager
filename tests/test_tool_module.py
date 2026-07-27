"""tool-request-decision module tests.

The happy-path tests are the cross-implementation CONTRACT tests: the module
writes packets with its own minimal IO, and the background thread answers via
the ROOT attention_manager queue library against the same files.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from amplifier_module_tool_request_decision import RequestDecisionTool
from amplifier_module_tool_request_decision import mount

from attention_manager.queue import PacketQueue

FAST = {"poll_interval_s": 0.05, "max_wait_seconds": 30}

OPTIONS = [
    {"id": "A", "label": "Migrate now", "consequence": "breaks two repos"},
    {"id": "B", "label": "Keep shim", "consequence": "buggy path ~2 weeks"},
]


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _out(result) -> dict:
    """Narrow ToolResult.output to the dict payload the tool returns on success."""
    assert isinstance(result.output, dict), f"expected dict output, got {result.output!r}"
    return result.output


class TestMount:
    async def test_mount_registers_tool(self):
        coordinator = MagicMock()
        coordinator.mount = AsyncMock()

        result = await mount(coordinator, {})

        coordinator.mount.assert_called_once()
        assert coordinator.mount.call_args[0][0] == "tools"
        assert result is not None
        assert result["name"] == "tool-request-decision"
        assert "request_decision" in result["provides"]

    async def test_tool_has_required_properties(self):
        coordinator = MagicMock()
        coordinator.mount = AsyncMock()
        await mount(coordinator, {})
        tool = coordinator.mount.call_args[0][1]
        assert tool.name == "request_decision"
        assert isinstance(tool.description, str) and tool.description
        assert isinstance(tool.input_schema, dict)
        assert callable(tool.execute)


class TestExecuteHappyPath:
    async def test_roundtrip_against_root_queue_lib(self, queue_root, answer_when_pending):
        """Contract cross-check: module IO writes, root lib answers."""
        tool = RequestDecisionTool(config=FAST)
        answer_when_pending(queue_root, "B", rationale="safer", answered_by="human")

        result = await tool.execute(
            {
                "question": "Migrate now, or keep the shim?",
                "options": OPTIONS,
                "recommendation": {"option": "B", "rationale": "no owner this week", "confidence": "medium"},
                "context": "minimal facts",
            }
        )

        assert result.success is True
        output = _out(result)
        assert output["answer"] == "B"
        assert output["rationale"] == "safer"
        assert output["answered_by"] == "human"

        # The packet the module wrote must be fully valid under the root model
        # and land in answered/ with resolution filled.
        packet = PacketQueue(queue_root).get(output["packet_id"])
        packet.validate()
        assert packet.source.kind == "decision"
        assert packet.resolution is not None and packet.resolution.answer == "B"

    async def test_session_id_captured_when_cheaply_available(self, queue_root, answer_when_pending):
        coordinator = MagicMock()
        coordinator.session_id = "sess-42"
        tool = RequestDecisionTool(coordinator=coordinator, config=FAST)
        answer_when_pending(queue_root, "A")

        result = await tool.execute({"question": "A or B?", "options": OPTIONS})
        packet = PacketQueue(queue_root).get(_out(result)["packet_id"])
        assert packet.source.session_id == "sess-42"


class TestValidation:
    async def test_missing_question_rejected(self, queue_root):
        result = await RequestDecisionTool(config=FAST).execute({"options": OPTIONS})
        assert result.success is False
        assert "question" in str(result.output)

    async def test_fewer_than_two_options_rejected(self, queue_root):
        result = await RequestDecisionTool(config=FAST).execute({"question": "?", "options": [OPTIONS[0]]})
        assert result.success is False
        assert "at least 2" in str(result.output)

    async def test_context_over_bound_rejected(self, queue_root):
        result = await RequestDecisionTool(config=FAST).execute(
            {"question": "?", "options": OPTIONS, "context": "x" * 8001}
        )
        assert result.success is False
        assert "8000" in str(result.output)

    async def test_recommendation_option_must_exist(self, queue_root):
        result = await RequestDecisionTool(config=FAST).execute(
            {"question": "?", "options": OPTIONS, "recommendation": {"option": "Z"}}
        )
        assert result.success is False
        assert "recommendation" in str(result.output)

    async def test_nothing_written_on_invalid_input(self, queue_root):
        await RequestDecisionTool(config=FAST).execute({"question": "?", "options": []})
        assert not (queue_root / "pending").exists() or not list((queue_root / "pending").glob("*.json"))


class TestTimeouts:
    async def test_fail_loud_timeout_without_declared_default(self, queue_root):
        tool = RequestDecisionTool(config={"poll_interval_s": 0.05, "max_wait_seconds": 0.2})
        result = await tool.execute({"question": "A or B?", "options": OPTIONS})
        assert result.success is False
        assert "fail-loud" in str(result.output)
        assert "unanswered" in str(result.output)
        # Packet stays pending — never silently resolved.
        assert len(PacketQueue(queue_root).list_pending()) == 1

    async def test_declared_apply_option_default_at_deadline(self, queue_root):
        tool = RequestDecisionTool(config={"poll_interval_s": 0.05, "max_wait_seconds": 30})
        deadline = _iso(datetime.now(timezone.utc) + timedelta(seconds=0.2))
        result = await tool.execute(
            {
                "question": "A or B?",
                "options": OPTIONS,
                "urgency": {
                    "tier": "today",
                    "deadline": deadline,
                    "on_timeout": {"action": "apply-option", "option": "B"},
                },
            }
        )
        assert result.success is True
        output = _out(result)
        assert output["answer"] == "B"
        assert output["answered_by"] == "timeout-default"
        assert "LOUD" in output["note"]

    async def test_declared_fail_loud_at_deadline(self, queue_root):
        tool = RequestDecisionTool(config={"poll_interval_s": 0.05, "max_wait_seconds": 30})
        deadline = _iso(datetime.now(timezone.utc) + timedelta(seconds=0.2))
        result = await tool.execute(
            {
                "question": "A or B?",
                "options": OPTIONS,
                "urgency": {"deadline": deadline, "on_timeout": {"action": "fail-loud"}},
            }
        )
        assert result.success is False
        assert "fail-loud" in str(result.output)

    async def test_on_timeout_without_deadline_rejected(self, queue_root):
        result = await RequestDecisionTool(config=FAST).execute(
            {
                "question": "A or B?",
                "options": OPTIONS,
                "urgency": {"on_timeout": {"action": "apply-option", "option": "B"}},
            }
        )
        assert result.success is False
        assert "deadline" in str(result.output)
