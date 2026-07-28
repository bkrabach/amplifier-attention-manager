"""Attractor gate producer — Interviewer publishing hexagon gates to the packet queue.

Design: docs/designs/attention-manager.md §Tier 2 producer #3 and decision D3
("attractor = work-unit format, not the brain"). A hexagon gate already IS a
re-entry packet: node prompt = question, outgoing edge labels = enumerated
options, ``[A]``/``[R]`` accelerators = affordances. This module maps the
engine's ``Question`` to a ``kind="attractor-gate"`` packet on the shared disk
queue and maps the human's resolution back to an ``Answer``.

Integration contract (verified against amplifier-module-loop-pipeline):

* The ``Interviewer`` Protocol is sync ``ask``/``ask_multiple``/``inform``.
  ``async_ask`` is DUCK-TYPED, not in the Protocol: the engine's human handler
  does ``if hasattr(interviewer, "async_ask"): return await
  interviewer.async_ask(question)``. We implement all four.
* Hexagon gates deliver ``type=MULTIPLE_CHOICE`` with one ``Option(key,
  label)`` per distinct outgoing edge label (key = parsed accelerator, e.g.
  ``"[A] Approve"`` → ``"A"``), or ``type=CONFIRMATION`` with NO options when
  the gate has no outgoing edges. ``Question.stage`` (node id) is the ONLY
  correlation handle.
* CRITICAL FOOTGUN (loop-pipeline ``handlers/human.py::_resolve_selection``):
  an unrecognized answer string silently falls back to the FIRST choice — a
  silent misroute. We therefore validate the resolved answer against the
  packet's declared options before returning, and raise loudly otherwise.
* FREEFORM questions are NOT supported in v1: the packet contract requires
  enumerated options (the cold-reader test). Hexagon gates with labeled edges
  are the design target; a freeform gate raises a loud error naming the
  gate/stage. See ``evals/scenarios/scenario-7-attractor-gate.md``.

All imports of the loop-pipeline package are lazy — the core package stays
stdlib-only. Install the extra with ``pip install
'amplifier-attention-manager[attractor]'`` to run work units.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

from .packet import OnTimeout, Option, Packet, Resolution, Source, Urgency
from .queue import PacketQueue

logger = logging.getLogger(__name__)

INSTALL_HINT = (
    "the attractor integration requires the 'amplifier-module-loop-pipeline' package. "
    "Install it with: pip install 'amplifier-attention-manager[attractor]'  (or directly: "
    "pip install 'amplifier-module-loop-pipeline @ git+https://github.com/microsoft/"
    "amplifier-bundle-attractor@main#subdirectory=modules/loop-pipeline')"
)


def import_loop_pipeline(submodule: str | None = None) -> ModuleType:
    """Lazily import the loop-pipeline package (or one of its submodules).

    Raises ImportError with a loud, actionable install hint when the optional
    dependency is missing — never a bare ModuleNotFoundError.
    """
    name = "amplifier_module_loop_pipeline" + (f".{submodule}" if submodule else "")
    try:
        return importlib.import_module(name)
    except ImportError as e:
        raise ImportError(f"cannot import {name!r}: {e}. {INSTALL_HINT}") from e


def _question_type_value(question: Any) -> str:
    """The QuestionType as its string value ('multiple_choice', ...), duck-typed."""
    qtype = getattr(question, "type", None)
    value = getattr(qtype, "value", None)
    return value if isinstance(value, str) else str(qtype)


class PacketInterviewer:
    """Interviewer implementation that publishes gates as packets on the queue.

    Satisfies the loop-pipeline ``Interviewer`` Protocol (sync ``ask``,
    ``ask_multiple``, ``inform``) PLUS the duck-typed ``async_ask`` the engine
    prefers. Each question becomes one ``kind="attractor-gate"`` packet in
    ``pending/``; the call blocks (or awaits) until the packet is answered via
    the normal queue contract, then returns ``Answer(value=<option id>)``.

    Timeouts are declared, never silent (D7): ``urgency.deadline`` +
    ``on_timeout: fail-loud`` are recorded on the packet ONLY when the
    Question carries an explicit ``timeout_seconds``; on expiry the await
    raises TimeoutError and the packet stays pending — nothing fabricates an
    answer. By default gates wait indefinitely (a gate may wait hours; the
    engine checks wall-clock only between steps, so an awaiting gate is safe).
    """

    def __init__(
        self,
        queue: PacketQueue,
        work_unit_name: str,
        events_emitter: Callable[..., Any] | None = None,
        poll_s: float = 1.0,
    ):
        self.queue = queue
        self.work_unit_name = work_unit_name
        # Called as emitter(event_name, **fields) — SupervisorState.append_event
        # has exactly this shape. None → informational messages fall back to
        # the module logger (still durable via logging config, never lost to a
        # bare print), but workunit.py always passes state.append_event.
        self.events_emitter = events_emitter
        self.poll_s = poll_s

    # -- packet construction (Question → Packet) -----------------------------

    def build_packet(self, question: Any) -> Packet:
        """Map an engine Question to a validated attractor-gate Packet.

        Raises ValueError (loud, naming the gate/stage) for FREEFORM
        questions and for gates that cannot yield >=2 options.
        """
        qtype = _question_type_value(question)
        stage = getattr(question, "stage", "") or ""
        gate = f"gate stage {stage!r} in work unit {self.work_unit_name!r}"

        if qtype == "freeform":
            raise ValueError(
                f"{gate} asks a FREEFORM question; freeform gates are not supported in v1 — "
                "the packet contract requires enumerated options (cold-reader test). "
                "Use a hexagon gate with labeled outgoing edges instead."
            )
        if qtype in ("confirmation", "yes_no"):
            # No labeled edges → the engine sends a bare confirmation. Synthesize
            # the yes/no pair; the returned ids "yes"/"no" are exactly the
            # AnswerValue.YES/.NO string values the engine's resolution accepts.
            options = [Option(id="yes", label="Yes"), Option(id="no", label="No")]
        elif qtype == "multiple_choice":
            options = []
            seen: set[str] = set()
            for opt in question.options:
                if opt.key in seen:
                    # Duplicate accelerator keys are pathological (two edge labels
                    # parsing to the same key); keep the first — routing goes by
                    # key either way, the label is display-only.
                    continue
                seen.add(opt.key)
                options.append(Option(id=opt.key, label=opt.label))
            if len(options) < 2:
                raise ValueError(
                    f"{gate} yields {len(options)} option(s) {[o.id for o in options]}; "
                    "attractor-gate packets require at least 2 (a single-option gate is not a "
                    "decision a cold reader can make). Add a second labeled outgoing edge "
                    "(e.g. '[X] Cancel') to the hexagon node."
                )
        else:
            raise ValueError(f"{gate} has unsupported question type {qtype!r}")

        context_parts = [f"attractor hexagon gate; stage: {stage}"]
        metadata = getattr(question, "metadata", None) or {}
        description = metadata.get("description")
        if description:
            context_parts.append(str(description))

        urgency = Urgency()
        timeout_seconds = getattr(question, "timeout_seconds", None)
        if timeout_seconds is not None:
            deadline = datetime.now(UTC) + timedelta(seconds=float(timeout_seconds))
            urgency = Urgency(
                tier="batch",
                deadline=deadline.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                on_timeout=OnTimeout(action="fail-loud"),
            )

        packet = Packet(
            question=question.text,
            options=options,
            source=Source(kind="attractor-gate", work_unit=self.work_unit_name),
            context="\n".join(context_parts),
            urgency=urgency,
        )
        packet.validate()  # fail loud here, not at queue.write time
        return packet

    # -- events ----------------------------------------------------------------

    def _emit(self, event: str, **fields: Any) -> None:
        if self.events_emitter is not None:
            self.events_emitter(event, **fields)
        else:
            logger.info("event %s (no emitter wired): %s", event, fields)

    def _publish(self, question: Any) -> Packet:
        packet = self.build_packet(question)
        self.queue.write(packet)
        self._emit(
            "gate:packet_created",
            work_unit=self.work_unit_name,
            stage=getattr(question, "stage", "") or "",
            packet_id=packet.id,
        )
        return packet

    def _resolution_to_answer(self, question: Any, packet: Packet, resolution: Resolution) -> Any:
        """Validate the resolution against the declared options and build an Answer.

        Misroute protection: the engine silently routes any unrecognized answer
        string to the FIRST edge choice. Never hand it one — raise loudly.
        """
        allowed = packet.option_ids()
        if resolution.answer not in allowed:
            raise RuntimeError(
                f"packet {packet.id} (gate stage {getattr(question, 'stage', '')!r}, work unit "
                f"{self.work_unit_name!r}) resolved with answer {resolution.answer!r} which is NOT one of "
                f"the declared options {allowed} — refusing to return it: the attractor engine silently "
                "routes unrecognized answers to the first edge (misroute). The answered/ file is corrupt "
                "or was written outside the queue contract."
            )
        self._emit(
            "gate:answered",
            work_unit=self.work_unit_name,
            stage=getattr(question, "stage", "") or "",
            packet_id=packet.id,
            answer=resolution.answer,
        )
        interviewer_mod = import_loop_pipeline("interviewer")
        return interviewer_mod.Answer(value=resolution.answer)

    # -- Interviewer protocol (sync) + duck-typed async_ask ---------------------

    async def async_ask(self, question: Any) -> Any:
        """Publish the gate as a packet and await its resolution (engine-preferred)."""
        packet = self._publish(question)
        resolution = await self.queue.await_resolution_async(
            packet.id,
            poll_s=self.poll_s,
            timeout_s=getattr(question, "timeout_seconds", None),
        )
        return self._resolution_to_answer(question, packet, resolution)

    def ask(self, question: Any) -> Any:
        """Blocking equivalent of async_ask (Interviewer Protocol)."""
        packet = self._publish(question)
        resolution = self.queue.await_resolution(
            packet.id,
            poll_s=self.poll_s,
            timeout_s=getattr(question, "timeout_seconds", None),
        )
        return self._resolution_to_answer(question, packet, resolution)

    def ask_multiple(self, questions: list[Any]) -> list[Any]:
        """Sequential batch ask (Interviewer Protocol)."""
        return [self.ask(q) for q in questions]

    def inform(self, message: str) -> None:
        """One-way notification: appended to the event log, never print-only."""
        self._emit("workunit:inform", work_unit=self.work_unit_name, message=message)
