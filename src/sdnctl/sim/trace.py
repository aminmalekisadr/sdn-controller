"""Trace: every message and state change, as JSON-ready records through ITraceSink."""

from collections.abc import Mapping
from typing import Any

from sdnctl.interfaces import IClock, ITraceSink


class MemoryTrace:
    """ITraceSink that keeps every record in memory, in order."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, event: Mapping[str, Any]) -> None:
        """Append one trace record."""
        self.records.append(dict(event))


class Tracer:
    """Stamps records with the current time and a kind, and passes them to a sink."""

    def __init__(self, clock: IClock, sink: ITraceSink) -> None:
        self._clock = clock
        self._sink = sink

    def emit(self, kind: str, **fields: Any) -> None:
        """Record {'t': now in us, 'kind': kind, **fields}; fields must be JSON-ready."""
        self._sink.record({"t": self._clock.now(), "kind": kind, **fields})
