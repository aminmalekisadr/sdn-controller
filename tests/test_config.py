"""Controller config: defaults, overrides, validation, and the timing it implies."""

import json
from pathlib import Path
from typing import Any

import pytest

from sdnctl.config import Timing, default_config, load_config
from sdnctl.jsonio import from_jsonable, to_jsonable

# SPEC Section 9.5, copied independently of sdnctl.config.DEFAULTS.
SPEC_CONFIG = json.loads("""
{
  "features": { "two_plus_two": true, "ocs": true },
  "timing_profile": "typical",
  "timing_us": {
    "typical": { "local_prune": 1000, "report": 1000, "plan": 1000, "compute": 1000,
                 "ocs_command": 5000, "mirror_move": 25000, "lane_bringup": 50000,
                 "hello_check": 2000, "error_check": 10000, "lane_up_report": 1000,
                 "device_write": 3000, "correlation_window": 5000 },
    "worst":   { "local_prune": 5000, "report": 5000, "plan": 5000, "compute": 5000,
                 "ocs_command": 50000, "mirror_move": 200000, "lane_bringup": 2500000,
                 "hello_check": 200000, "error_check": 2000000, "lane_up_report": 5000,
                 "device_write": 10000, "correlation_window": 5000 }
  },
  "timeouts_us": { "ack": 50000, "ocs_done": 1000000, "verify": 6000000 },
  "limits": { "ack_resends": 1, "ocs_resends": 1 }
}
""")


def test_defaults_equal_spec() -> None:
    assert to_jsonable(default_config()) == SPEC_CONFIG


def test_config_json_round_trip() -> None:
    cfg = default_config()
    assert from_jsonable(type(cfg), json.loads(json.dumps(to_jsonable(cfg)))) == cfg


def test_partial_override_merges_over_defaults(tmp_path: Path) -> None:
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"timing_profile": "worst", "features": {"ocs": False}}))
    cfg = load_config(path)
    assert cfg.features.two_plus_two is True
    assert cfg.features.ocs is False
    assert cfg.timing().lane_bringup == 2_500_000
    assert cfg.timeouts_us.ack == 50_000


@pytest.mark.parametrize(
    "bad",
    [
        {"featurez": {}},
        {"features": {"three_plus_one": True}},
        {"timing_profile": "fast"},
        {"timeouts_us": {"ack": "50000"}},
        {"features": {"ocs": 1}},
        {"limits": 1},
    ],
)
def test_bad_config_is_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        load_config(bad)


def g4_timeline_ms(t: Timing) -> dict[str, int]:
    """G4 timeline (SPEC 11.0 timing rules 1-7) built from config values only."""
    us = {"lane_down_at_controller": t.report}
    us["plan_done"] = us["lane_down_at_controller"] + t.plan
    us["prepare_acked"] = us["plan_done"] + t.device_write
    us["ocs_applied"] = us["plan_done"] + t.ocs_command
    us["ocs_done"] = us["ocs_applied"] + t.mirror_move
    us["light"] = us["ocs_done"] + t.lane_bringup
    us["hello_ok"] = us["light"] + t.hello_check
    us["errors_clean"] = us["hello_ok"] + t.error_check
    us["lane_up_at_controller"] = us["errors_clean"] + t.lane_up_report
    us["groups_acked"] = us["lane_up_at_controller"] + t.device_write
    assert all(v % 1000 == 0 for v in us.values())
    return {k: v // 1000 for k, v in us.items()}


@pytest.mark.parametrize("profile", ["typical", "worst"])
def test_profiles_reproduce_g4_timeline(profile: str, golden: dict[str, Any]) -> None:
    cfg = load_config({"timing_profile": profile})
    assert g4_timeline_ms(cfg.timing()) == golden["G4a"][f"timeline_{profile}"]


def test_timeouts_exceed_worst_profile() -> None:
    cfg = default_config()
    worst = cfg.timing_us["worst"]
    assert cfg.timeouts_us.ocs_done > worst.ocs_command + worst.mirror_move
    verify = worst.lane_bringup + worst.hello_check + worst.error_check + worst.lane_up_report
    assert cfg.timeouts_us.verify > verify
