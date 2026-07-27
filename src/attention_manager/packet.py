"""Packet model — the escalation packet, validated against the on-disk contract.

The authoritative file-format contract lives in ``context/packet-schema.md``.
This module is the rich implementation used by the CLI, the app, and tests.
The worker-side modules (tool-request-decision, hooks-packet-approval) do NOT
import this module — they implement their own minimal IO against the same
documented contract.

Fail-loud principle (design decision D7): validation raises ``ValueError`` with
a specific message. No silent coercion, no defaults invented for missing data.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import Any

SCHEMA_VERSION = 1
MAX_CONTEXT_CHARS = 8000
VALID_KINDS = ("decision", "permission", "attractor-gate", "recipe-gate")
VALID_TIERS = ("batch", "today", "now")
VALID_TIMEOUT_ACTIONS = ("apply-option", "fail-loud")
PERMISSION_OPTION_IDS = ("allow", "deny")


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with Z suffix, second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_packet_id(now: datetime | None = None) -> str:
    """Sortable unique packet id: pkt-<UTC yyyymmdd-HHMMSS>-<4 hex>."""
    now = now or datetime.now(timezone.utc)
    return f"pkt-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass
class Option:
    id: str
    label: str
    consequence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "label": self.label}
        if self.consequence is not None:
            d["consequence"] = self.consequence
        return d


@dataclass
class Recommendation:
    option: str
    rationale: str | None = None
    confidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"option": self.option}
        if self.rationale is not None:
            d["rationale"] = self.rationale
        if self.confidence is not None:
            d["confidence"] = self.confidence
        return d


@dataclass
class OnTimeout:
    action: str  # "apply-option" | "fail-loud"
    option: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"action": self.action}
        if self.option is not None:
            d["option"] = self.option
        return d


@dataclass
class Urgency:
    tier: str = "batch"  # "batch" | "today" | "now"
    deadline: str | None = None  # ISO-8601
    on_timeout: OnTimeout | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"tier": self.tier}
        if self.deadline is not None:
            d["deadline"] = self.deadline
        if self.on_timeout is not None:
            d["on_timeout"] = self.on_timeout.to_dict()
        return d


@dataclass
class Source:
    kind: str  # "decision" | "permission" | "attractor-gate" | "recipe-gate"
    session_id: str | None = None
    work_unit: str | None = None
    muxplex_session: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind}
        for key in ("session_id", "work_unit", "muxplex_session"):
            value = getattr(self, key)
            if value is not None:
                d[key] = value
        return d


@dataclass
class Triage:
    handled_by: str | None = None
    rule_refs: list[str] | None = None
    why: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.handled_by is not None:
            d["handled_by"] = self.handled_by
        if self.rule_refs is not None:
            d["rule_refs"] = list(self.rule_refs)
        if self.why is not None:
            d["why"] = self.why
        return d


@dataclass
class Resolution:
    answer: str
    answered_by: str  # "human" | "manager" | "timeout-default"
    answered_at: str  # ISO-8601
    rationale: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "answer": self.answer,
            "answered_by": self.answered_by,
            "answered_at": self.answered_at,
        }
        if self.rationale is not None:
            d["rationale"] = self.rationale
        return d


@dataclass
class Packet:
    """One escalation packet. See context/packet-schema.md for the contract."""

    question: str
    options: list[Option]
    source: Source
    id: str = field(default_factory=new_packet_id)
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: int = SCHEMA_VERSION
    recommendation: Recommendation | None = None
    context: str = ""
    links: dict[str, Any] = field(default_factory=dict)
    urgency: Urgency = field(default_factory=Urgency)
    triage: Triage | None = None
    resolution: Resolution | None = None

    # -- validation ---------------------------------------------------------

    def option_ids(self) -> list[str]:
        return [o.id for o in self.options]

    def validate(self) -> None:
        """Validate against the packet contract. Raises ValueError (fail loud)."""
        _require(
            isinstance(self.id, str) and self.id.startswith("pkt-"),
            f"packet id must start with 'pkt-', got {self.id!r}",
        )
        _require(
            self.schema_version == SCHEMA_VERSION,
            f"unsupported schema_version {self.schema_version!r}; expected {SCHEMA_VERSION}",
        )
        _require(isinstance(self.created_at, str) and bool(self.created_at), "created_at is required (ISO-8601 string)")
        _require(self.source.kind in VALID_KINDS, f"source.kind {self.source.kind!r} not in {VALID_KINDS}")
        _require(
            isinstance(self.question, str) and bool(self.question.strip()), "question is required and must be non-empty"
        )

        _require(isinstance(self.options, list) and len(self.options) >= 1, "options must be a non-empty list")
        ids = self.option_ids()
        for opt in self.options:
            _require(isinstance(opt.id, str) and bool(opt.id), "every option requires a non-empty string 'id'")
            _require(isinstance(opt.label, str) and bool(opt.label), f"option {opt.id!r} requires a non-empty 'label'")
        _require(len(set(ids)) == len(ids), f"option ids must be unique, got {ids}")

        if self.source.kind == "permission":
            _require(
                len(self.options) == 2 and set(ids) == set(PERMISSION_OPTION_IDS),
                f"permission packets require exactly options {list(PERMISSION_OPTION_IDS)}, got {ids}",
            )

        if self.recommendation is not None:
            _require(
                self.recommendation.option in ids,
                f"recommendation.option {self.recommendation.option!r} is not one of the packet options {ids}",
            )

        _require(isinstance(self.context, str), "context must be a string")
        _require(
            len(self.context) <= MAX_CONTEXT_CHARS,
            f"context is {len(self.context)} chars; bounded contract maximum is {MAX_CONTEXT_CHARS}",
        )

        _require(self.urgency.tier in VALID_TIERS, f"urgency.tier {self.urgency.tier!r} not in {VALID_TIERS}")
        if self.urgency.on_timeout is not None:
            ot = self.urgency.on_timeout
            _require(
                ot.action in VALID_TIMEOUT_ACTIONS,
                f"urgency.on_timeout.action {ot.action!r} not in {VALID_TIMEOUT_ACTIONS}",
            )
            if ot.action == "apply-option":
                _require(ot.option is not None, "urgency.on_timeout.action 'apply-option' requires an 'option'")
                _require(
                    ot.option in ids, f"urgency.on_timeout.option {ot.option!r} is not one of the packet options {ids}"
                )

        if self.resolution is not None:
            res = self.resolution
            _require(res.answer in ids, f"resolution.answer {res.answer!r} is not one of the packet options {ids}")
            _require(bool(res.answered_by), "resolution.answered_by is required")
            _require(bool(res.answered_at), "resolution.answered_at is required")

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "source": self.source.to_dict(),
            "question": self.question,
            "options": [o.to_dict() for o in self.options],
            "context": self.context,
            "links": dict(self.links),
            "urgency": self.urgency.to_dict(),
        }
        if self.recommendation is not None:
            d["recommendation"] = self.recommendation.to_dict()
        if self.triage is not None:
            d["triage"] = self.triage.to_dict()
        if self.resolution is not None:
            d["resolution"] = self.resolution.to_dict()
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Packet:
        try:
            source_d = data["source"]
            urgency_d = data.get("urgency") or {}
            on_timeout_d = urgency_d.get("on_timeout")
            rec_d = data.get("recommendation")
            triage_d = data.get("triage")
            res_d = data.get("resolution")
            return cls(
                id=data["id"],
                created_at=data["created_at"],
                schema_version=data.get("schema_version", SCHEMA_VERSION),
                source=Source(
                    kind=source_d["kind"],
                    session_id=source_d.get("session_id"),
                    work_unit=source_d.get("work_unit"),
                    muxplex_session=source_d.get("muxplex_session"),
                ),
                question=data["question"],
                options=[
                    Option(id=o["id"], label=o["label"], consequence=o.get("consequence")) for o in data["options"]
                ],
                recommendation=(
                    Recommendation(
                        option=rec_d["option"],
                        rationale=rec_d.get("rationale"),
                        confidence=rec_d.get("confidence"),
                    )
                    if rec_d
                    else None
                ),
                context=data.get("context", ""),
                links=data.get("links") or {},
                urgency=Urgency(
                    tier=urgency_d.get("tier", "batch"),
                    deadline=urgency_d.get("deadline"),
                    on_timeout=(
                        OnTimeout(action=on_timeout_d["action"], option=on_timeout_d.get("option"))
                        if on_timeout_d
                        else None
                    ),
                ),
                triage=(
                    Triage(
                        handled_by=triage_d.get("handled_by"),
                        rule_refs=triage_d.get("rule_refs"),
                        why=triage_d.get("why"),
                    )
                    if triage_d
                    else None
                ),
                resolution=(
                    Resolution(
                        answer=res_d["answer"],
                        answered_by=res_d["answered_by"],
                        answered_at=res_d["answered_at"],
                        rationale=res_d.get("rationale"),
                    )
                    if res_d
                    else None
                ),
            )
        except KeyError as e:  # fail loud, name the missing field
            raise ValueError(f"packet dict missing required field: {e.args[0]}") from e

    @classmethod
    def from_json(cls, text: str) -> Packet:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"packet is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"packet JSON must be an object, got {type(data).__name__}")
        return cls.from_dict(data)
