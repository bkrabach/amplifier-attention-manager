"""hooks-packet-approval — packet-writing ApprovalProvider (permission gates only).

Producer #2 of the escalation bus. Implements the kernel ``ApprovalProvider``
protocol (``request_approval(ApprovalRequest) -> ApprovalResponse``, see
amplifier-core ``interfaces.py``). Instead of blocking a console, it serializes
a packet (kind="permission", options exactly allow/deny) to the shared disk
queue and awaits resolution.

Registration: ``mount()`` registers the provider as the capability
``attention.packet_approval_provider``. In ``on_session_ready()`` (after ALL
modules have mounted) it additionally registers itself with the approval hook's
``approval.register_provider`` capability if the hooks-approval module is
composed — the same registration mechanism the app layer uses.

Gate policy (config ``gate_tools``): when the module config lists tool names in
``gate_tools`` (e.g. ``["bash"]``), mount() registers a ``tool:pre`` policy hook
that adds those tools to ``session_state["require_approval_tools"]`` — the
generic policy-module mechanism hooks-approval checks FIRST in
``_needs_approval()``, regardless of its own config. This is REQUIRED for
gating to work under amplifier-app-cli: the CLI unconditionally composes the
modes behavior (``_build_modes_behaviors()`` in the CLI's runtime/config.py)
AFTER the user bundle, and that behavior sets ``policy_driven_only: true`` on
hooks-approval, which makes hooks-approval's static ``rules``/``tools`` config
inert (``_needs_approval()`` returns False before ever reading them). The
session-state path is the only gating path that survives. Priority is -15:
after hooks-mode's tool moderation (-20, which REPLACES the set wholesale on
every evaluation), before hooks-approval's check (-10).

SELF-CONTAINED BRICK: this module implements its own minimal packet IO against
the on-disk file contract documented in ``context/packet-schema.md`` of the
amplifier-attention-manager repo. It deliberately does NOT import the
attention_manager package — it is installed standalone in worker sessions.

Timeout mapping (design D7, fail loud — resolves design open question 2):

- ``ApprovalRequest.timeout`` is honored ONLY when explicitly present (not
  None) and is recorded in the packet's ``urgency.deadline`` +
  ``urgency.on_timeout`` (action "fail-loud" — the kernel ApprovalRequest
  carries no default option, so the only honest timeout action is a loud deny).
- Absent a request timeout, module config ``max_wait_seconds`` governs
  (default None = wait indefinitely).
- On timeout the provider returns ``approved=False`` with an explicit
  fail-loud reason. The kernel's 300s->deny default is NEVER silently
  inherited: this provider does not raise TimeoutError for the approval hook
  to swallow into its generic default — it returns its own loud denial.
"""

# Amplifier module metadata
__amplifier_module_type__ = "hook"

import asyncio
import json
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from amplifier_core import ApprovalRequest, ApprovalResponse, HookResult

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_DIR = "~/.amplifier/attention/queue"
ENV_QUEUE_DIR = "ATTENTION_QUEUE_DIR"
# Worker↔packet linkage convention (context/packet-schema.md): the dispatcher
# exports the work-unit name into the worker's environment; when set, packets
# carry it as source.work_unit so `queue list`'s SOURCE column can name the
# worker that raised each packet.
ENV_WORK_UNIT = "ATTENTION_WORK_UNIT"
SCHEMA_VERSION = 1
MAX_CONTEXT_CHARS = 8000
DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_URGENCY_TIER = "today"  # a permission gate blocks a live worker turn

PROVIDER_CAPABILITY = "attention.packet_approval_provider"
APPROVAL_REGISTER_CAPABILITY = "approval.register_provider"

# session_state key hooks-approval checks FIRST in _needs_approval(), even when
# policy_driven_only is set (the modes behavior composed by amplifier-app-cli
# forces policy_driven_only=true, making all other gating config inert).
REQUIRE_APPROVAL_STATE_KEY = "require_approval_tools"
# tool:pre ordering: hooks-mode moderation (-20) REPLACES the session-state set
# wholesale on every evaluation; hooks-approval reads it at -10. We must write
# strictly between the two.
GATE_POLICY_PRIORITY = -15


# -- minimal packet IO against the file contract (context/packet-schema.md) ---


def _queue_root() -> Path:
    return Path(os.environ.get(ENV_QUEUE_DIR) or DEFAULT_QUEUE_DIR).expanduser()


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_packet_id() -> str:
    return f"pkt-{_now_utc():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _write_pending(root: Path, packet: dict[str, Any]) -> Path:
    pending = root / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / f"{packet['id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _read_resolution(root: Path, packet_id: str) -> dict[str, Any] | None:
    path = root / "answered" / f"{packet_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    resolution = data.get("resolution")
    if not resolution:
        raise ValueError(f"packet {packet_id} is in answered/ but has no resolution — corrupt queue state")
    return resolution


def _bounded_context(request: ApprovalRequest) -> str:
    """Serialize request metadata into the packet's bounded context field."""
    material = {
        "tool_name": request.tool_name,
        "risk_level": request.risk_level,
        "details": request.details,
    }
    text = json.dumps(material, indent=2, default=str)
    limit = MAX_CONTEXT_CHARS - 100  # leave room for the truncation marker
    if len(text) > limit:
        text = text[:limit] + "\n... [truncated to honor the bounded-context contract]"
    return text


# -- the provider ---------------------------------------------------------------


class PacketApprovalProvider:
    """ApprovalProvider that serializes permission gates as queue packets."""

    def __init__(self, config: dict[str, Any] | None = None, coordinator: Any = None):
        config = config or {}
        self._coordinator = coordinator
        raw_max_wait = config.get("max_wait_seconds")
        self.max_wait_seconds: float | None = float(raw_max_wait) if raw_max_wait is not None else None
        self.poll_interval_s = float(config.get("poll_interval_s", DEFAULT_POLL_INTERVAL_S))
        self.urgency_tier = config.get("urgency_tier", DEFAULT_URGENCY_TIER)

    def build_packet(self, request: ApprovalRequest, now: datetime | None = None) -> dict[str, Any]:
        now = now or _now_utc()
        urgency: dict[str, Any] = {"tier": self.urgency_tier}
        if request.timeout is not None:
            # Honor the declared request timeout and RECORD it on the packet.
            # ApprovalRequest carries no default option, so the only honest
            # timeout action is fail-loud (deny, with an explicit reason).
            urgency["deadline"] = _iso(now + timedelta(seconds=request.timeout))
            urgency["on_timeout"] = {"action": "fail-loud"}
        source: dict[str, Any] = {"kind": "permission"}
        links: dict[str, Any] = {}
        work_unit = os.environ.get(ENV_WORK_UNIT)
        if work_unit:
            source["work_unit"] = work_unit
        # Provenance + re-entry, when cheaply available (same getattr pattern
        # as tool-request-decision). Absent a coordinator session id, links
        # stays empty — links.resume is producer-dependent by contract.
        session_id = getattr(self._coordinator, "session_id", None)
        if session_id:
            source["session_id"] = str(session_id)
            links["resume"] = f"amplifier session resume {session_id}"
        return {
            "id": _new_packet_id(),
            "created_at": _iso(now),
            "schema_version": SCHEMA_VERSION,
            "source": source,
            "question": f"Allow or deny: {request.action}",
            "options": [
                {"id": "allow", "label": "Allow", "consequence": f"'{request.tool_name}' proceeds"},
                {"id": "deny", "label": "Deny", "consequence": f"'{request.tool_name}' is blocked"},
            ],
            "context": _bounded_context(request),
            "links": links,
            "urgency": urgency,
        }

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        root = _queue_root()
        packet = self.build_packet(request)
        packet_id = packet["id"]
        _write_pending(root, packet)
        logger.info("hooks-packet-approval wrote permission packet %s to %s", packet_id, root)

        # Effective wait: declared request timeout wins when explicitly present;
        # otherwise module config; otherwise indefinite.
        if request.timeout is not None:
            effective_wait: float | None = request.timeout
            timeout_reason = (
                f"attention-manager fail-loud: packet {packet_id} unanswered after the DECLARED "
                f"request timeout of {request.timeout:g}s; denying (gates fail closed, no silent default)"
            )
        else:
            effective_wait = self.max_wait_seconds
            timeout_reason = (
                f"attention-manager fail-loud: packet {packet_id} unanswered after "
                f"{self.max_wait_seconds}s; no declared default"
            )

        started = _now_utc()
        while True:
            resolution = _read_resolution(root, packet_id)
            if resolution is not None:
                answer = resolution["answer"]
                if answer == "allow":
                    return ApprovalResponse(approved=True, reason=resolution.get("rationale"))
                if answer == "deny":
                    return ApprovalResponse(approved=False, reason=resolution.get("rationale"))
                # The queue validates answers against packet options, so this
                # is unreachable through honest channels — fail closed, loudly.
                return ApprovalResponse(
                    approved=False,
                    reason=(
                        f"attention-manager fail-loud: packet {packet_id} resolved with non-permission "
                        f"answer {answer!r}; permission packets accept only allow/deny — denying"
                    ),
                )

            if effective_wait is not None and (_now_utc() - started).total_seconds() >= effective_wait:
                logger.warning(timeout_reason)
                return ApprovalResponse(approved=False, reason=timeout_reason)

            await asyncio.sleep(self.poll_interval_s)


# -- mount ------------------------------------------------------------------------


async def mount(coordinator: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mount the packet-writing approval provider.

    Registers the provider as a capability at mount time; integration with a
    composed hooks-approval module happens in on_session_ready() (after ALL
    modules have mounted, so mount order cannot matter).
    """
    config = config or {}
    provider = PacketApprovalProvider(config, coordinator=coordinator)
    coordinator.register_capability(PROVIDER_CAPABILITY, provider)
    logger.info("hooks-packet-approval mounted: registered capability %s", PROVIDER_CAPABILITY)

    gate_tools = [str(t) for t in config.get("gate_tools", [])]
    if gate_tools:
        _register_gate_policy(coordinator, gate_tools)

    return {
        "name": "hooks-packet-approval",
        "version": "0.1.0",
        "provides": [PROVIDER_CAPABILITY],
    }


def _register_gate_policy(coordinator: Any, gate_tools: list[str]) -> None:
    """Register the tool:pre policy hook that flags ``gate_tools`` for approval.

    Writes the tools into ``session_state["require_approval_tools"]`` — the
    generic policy-module key that hooks-approval's ``_needs_approval()`` checks
    FIRST, before (and regardless of) ``policy_driven_only``. This is the only
    gating path that works under amplifier-app-cli, which composes the modes
    behavior (``policy_driven_only: true``) after every user bundle.

    Runs at priority -15 on every tool:pre, because hooks-mode's moderation
    hook (-20) REPLACES the set wholesale on every evaluation; hooks-approval
    reads it at -10.

    It also RE-ASSERTS the packet provider with hooks-approval on every
    tool:pre: amplifier-app-cli registers its own console ApprovalProvider via
    ``approval.register_provider`` AFTER session initialization (i.e. after our
    ``on_session_ready()`` registration — see the CLI's session_runner.py),
    which would otherwise silently replace the packet provider. A console
    provider in a headless worker dies with "EOF when reading a line" and the
    gate fail-safes to deny instead of writing a packet. Re-registering here is
    an idempotent attribute set on the approval hook and guarantees the packet
    provider is in place at the exact moment hooks-approval consults it.
    """
    hooks = coordinator.get("hooks")
    if not hooks:
        logger.error(
            "hooks-packet-approval: gate_tools=%s configured but no hooks registry available — "
            "the permission gate CANNOT be enforced",
            gate_tools,
        )
        return

    async def gate_policy(event: str, data: dict[str, Any]) -> HookResult:
        session_state = getattr(coordinator, "session_state", None)
        if session_state is None:
            logger.error(
                "hooks-packet-approval: coordinator has no session_state — cannot flag %s for approval",
                gate_tools,
            )
            return HookResult(action="continue")
        # Read-modify-write: hooks-mode may have replaced the value with a new
        # set (or cleared it); always union our tools into whatever is current.
        current: set[str] = set(session_state.get(REQUIRE_APPROVAL_STATE_KEY) or ())
        current.update(gate_tools)
        session_state[REQUIRE_APPROVAL_STATE_KEY] = current

        # Re-assert the packet provider (the app layer may have replaced it
        # with a console provider after on_session_ready — see docstring).
        provider = coordinator.get_capability(PROVIDER_CAPABILITY)
        register = coordinator.get_capability(APPROVAL_REGISTER_CAPABILITY)
        if provider is not None and register is not None:
            register(provider)

        return HookResult(action="continue")

    hooks.register(
        "tool:pre",
        gate_policy,
        priority=GATE_POLICY_PRIORITY,
        name="packet-approval-gate-policy",
    )
    logger.info(
        "hooks-packet-approval: gate policy registered for tools %s (priority %d)",
        gate_tools,
        GATE_POLICY_PRIORITY,
    )


async def on_session_ready(coordinator: Any) -> None:
    """After full composition: register with hooks-approval if it is composed."""
    provider = coordinator.get_capability(PROVIDER_CAPABILITY)
    register = coordinator.get_capability(APPROVAL_REGISTER_CAPABILITY)
    if provider is None:
        logger.warning("hooks-packet-approval: own provider capability missing at on_session_ready")
        return
    if register is None:
        logger.info(
            "hooks-packet-approval: no %s capability (hooks-approval not composed); provider remains available as %s",
            APPROVAL_REGISTER_CAPABILITY,
            PROVIDER_CAPABILITY,
        )
        return
    register(provider)
    logger.info("hooks-packet-approval: registered provider with hooks-approval")
