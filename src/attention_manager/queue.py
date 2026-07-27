"""PacketQueue — the disk-backed escalation queue.

Directory contract (authoritative doc: context/packet-schema.md):

    <root>/
      pending/    packets awaiting an answer
      answered/   resolved packets (resolution filled)
      auto/       Phase-2+ auto-answered packets (manager log)
      bounced/    malformed packets returned to producers

Root resolution: ``$ATTENTION_QUEUE_DIR`` if set, else ``~/.amplifier/attention/queue``.
All writes are atomic (write ``.tmp`` then ``os.replace``). The queue is rebuilt
from the filesystem on every scan — no queue state lives anywhere else.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from .packet import Packet
from .packet import Resolution
from .packet import utc_now_iso

DEFAULT_QUEUE_DIR = "~/.amplifier/attention/queue"
ENV_QUEUE_DIR = "ATTENTION_QUEUE_DIR"

SUBDIRS = ("pending", "answered", "auto", "bounced")


def default_queue_root() -> Path:
    """Resolve the queue root: $ATTENTION_QUEUE_DIR else ~/.amplifier/attention/queue."""
    return Path(os.environ.get(ENV_QUEUE_DIR) or DEFAULT_QUEUE_DIR).expanduser()


class PacketQueue:
    """File-backed packet queue. Every operation reads fresh from disk."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser() if root is not None else default_queue_root()

    # -- paths ---------------------------------------------------------------

    def dir(self, subdir: str) -> Path:
        if subdir not in SUBDIRS:
            raise ValueError(f"unknown queue subdir {subdir!r}; expected one of {SUBDIRS}")
        path = self.root / subdir
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path_for(self, packet_id: str, subdir: str = "pending") -> Path:
        return self.dir(subdir) / f"{packet_id}.json"

    # -- write / read --------------------------------------------------------

    def _write_atomic(self, path: Path, text: str) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def write(self, packet: Packet, subdir: str = "pending") -> Path:
        """Validate and atomically write a packet. Returns the written path."""
        packet.validate()
        path = self.path_for(packet.id, subdir)
        self._write_atomic(path, packet.to_json())
        return path

    def _load(self, path: Path) -> Packet:
        try:
            return Packet.from_json(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"malformed packet file {path}: {e}") from e

    def list_pending(self) -> list[Packet]:
        """All pending packets, sorted by id (ids are time-sortable)."""
        packets = [self._load(p) for p in sorted(self.dir("pending").glob("pkt-*.json"))]
        return sorted(packets, key=lambda p: p.id)

    def locate(self, packet_id: str) -> tuple[str, Path]:
        """Find a packet by id across all subdirs. Raises KeyError if absent."""
        for subdir in SUBDIRS:
            path = self.path_for(packet_id, subdir)
            if path.exists():
                return subdir, path
        raise KeyError(f"packet {packet_id!r} not found in any of {SUBDIRS} under {self.root}")

    def get(self, packet_id: str) -> Packet:
        """Load a packet by id, searching all subdirs."""
        _, path = self.locate(packet_id)
        return self._load(path)

    # -- answering -----------------------------------------------------------

    def answer(
        self,
        packet_id: str,
        option: str,
        rationale: str | None = None,
        answered_by: str = "human",
    ) -> Packet:
        """Answer a pending packet: fill resolution and move it to answered/.

        Raises ValueError if the packet is not pending or the option is not one
        of the packet's declared options (fail loud — never invent an answer).
        """
        subdir, path = self.locate(packet_id)
        if subdir != "pending":
            raise ValueError(f"packet {packet_id!r} is in {subdir}/, not pending/ — cannot answer")

        packet = self._load(path)
        ids = packet.option_ids()
        if option not in ids:
            raise ValueError(f"option {option!r} is not one of packet {packet_id!r} options {ids}")

        packet.resolution = Resolution(
            answer=option,
            rationale=rationale,
            answered_by=answered_by,
            answered_at=utc_now_iso(),
        )
        # Atomic move pending/ -> answered/: write the resolved packet into
        # answered/ first, then remove the pending file. A crash between the
        # two steps leaves both present; answered/ is authoritative.
        self.write(packet, subdir="answered")
        path.unlink(missing_ok=True)
        return packet

    # -- awaiting ------------------------------------------------------------

    def _check_resolution(self, packet_id: str) -> Resolution | None:
        path = self.path_for(packet_id, "answered")
        if not path.exists():
            return None
        packet = self._load(path)
        if packet.resolution is None:
            raise ValueError(f"packet {packet_id!r} is in answered/ but has no resolution — corrupt queue state")
        return packet.resolution

    def await_resolution(
        self,
        packet_id: str,
        poll_s: float = 1.0,
        timeout_s: float | None = None,
    ) -> Resolution:
        """Block until the packet is answered. timeout_s=None waits indefinitely.

        Raises TimeoutError on timeout — never returns a fabricated answer.
        """
        started = time.monotonic()
        while True:
            resolution = self._check_resolution(packet_id)
            if resolution is not None:
                return resolution
            if timeout_s is not None and (time.monotonic() - started) >= timeout_s:
                raise TimeoutError(f"packet {packet_id!r} unanswered after {timeout_s}s")
            time.sleep(poll_s)

    async def await_resolution_async(
        self,
        packet_id: str,
        poll_s: float = 1.0,
        timeout_s: float | None = None,
    ) -> Resolution:
        """Async variant of await_resolution. Same fail-loud timeout semantics."""
        started = time.monotonic()
        while True:
            resolution = self._check_resolution(packet_id)
            if resolution is not None:
                return resolution
            if timeout_s is not None and (time.monotonic() - started) >= timeout_s:
                raise TimeoutError(f"packet {packet_id!r} unanswered after {timeout_s}s")
            await asyncio.sleep(poll_s)
