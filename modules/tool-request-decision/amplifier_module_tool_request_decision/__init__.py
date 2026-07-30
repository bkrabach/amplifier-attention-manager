"""tool-request-decision — decision escalation to the attention-manager packet queue.

Producer #1 of the escalation bus. Workers call this tool at a genuine
multi-option human-decision point. It writes a packet (kind="decision") to the
shared disk queue, awaits resolution, and returns the chosen option + rationale
as its ToolResult.

SELF-CONTAINED BRICK: this module implements its own minimal packet IO against
the on-disk file contract documented in ``context/packet-schema.md`` of the
amplifier-attention-manager repo. It deliberately does NOT import the
attention_manager package — it is installed standalone in worker sessions.

Fail-loud (design D7): an unanswered packet never yields a silent default.
Timeouts either apply an EXPLICITLY DECLARED option (urgency.on_timeout) with a
loud note, or return an error ToolResult stating the packet went unanswered.
"""

# Amplifier module metadata
__amplifier_module_type__ = "tool"

import asyncio
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amplifier_core import ToolResult

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_DIR = "~/.amplifier/attention/queue"
ENV_QUEUE_DIR = "ATTENTION_QUEUE_DIR"
SCHEMA_VERSION = 1
MAX_CONTEXT_CHARS = 8000
VALID_TIERS = ("batch", "today", "now")
VALID_TIMEOUT_ACTIONS = ("apply-option", "fail-loud")
DEFAULT_MAX_WAIT_SECONDS = 3600.0
DEFAULT_POLL_INTERVAL_S = 1.0


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


def _parse_deadline(deadline: str) -> datetime:
    return datetime.fromisoformat(deadline)  # 3.11+ accepts the 'Z' suffix natively


# -- input validation ----------------------------------------------------------


def _validate_input(input_data: dict[str, Any]) -> str | None:
    """Return an error message (fail loud) or None if valid."""
    question = input_data.get("question")
    if not isinstance(question, str) or not question.strip():
        return "'question' is required and must be a non-empty string"

    options = input_data.get("options")
    if not isinstance(options, list) or len(options) < 2:
        return "'options' is required and must be a list of at least 2 options"
    ids: list[str] = []
    for opt in options:
        if not isinstance(opt, dict) or not opt.get("id") or not opt.get("label"):
            return f"every option requires 'id' and 'label'; got {opt!r}"
        ids.append(opt["id"])
    if len(set(ids)) != len(ids):
        return f"option ids must be unique, got {ids}"

    recommendation = input_data.get("recommendation")
    if recommendation is not None:
        if not isinstance(recommendation, dict) or recommendation.get("option") not in ids:
            return f"recommendation.option must be one of the option ids {ids}"

    context = input_data.get("context", "")
    if not isinstance(context, str):
        return "'context' must be a string"
    if len(context) > MAX_CONTEXT_CHARS:
        return f"'context' is {len(context)} chars; bounded contract maximum is {MAX_CONTEXT_CHARS}"

    urgency = input_data.get("urgency") or {}
    if not isinstance(urgency, dict):
        return "'urgency' must be an object"
    tier = urgency.get("tier", "batch")
    if tier not in VALID_TIERS:
        return f"urgency.tier {tier!r} not in {VALID_TIERS}"
    on_timeout = urgency.get("on_timeout")
    if on_timeout is not None:
        if not isinstance(on_timeout, dict) or on_timeout.get("action") not in VALID_TIMEOUT_ACTIONS:
            return f"urgency.on_timeout.action must be one of {VALID_TIMEOUT_ACTIONS}"
        if not urgency.get("deadline"):
            return "urgency.on_timeout requires urgency.deadline (a timeout policy without a deadline is meaningless)"
        if on_timeout["action"] == "apply-option":
            if on_timeout.get("option") not in ids:
                return f"urgency.on_timeout.option must be one of the option ids {ids} for action 'apply-option'"
        try:
            _parse_deadline(urgency["deadline"])
        except ValueError:
            return f"urgency.deadline {urgency['deadline']!r} is not a valid ISO-8601 timestamp"
    return None


# -- the tool -------------------------------------------------------------------


class RequestDecisionTool:
    """Escalate a multi-option decision to the human via the packet queue."""

    def __init__(self, coordinator: Any = None, config: dict[str, Any] | None = None):
        config = config or {}
        self._coordinator = coordinator
        self.max_wait_seconds = float(config.get("max_wait_seconds", DEFAULT_MAX_WAIT_SECONDS))
        self.poll_interval_s = float(config.get("poll_interval_s", DEFAULT_POLL_INTERVAL_S))

    @property
    def name(self) -> str:
        return "request_decision"

    @property
    def description(self) -> str:
        return (
            "Escalate a genuine multi-option human decision to the attention manager. "
            "Writes a re-entry packet to the shared queue and BLOCKS until it is "
            "answered (by the human or the manager). Returns the chosen option id, "
            "rationale, and who answered. Use ONLY at real decision points you cannot "
            "resolve from your instructions or the rulebook; provide enumerated options "
            "with consequences and your recommendation. The packet must be answerable "
            "cold — include the minimal facts needed to decide in 'context'."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "ONE decision, one sentence",
                },
                "options": {
                    "type": "array",
                    "minItems": 2,
                    "description": "Enumerated options; the answer will be one of these ids",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "consequence": {"type": "string"},
                        },
                        "required": ["id", "label"],
                    },
                },
                "recommendation": {
                    "type": "object",
                    "description": "Your recommended option with rationale and confidence",
                    "properties": {
                        "option": {"type": "string"},
                        "rationale": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": ["option"],
                },
                "context": {
                    "type": "string",
                    "description": "Bounded decision material — the minimal facts needed to decide cold (max 8000 chars)",
                },
                "urgency": {
                    "type": "object",
                    "properties": {
                        "tier": {"type": "string", "enum": ["batch", "today", "now"]},
                        "deadline": {"type": "string", "description": "ISO-8601 deadline"},
                        "on_timeout": {
                            "type": "object",
                            "description": "EXPLICIT declared timeout policy — never a silent default",
                            "properties": {
                                "action": {"type": "string", "enum": ["apply-option", "fail-loud"]},
                                "option": {"type": "string"},
                            },
                            "required": ["action"],
                        },
                    },
                },
            },
            "required": ["question", "options"],
        }

    async def execute(self, input_data: dict[str, Any]) -> ToolResult:
        error = _validate_input(input_data)
        if error is not None:
            return ToolResult(success=False, output=f"request_decision rejected (fail loud): {error}")

        root = _queue_root()
        urgency_in = input_data.get("urgency") or {}
        source: dict[str, Any] = {"kind": "decision"}
        links: dict[str, Any] = {}
        session_id = getattr(self._coordinator, "session_id", None)
        if session_id:
            source["session_id"] = str(session_id)
            # The re-entry link the packet contract promises: how a human (or
            # the manager) re-drives the blocked worker turn after answering.
            links["resume"] = f"amplifier session resume {session_id}"

        packet: dict[str, Any] = {
            "id": _new_packet_id(),
            "created_at": _iso(_now_utc()),
            "schema_version": SCHEMA_VERSION,
            "source": source,
            "question": input_data["question"],
            "options": input_data["options"],
            "context": input_data.get("context", ""),
            "links": links,
            "urgency": {
                "tier": urgency_in.get("tier", "batch"),
                **{k: v for k, v in urgency_in.items() if k != "tier"},
            },
        }
        if input_data.get("recommendation"):
            packet["recommendation"] = input_data["recommendation"]

        _write_pending(root, packet)
        logger.info("request_decision wrote packet %s to %s", packet["id"], root)

        return await self._await_resolution(root, packet)

    async def _await_resolution(self, root: Path, packet: dict[str, Any]) -> ToolResult:
        packet_id = packet["id"]
        urgency = packet.get("urgency") or {}
        on_timeout = urgency.get("on_timeout")
        deadline = _parse_deadline(urgency["deadline"]) if on_timeout else None
        started = _now_utc()

        while True:
            resolution = _read_resolution(root, packet_id)
            if resolution is not None:
                return ToolResult(
                    success=True,
                    output={
                        "answer": resolution["answer"],
                        "rationale": resolution.get("rationale"),
                        "answered_by": resolution["answered_by"],
                        "packet_id": packet_id,
                    },
                )

            now = _now_utc()
            if deadline is not None and on_timeout is not None:
                if now >= deadline:
                    if on_timeout["action"] == "apply-option":
                        option = on_timeout["option"]
                        logger.warning(
                            "packet %s unanswered at declared deadline; applying DECLARED default option %s",
                            packet_id,
                            option,
                        )
                        return ToolResult(
                            success=True,
                            output={
                                "answer": option,
                                "rationale": None,
                                "answered_by": "timeout-default",
                                "packet_id": packet_id,
                                "note": (
                                    f"LOUD: packet {packet_id} was NOT answered by a human or the manager. "
                                    f"The DECLARED on_timeout default option {option!r} was applied at the "
                                    f"declared deadline {urgency['deadline']}. Treat this answer accordingly."
                                ),
                            },
                        )
                    # action == "fail-loud"
                    return ToolResult(
                        success=False,
                        output=(
                            f"attention-manager fail-loud: packet {packet_id} unanswered at declared "
                            f"deadline {urgency['deadline']} with on_timeout action 'fail-loud'. "
                            f"No answer was fabricated. The packet remains pending in the queue."
                        ),
                    )
            else:
                elapsed = (now - started).total_seconds()
                if elapsed >= self.max_wait_seconds:
                    return ToolResult(
                        success=False,
                        output=(
                            f"attention-manager fail-loud: packet {packet_id} unanswered after "
                            f"{self.max_wait_seconds:g}s and no on_timeout default was declared. "
                            f"No answer was fabricated. The packet remains pending in the queue; "
                            f"the turn can be re-driven once it is answered."
                        ),
                    )

            await asyncio.sleep(self.poll_interval_s)


async def mount(coordinator: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mount the request_decision tool into the coordinator."""
    tool = RequestDecisionTool(coordinator=coordinator, config=config or {})
    await coordinator.mount("tools", tool, name=tool.name)
    logger.info("tool-request-decision mounted: registered 'request_decision'")
    return {
        "name": "tool-request-decision",
        "version": "0.1.0",
        "provides": ["request_decision"],
    }
