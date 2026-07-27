"""hooks-packet-approval module tests.

Happy-path tests are cross-implementation CONTRACT tests: the provider writes
packets with its own minimal IO; the background thread answers via the ROOT
attention_manager queue library against the same files.
"""

from typing import Any
from unittest.mock import MagicMock

from amplifier_core import ApprovalRequest
from amplifier_module_hooks_packet_approval import APPROVAL_REGISTER_CAPABILITY
from amplifier_module_hooks_packet_approval import GATE_POLICY_PRIORITY
from amplifier_module_hooks_packet_approval import PROVIDER_CAPABILITY
from amplifier_module_hooks_packet_approval import REQUIRE_APPROVAL_STATE_KEY
from amplifier_module_hooks_packet_approval import PacketApprovalProvider
from amplifier_module_hooks_packet_approval import mount
from amplifier_module_hooks_packet_approval import on_session_ready

from attention_manager.queue import PacketQueue

FAST = {"poll_interval_s": 0.05}


def make_request(
    tool_name: str = "bash",
    action: str = "Execute: rm -rf build/",
    details: dict[str, Any] | None = None,
    risk_level: str = "high",
    timeout: float | None = None,
) -> ApprovalRequest:
    return ApprovalRequest(
        tool_name=tool_name,
        action=action,
        details=details if details is not None else {"command": "rm -rf build/"},
        risk_level=risk_level,
        timeout=timeout,
    )


class TestMount:
    async def test_mount_registers_provider_capability(self):
        coordinator = MagicMock()
        result = await mount(coordinator, {})
        coordinator.register_capability.assert_called_once()
        name, provider = coordinator.register_capability.call_args[0]
        assert name == PROVIDER_CAPABILITY
        assert isinstance(provider, PacketApprovalProvider)
        assert result is not None and result["name"] == "hooks-packet-approval"

    async def test_on_session_ready_registers_with_approval_hook(self):
        provider = PacketApprovalProvider({})
        register = MagicMock()

        def get_capability(name):
            return {PROVIDER_CAPABILITY: provider, APPROVAL_REGISTER_CAPABILITY: register}.get(name)

        coordinator = MagicMock()
        coordinator.get_capability = get_capability
        await on_session_ready(coordinator)
        register.assert_called_once_with(provider)

    async def test_on_session_ready_tolerates_missing_approval_hook(self):
        provider = PacketApprovalProvider({})
        coordinator = MagicMock()
        coordinator.get_capability = lambda name: provider if name == PROVIDER_CAPABILITY else None
        await on_session_ready(coordinator)  # must not raise


class TestGatePolicy:
    """gate_tools config → tool:pre policy hook that survives policy_driven_only.

    amplifier-app-cli composes the modes behavior (policy_driven_only: true on
    hooks-approval) after every user bundle, which makes hooks-approval's
    static rules/tools config inert. The ONLY gating path that survives is
    session_state["require_approval_tools"], which hooks-approval checks first.
    These tests pin that mechanism.
    """

    @staticmethod
    def _mount_and_capture(config: dict[str, Any]):
        """Mount with config; return (coordinator, registered hook calls)."""
        coordinator = MagicMock()
        coordinator.session_state = {}
        hooks = MagicMock()
        coordinator.get = lambda name: hooks if name == "hooks" else None
        import asyncio

        asyncio.get_event_loop()
        return coordinator, hooks

    async def test_mount_without_gate_tools_registers_no_hook(self):
        coordinator = MagicMock()
        hooks = MagicMock()
        coordinator.get = lambda name: hooks if name == "hooks" else None
        await mount(coordinator, {})
        hooks.register.assert_not_called()

    async def test_mount_with_gate_tools_registers_tool_pre_policy(self):
        coordinator = MagicMock()
        coordinator.session_state = {}
        hooks = MagicMock()
        coordinator.get = lambda name: hooks if name == "hooks" else None

        await mount(coordinator, {"gate_tools": ["bash"]})

        hooks.register.assert_called_once()
        args, kwargs = hooks.register.call_args
        assert args[0] == "tool:pre"
        # Must run AFTER hooks-mode moderation (-20) and BEFORE hooks-approval (-10).
        assert kwargs["priority"] == GATE_POLICY_PRIORITY
        assert -20 < GATE_POLICY_PRIORITY < -10

    async def test_gate_policy_flags_tools_in_session_state(self):
        coordinator = MagicMock()
        coordinator.session_state = {}
        coordinator.get_capability = lambda name: None
        hooks = MagicMock()
        coordinator.get = lambda name: hooks if name == "hooks" else None
        await mount(coordinator, {"gate_tools": ["bash"]})
        handler = hooks.register.call_args[0][1]

        result = await handler("tool:pre", {"tool_name": "bash"})

        assert result.action == "continue"
        assert coordinator.session_state[REQUIRE_APPROVAL_STATE_KEY] == {"bash"}

    async def test_gate_policy_reasserts_packet_provider(self):
        # amplifier-app-cli registers its own console ApprovalProvider AFTER
        # on_session_ready (session_runner.py), replacing ours. The gate policy
        # must re-assert the packet provider on every tool:pre.
        provider = PacketApprovalProvider({})
        register = MagicMock()
        coordinator = MagicMock()
        coordinator.session_state = {}
        coordinator.get_capability = lambda name: {
            PROVIDER_CAPABILITY: provider,
            APPROVAL_REGISTER_CAPABILITY: register,
        }.get(name)
        hooks = MagicMock()
        coordinator.get = lambda name: hooks if name == "hooks" else None
        await mount(coordinator, {"gate_tools": ["bash"]})
        handler = hooks.register.call_args[0][1]

        await handler("tool:pre", {"tool_name": "bash"})

        register.assert_called_once_with(provider)

    async def test_gate_policy_preserves_existing_entries(self):
        # hooks-mode replaces the set wholesale (e.g. mode confirm tools);
        # our policy must UNION, never clobber.
        coordinator = MagicMock()
        coordinator.session_state = {REQUIRE_APPROVAL_STATE_KEY: {"write_file"}}
        hooks = MagicMock()
        coordinator.get = lambda name: hooks if name == "hooks" else None
        await mount(coordinator, {"gate_tools": ["bash"]})
        handler = hooks.register.call_args[0][1]

        await handler("tool:pre", {"tool_name": "bash"})

        assert coordinator.session_state[REQUIRE_APPROVAL_STATE_KEY] == {"write_file", "bash"}

    async def test_gate_policy_continues_without_session_state(self):
        coordinator = MagicMock(spec=["get", "register_capability"])  # no session_state attr
        hooks = MagicMock()
        coordinator.get = lambda name: hooks if name == "hooks" else None
        await mount(coordinator, {"gate_tools": ["bash"]})
        handler = hooks.register.call_args[0][1]

        result = await handler("tool:pre", {"tool_name": "bash"})
        assert result.action == "continue"  # must not raise


class TestRequestApproval:
    async def test_allow_roundtrip_against_root_queue_lib(self, queue_root, answer_when_pending):
        provider = PacketApprovalProvider(FAST)
        answer_when_pending(queue_root, "allow", rationale="looks safe")

        response = await provider.request_approval(make_request())

        assert response.approved is True
        assert response.reason == "looks safe"

        # The packet the module wrote must be fully valid under the root model.
        queue = PacketQueue(queue_root)
        packets = [queue.get(p.stem) for p in (queue_root / "answered").glob("pkt-*.json")]
        assert len(packets) == 1
        packet = packets[0]
        packet.validate()
        assert packet.source.kind == "permission"
        assert sorted(packet.option_ids()) == ["allow", "deny"]
        assert "rm -rf build/" in packet.context

    async def test_deny_roundtrip(self, queue_root, answer_when_pending):
        provider = PacketApprovalProvider(FAST)
        answer_when_pending(queue_root, "deny", rationale="too risky")

        response = await provider.request_approval(make_request())
        assert response.approved is False
        assert response.reason == "too risky"

    async def test_fail_loud_timeout_from_config(self, queue_root):
        provider = PacketApprovalProvider({"poll_interval_s": 0.05, "max_wait_seconds": 0.2})
        response = await provider.request_approval(make_request())
        assert response.approved is False
        assert response.reason is not None
        assert "fail-loud" in response.reason
        assert "no declared default" in response.reason
        # Packet stays pending — never silently resolved.
        assert len(PacketQueue(queue_root).list_pending()) == 1

    async def test_declared_request_timeout_recorded_and_honored(self, queue_root):
        provider = PacketApprovalProvider(FAST)
        response = await provider.request_approval(make_request(timeout=0.2))

        assert response.approved is False
        assert response.reason is not None
        assert "fail-loud" in response.reason and "DECLARED" in response.reason

        # The declared timeout must be RECORDED on the packet (still pending).
        pending = PacketQueue(queue_root).list_pending()
        assert len(pending) == 1
        packet = pending[0]
        assert packet.urgency.deadline is not None
        assert packet.urgency.on_timeout is not None
        assert packet.urgency.on_timeout.action == "fail-loud"

    async def test_indefinite_wait_by_default_config(self, queue_root, answer_when_pending):
        # max_wait_seconds default is None (indefinite) — an eventual answer resolves it.
        provider = PacketApprovalProvider(FAST)
        assert provider.max_wait_seconds is None
        answer_when_pending(queue_root, "allow")
        response = await provider.request_approval(make_request())
        assert response.approved is True

    async def test_permission_packet_bounded_context(self, queue_root):
        provider = PacketApprovalProvider({"poll_interval_s": 0.05, "max_wait_seconds": 0.2})
        big_details = {"command": "x" * 20000}
        await provider.request_approval(make_request(details=big_details))
        packet = PacketQueue(queue_root).list_pending()[0]
        packet.validate()  # would fail loud if context exceeded 8000 chars
        assert "truncated" in packet.context
