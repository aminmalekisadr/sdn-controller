"""Run results on disk (SPEC 10, Phase 8): trace.jsonl, summary.json and capacity.csv.

The same run always writes the same bytes, with '\\n' line endings on every platform. Times are
microseconds of simulated time, like the trace's "t", except the scenario's own t_ms fields.
"""

import csv
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sdnctl.config import ControllerConfig, Features, Limits, Timeouts, Timing
from sdnctl.interfaces import CapacitySample, IncidentOutcome, SimReport
from sdnctl.jsonio import dump_json
from sdnctl.scenario import Scenario

TRACE_FILE = "trace.jsonl"
SUMMARY_FILE = "summary.json"
CAPACITY_FILE = "capacity.csv"
REPORT_FILES = (TRACE_FILE, SUMMARY_FILE, CAPACITY_FILE)
CAPACITY_HEADER = ("t_us", "key", "value")


@dataclass(frozen=True)
class CapacityRange:
    """Lowest and final value of one capacity key during a run (every key starts at 1.0)."""

    min: float
    final: float


@dataclass(frozen=True)
class RunSummary:
    """What summary.json holds: the settings run, incident end states, messages and capacity."""

    scenario: str
    topology: str
    spare_modules_per_domain: int
    features: Features
    timing_profile: str
    timing_us: Timing
    timeouts_us: Timeouts
    limits: Limits
    events: tuple[Mapping[str, Any], ...]  # as in the scenario file
    run_until_ms: int
    incidents: tuple[IncidentOutcome, ...]
    messages: Mapping[str, int]  # counted messages by type, sorted by type
    messages_total: int
    capacity: Mapping[str, CapacityRange]  # only the keys that changed, sorted by key
    trace_records: int


def capacity_ranges(samples: Iterable[CapacitySample]) -> dict[str, CapacityRange]:
    """Lowest and final value of every key that has a sample, sorted by key."""
    lowest: dict[str, float] = {}
    final: dict[str, float] = {}
    for s in samples:
        lowest[s.key] = min(lowest.get(s.key, 1.0), s.value)
        final[s.key] = s.value
    return {k: CapacityRange(lowest[k], final[k]) for k in sorted(final)}


def summarize(scenario: Scenario, config: ControllerConfig, report: SimReport) -> RunSummary:
    """The summary of one run of scenario (as it ran) with config's timing, timeouts, limits."""
    return RunSummary(
        scenario=scenario.name,
        topology=scenario.topology,
        spare_modules_per_domain=scenario.spare_modules_per_domain,
        features=scenario.features,
        timing_profile=scenario.timing_profile,
        timing_us=config.timing_us[scenario.timing_profile],
        timeouts_us=config.timeouts_us,
        limits=config.limits,
        events=tuple(e.as_dict() for e in scenario.events),
        run_until_ms=scenario.end_ms(),
        incidents=report.incidents,
        messages=dict(sorted(report.messages.items())),
        messages_total=report.messages_total(),
        capacity=capacity_ranges(report.capacity),
        trace_records=len(report.trace),
    )


def write_trace(records: Iterable[Mapping[str, Any]], path: str | Path) -> None:
    """One compact JSON object per line, in trace order; a record that is not JSON raises."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, separators=(",", ":")))
            f.write("\n")


def write_capacity(samples: Iterable[CapacitySample], path: str | Path) -> None:
    """Every capacity sample as a CSV row t_us,key,value, in time order."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        out = csv.writer(f, lineterminator="\n")
        out.writerow(CAPACITY_HEADER)
        for s in samples:
            out.writerow((s.time, s.key, repr(s.value)))


def write_report(out_dir: str | Path, summary: RunSummary, report: SimReport) -> None:
    """Write trace.jsonl, summary.json and capacity.csv into out_dir, creating it."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    write_trace(report.trace, d / TRACE_FILE)
    dump_json(summary, d / SUMMARY_FILE, indent=2)
    write_capacity(report.capacity, d / CAPACITY_FILE)
