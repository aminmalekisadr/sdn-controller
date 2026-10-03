"""Sort keys and enums of sdnctl.types."""

import pytest

from sdnctl.types import (
    DownCause,
    IncidentState,
    LaneState,
    ModuleMapping,
    MsgType,
    Tier,
    device_key,
    lane_key,
    port_key,
)


def test_device_key_sorts_by_tier_then_number() -> None:
    ids = ["l2-2304", "npu-10", "l1-1536", "npu-9", "l1-1552", "npu-0"]
    assert sorted(ids, key=device_key) == [
        "npu-0",
        "npu-9",
        "npu-10",
        "l1-1536",
        "l1-1552",
        "l2-2304",
    ]
    assert device_key("l1-1536") == (1, 1536)


def test_port_and_lane_keys_are_numeric() -> None:
    assert port_key("l1-1536.p12") == ((1, 1536), 12)
    assert lane_key("l1-1536.p12.1") == ((1, 1536), 12, 1)
    lanes = ["l1-1536.p12.0", "npu-0.p2.1", "l1-1536.p2.0", "npu-0.p2.0"]
    assert sorted(lanes, key=lane_key) == [
        "npu-0.p2.0",
        "npu-0.p2.1",
        "l1-1536.p2.0",
        "l1-1536.p12.0",
    ]


@pytest.mark.parametrize("bad", ["host-1", "npu-x", "npu-0.p1", "npu-0.q1.0", "npu-0.p1.0.0"])
def test_lane_key_rejects_bad_ids(bad: str) -> None:
    with pytest.raises(ValueError):
        lane_key(bad)


def test_enum_values_match_spec() -> None:
    assert [t.value for t in Tier] == ["npu", "l1", "l2"]
    assert ModuleMapping.TWO_PLUS_TWO.value == "2+2"
    assert ModuleMapping.TWO_TO_ONE.value == "2:1"
    assert DownCause.LOSS_OF_LIGHT.value == "los"
    assert LaneState.IDLE.value == "idle"
    assert IncidentState.DEGRADED_NO_SPARE.value == "degraded_no_spare"
    assert len(MsgType) == 10
