"""Scenario files (SPEC 11): load one, and run it in the simulator with the SdnController.

Format: {"name", "topology", "spare_modules_per_domain" (default 0), "features": {"two_plus_two",
"ocs"}, "timing_profile" (default "typical"), "events": [{"t_ms", "event", ...args}]}.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sdnctl.config import ControllerConfig, Features
from sdnctl.controller import SdnController
from sdnctl.sim import Simulator

RUN_AFTER_LAST_EVENT_MS = 30_000  # longer than every timeout and the worst-case restore
_KEYS = {"name", "topology", "spare_modules_per_domain", "features", "timing_profile", "events"}


@dataclass(frozen=True)
class ScenarioEvent:
    """One injected event: time, name and arguments."""

    t_ms: int
    event: str
    args: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """The event as a scenario file writes it."""
        return {"t_ms": self.t_ms, "event": self.event, **self.args}


@dataclass(frozen=True)
class Scenario:
    """One scenario: topology, features, timing profile and events."""

    name: str
    topology: str
    spare_modules_per_domain: int
    features: Features
    timing_profile: str
    events: tuple[ScenarioEvent, ...]

    def end_ms(self) -> int:
        """When a run of this scenario stops."""
        return max((e.t_ms for e in self.events), default=0) + RUN_AFTER_LAST_EVENT_MS


def load_scenario(source: str | Path | Mapping[str, Any]) -> Scenario:
    """Read a scenario from a JSON file or a dict; unknown or missing keys raise ValueError."""
    data = source if isinstance(source, Mapping) else json.loads(
        Path(source).read_text(encoding="utf-8")
    )
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise ValueError(f"unknown scenario keys: {unknown}")
    missing = sorted({"name", "topology", "features", "events"} - set(data))
    if missing:
        raise ValueError(f"scenario is missing: {missing}")
    features = data["features"]
    if not isinstance(features, Mapping) or set(features) != {"two_plus_two", "ocs"}:
        raise ValueError("scenario features must be {\"two_plus_two\": ..., \"ocs\": ...}")
    events = []
    for i, e in enumerate(data["events"]):
        if not isinstance(e, Mapping) or not {"t_ms", "event"} <= set(e):
            raise ValueError(f"scenario event {i} needs t_ms and event")
        args = {k: v for k, v in e.items() if k not in ("t_ms", "event")}
        events.append(ScenarioEvent(int(e["t_ms"]), str(e["event"]), args))
    return Scenario(
        name=str(data["name"]),
        topology=str(data["topology"]),
        spare_modules_per_domain=int(data.get("spare_modules_per_domain", 0)),
        features=Features(bool(features["two_plus_two"]), bool(features["ocs"])),
        timing_profile=str(data.get("timing_profile", "typical")),
        events=tuple(events),
    )


def start_scenario(
    scenario: Scenario,
    timing_profile: str | None = None,
    config: ControllerConfig | None = None,
) -> Simulator:
    """A simulator with the scenario loaded, configured and its events injected, not yet run.

    config gives the timing values, timeouts and limits (default: SPEC 9.5); the scenario's
    features and timing profile replace the config's own.
    """
    sim = Simulator(controller=SdnController, config=config)
    sim.load_topology(
        scenario.topology, scenario.features.two_plus_two, scenario.spare_modules_per_domain
    )
    sim.configure(ocs=scenario.features.ocs, timing=timing_profile or scenario.timing_profile)
    for e in scenario.events:
        sim.inject(e.t_ms, e.event, **e.args)
    return sim


def run_scenario(
    scenario: Scenario,
    timing_profile: str | None = None,
    config: ControllerConfig | None = None,
) -> Simulator:
    """Run a scenario to its end and return the simulator (call report() for the results)."""
    sim = start_scenario(scenario, timing_profile, config)
    sim.run(scenario.end_ms())
    return sim
