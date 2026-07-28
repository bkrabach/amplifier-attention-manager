"""Notification sinks + batching (design §Tier 3: "notifications announce
batches, not individual packets").

Sinks are EXPLICITLY configured (``--notify`` flag or ``$ATTENTION_NOTIFY``).
There is deliberately NO default silent sink: if none is configured, the
supervisor logs a loud one-time warning that notifications are disabled.
Configured-but-failing delivery is loud degraded operation: the error is
logged (event + stderr), the items stay queued for retry on the next flush,
and the loop never crashes — and never drops silently (D7).

Sink specs:

    file:<path>   append ONE JSON line per batch to <path>
    ntfy:<url>    POST a batch summary (title + body) to an ntfy endpoint
    console       print the batch to stdout
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .packet import utc_now_iso

DEFAULT_BATCH_WINDOW_S = 20.0
DEFAULT_BATCH_MAX = 10
QUESTION_TRUNCATE = 80


def _truncate(text: str, width: int = QUESTION_TRUNCATE) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "\u2026"


@dataclass
class BatchItem:
    packet_id: str  # packet id for kind="packet"; worker session name for finish-line kinds
    question: str
    ts: str  # wall-clock ISO, for the delivered payload
    enqueued_at: float  # monotonic, for the window policy
    kind: str = "packet"  # "packet" | "finish_line" | "finish_line_failed"


class Sink(Protocol):
    name: str

    def deliver(self, items: list[BatchItem]) -> None:
        """Deliver one batch. Raises on failure (caller handles retry)."""
        ...


class FileSink:
    """Appends ONE JSON line per batch: {ts, count, packets: [{id, question}]}."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.name = f"file:{self.path}"

    def deliver(self, items: list[BatchItem]) -> None:
        line = json.dumps(
            {
                "ts": utc_now_iso(),
                "count": len(items),
                "packets": [{"id": i.packet_id, "question": i.question, "kind": i.kind} for i in items],
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


class ConsoleSink:
    """Prints the batch to stdout."""

    name = "console"

    def deliver(self, items: list[BatchItem]) -> None:
        print(f"[attention] {len(items)} packet(s) need you:")
        for item in items:
            prefix = "" if item.kind == "packet" else f"[{item.kind}] "
            print(f"  {prefix}{item.packet_id}: {_truncate(item.question)}")


class NtfySink:
    """POSTs a batch summary to an ntfy topic URL via urllib (stdlib only)."""

    def __init__(self, url: str, timeout_s: float = 10.0):
        self.url = url
        self.timeout_s = timeout_s
        self.name = f"ntfy:{url}"

    def deliver(self, items: list[BatchItem]) -> None:
        title = f"{len(items)} packet{'s' if len(items) != 1 else ''} need you"
        body = "\n".join(f"{i.packet_id}: {_truncate(i.question)}" for i in items)
        request = urllib.request.Request(
            self.url,
            data=body.encode("utf-8"),
            headers={"Title": title, "Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        # urllib raises HTTPError (non-2xx) / URLError (network) — both propagate
        # to the batcher, which keeps the items queued and reports loudly.
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            response.read()


def parse_sink(spec: str) -> Sink:
    """Parse a sink spec. Unknown specs fail loud (never a silent no-op sink)."""
    if spec == "console":
        return ConsoleSink()
    if spec.startswith("file:"):
        path = spec[len("file:") :]
        if not path:
            raise ValueError("sink spec 'file:' requires a path, e.g. file:/tmp/notify.jsonl")
        return FileSink(path)
    if spec.startswith("ntfy:"):
        url = spec[len("ntfy:") :]
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"sink spec 'ntfy:' requires an http(s) URL, got {url!r}")
        return NtfySink(url)
    raise ValueError(f"unknown notification sink {spec!r}; expected file:<path>, ntfy:<url>, or console")


@dataclass
class FlushOutcome:
    delivered: bool
    count: int
    packet_ids: list[str]
    sink: str
    error: str | None = None


@dataclass
class NotificationBatcher:
    """Collects packet-arrival items; flushes as batches per policy.

    Flush when the OLDEST item's age >= batch_window_s, or when the batch
    size >= batch_max. On delivery failure the items are RETAINED and retried
    on the next flush check — loud degraded, never dropped.
    """

    sink: Sink
    batch_window_s: float = DEFAULT_BATCH_WINDOW_S
    batch_max: int = DEFAULT_BATCH_MAX
    clock: object = time.monotonic  # injectable for tests
    items: list[BatchItem] = field(default_factory=list)

    def _now(self) -> float:
        return self.clock()  # type: ignore[operator]

    def enqueue(self, packet_id: str, question: str, kind: str = "packet") -> None:
        self.items.append(
            BatchItem(packet_id=packet_id, question=question, ts=utc_now_iso(), enqueued_at=self._now(), kind=kind)
        )

    def due(self) -> bool:
        if not self.items:
            return False
        if len(self.items) >= self.batch_max:
            return True
        oldest_age = self._now() - self.items[0].enqueued_at
        return oldest_age >= self.batch_window_s

    def flush(self) -> FlushOutcome:
        """Deliver everything queued. Failure keeps items for the next attempt."""
        batch = list(self.items)
        packet_ids = [i.packet_id for i in batch]
        try:
            self.sink.deliver(batch)
        except Exception as e:  # loud degraded: report, retain, retry later
            return FlushOutcome(
                delivered=False, count=len(batch), packet_ids=packet_ids, sink=self.sink.name, error=str(e)
            )
        self.items = self.items[len(batch) :]  # drop exactly what was delivered
        return FlushOutcome(delivered=True, count=len(batch), packet_ids=packet_ids, sink=self.sink.name)

    def flush_if_due(self) -> FlushOutcome | None:
        """Flush when the batching policy says so. None = nothing to do yet."""
        if not self.due():
            return None
        return self.flush()
