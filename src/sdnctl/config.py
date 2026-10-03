"""Controller config of SPEC Section 9.5: feature flags, timing profiles, timeouts and limits.

All times are design estimates in microseconds. They live here, in config, and nowhere else.
"""

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sdnctl.jsonio import from_jsonable
from sdnctl.types import SimTime

DEFAULTS: dict[str, Any] = {
    "features": {"two_plus_two": True, "ocs": True},
    "timing_profile": "typical",
    "timing_us": {
        "typical": {
            "local_prune": 1000,
            "report": 1000,
            "plan": 1000,
            "compute": 1000,
            "ocs_command": 5000,
            "mirror_move": 25000,
            "lane_bringup": 50000,
            "hello_check": 2000,
            "error_check": 10000,
            "lane_up_report": 1000,
            "device_write": 3000,
            "correlation_window": 5000,
        },
        "worst": {
            "local_prune": 5000,
            "report": 5000,
            "plan": 5000,
            "compute": 5000,
            "ocs_command": 50000,
            "mirror_move": 200000,
            "lane_bringup": 2500000,
            "hello_check": 200000,
            "error_check": 2000000,
            "lane_up_report": 5000,
            "device_write": 10000,
            "correlation_window": 5000,
        },
    },
    "timeouts_us": {"ack": 50000, "ocs_done": 1000000, "verify": 6000000},
    "limits": {"ack_resends": 1, "ocs_resends": 1},
}


@dataclass(frozen=True)
class Features:
    """Feature flags: 2+2 module mapping (else 2:1) and the OCS controller."""

    two_plus_two: bool
    ocs: bool


@dataclass(frozen=True)
class Timing:
    """One timing profile, in microseconds (SPEC Section 11.0 says where each one applies)."""

    local_prune: SimTime
    report: SimTime
    plan: SimTime
    compute: SimTime
    ocs_command: SimTime
    mirror_move: SimTime
    lane_bringup: SimTime
    hello_check: SimTime
    error_check: SimTime
    lane_up_report: SimTime
    device_write: SimTime
    correlation_window: SimTime


@dataclass(frozen=True)
class Timeouts:
    """Scheduler timeouts, in microseconds (SPEC Section 7.3)."""

    ack: SimTime
    ocs_done: SimTime
    verify: SimTime


@dataclass(frozen=True)
class Limits:
    """How often the scheduler resends before giving up."""

    ack_resends: int
    ocs_resends: int


@dataclass(frozen=True)
class ControllerConfig:
    """The whole controller config."""

    features: Features
    timing_profile: str
    timing_us: dict[str, Timing]
    timeouts_us: Timeouts
    limits: Limits

    def __post_init__(self) -> None:
        if self.timing_profile not in self.timing_us:
            raise ValueError(
                f"unknown timing profile {self.timing_profile!r}; "
                f"known: {sorted(self.timing_us)}"
            )

    def timing(self) -> Timing:
        """The active timing profile."""
        return self.timing_us[self.timing_profile]


def default_config() -> ControllerConfig:
    """The config of SPEC Section 9.5."""
    return load_config(None)


def load_config(source: str | Path | Mapping[str, Any] | None) -> ControllerConfig:
    """Load a config from a JSON file or a dict, merged over the defaults; unknown keys raise."""
    if source is None:
        overrides: Mapping[str, Any] = {}
    elif isinstance(source, Mapping):
        overrides = source
    else:
        overrides = json.loads(Path(source).read_text(encoding="utf-8"))
    return from_jsonable(ControllerConfig, _merge(DEFAULTS, overrides, "config"))


def _merge(base: Mapping[str, Any], overrides: Mapping[str, Any], path: str) -> dict[str, Any]:
    out = copy.deepcopy(dict(base))
    for key, value in overrides.items():
        if key not in base:
            raise ValueError(f"unknown config key {path}.{key}")
        if isinstance(base[key], dict):
            if not isinstance(value, Mapping):
                raise ValueError(f"config key {path}.{key} must be an object")
            out[key] = _merge(base[key], value, f"{path}.{key}")
        else:
            out[key] = value
    return out
