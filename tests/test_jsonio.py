"""The generic JSON codec on model and interface types, files, and decode errors."""

import json
from pathlib import Path
from typing import Any

import pytest

from sdnctl.interfaces import NoRepair, RepairPlan
from sdnctl.jsonio import dump_json, from_jsonable, load_json, to_jsonable
from sdnctl.messages import CONTROLLER, Header, LaneDown, Prepare
from sdnctl.model import (
    CrossConnect,
    Device,
    Domain,
    Incident,
    Lane,
    Link,
    Module,
    Port,
    TopologySpec,
)
from sdnctl.types import (
    DownCause,
    IncidentState,
    LinkKind,
    ModuleMapping,
    MsgType,
    Role,
    Tier,
)


def tiny_spec() -> TopologySpec:
    """Two NPUs joined by one optical link through the OCS, 2+2 on a one-port domain."""
    devices = (
        Device("npu-0", Tier.NPU, Role.NPU, rack=0, plane=None, board=0, script_id=0),
        Device("npu-64", Tier.NPU, Role.NPU, rack=1, plane=None, board=0, script_id=64),
    )
    ports = tuple(Port(f"{d.id}.p13", d.id, 13, LinkKind.OPTICAL, spare=False) for d in devices)
    lanes = tuple(
        Lane(
            f"{d.id}.p13.{j}",
            d.id,
            f"{d.id}.p13",
            j,
            module=f"{d.id}.m{j}",
            ocs_port=f"{'NS'[i]}{j + 1}",
            spare=False,
        )
        for i, d in enumerate(devices)
        for j in range(2)
    )
    modules = tuple(
        Module(f"{d.id}.m{j}", d.id, f"{d.id}.d0", (f"{d.id}.p13.{j}",), spare=False)
        for d in devices
        for j in range(2)
    )
    domains = tuple(
        Domain(
            f"{d.id}.d0",
            d.id,
            (f"{d.id}.p13",),
            (f"{d.id}.m0", f"{d.id}.m1"),
            spare_modules=(),
            mapping=ModuleMapping.TWO_PLUS_TWO,
        )
        for d in devices
    )
    return TopologySpec(
        name="tiny",
        mapping=ModuleMapping.TWO_PLUS_TWO,
        spare_modules_per_domain=0,
        devices=devices,
        ports=ports,
        links=(Link("npu-0.p13", "npu-64.p13", LinkKind.OPTICAL),),
        lanes=lanes,
        modules=modules,
        domains=domains,
        xconnects=(CrossConnect("N1", "S1"), CrossConnect("N2", "S2")),
        has_ocs=True,
        routed_tiers=frozenset({Tier.NPU, Tier.L1}),
    )


def round_trip(tp: type[Any], value: Any) -> Any:
    """Encode to JSON text and decode again."""
    return from_jsonable(tp, json.loads(json.dumps(to_jsonable(value))))


def test_topology_spec_round_trip_through_file(tmp_path: Path) -> None:
    spec = tiny_spec()
    path = tmp_path / "sub" / "tiny.json"
    dump_json(spec, path)
    assert load_json(TopologySpec, path) == spec


def test_frozensets_give_deterministic_json() -> None:
    a = to_jsonable(frozenset({Tier.L1, Tier.NPU, Tier.L2}))
    b = to_jsonable(frozenset({Tier.L2, Tier.NPU, Tier.L1}))
    assert a == b == ["l1", "l2", "npu"]


def test_incident_round_trip() -> None:
    report = LaneDown(
        Header(MsgType.LANE_DOWN, 1, 1, None, "npu-0", CONTROLLER, 1000),
        device="npu-0",
        lanes=("npu-0.p13.0", "npu-0.p13.1", "npu-0.p14.0", "npu-0.p14.1"),
        cause=DownCause.MODULE_FAULT,
        modules=("npu-0.m0", "npu-0.m1"),
    )
    incident = Incident(
        id=1,
        opened_at=1000,
        reports=[report],
        failed_device="npu-0",
        failed_modules=("npu-0.m0", "npu-0.m1"),
        lanes=frozenset(report.lanes),
        lost=frozenset({("npu-0", "npu-64"), ("npu-64", "npu-0")}),
        state=IncidentState.REROUTING,
    )
    assert round_trip(Incident, incident) == incident


def test_repair_plan_round_trip() -> None:
    prepare = Prepare(
        Header(MsgType.PREPARE, 3, 1, 1, CONTROLLER, "npu-0", 2000),
        open_lanes=("npu-0.p15.0", "npu-0.p15.1"),
        expected_peer={"npu-0.p15.0": "npu-64.p13.0", "npu-0.p15.1": "npu-64.p14.0"},
    )
    plan = RepairPlan(
        failed_device="npu-0",
        failed_modules=("npu-0.m0",),
        spare_module="npu-0.m2",
        disconnect=(CrossConnect("N1", "S1"), CrossConnect("N3", "S3")),
        connect=(CrossConnect("N257", "S1"), CrossConnect("N258", "S3")),
        targets=frozenset({"npu-0", "npu-64"}),
        prepares={"npu-0": prepare},
    )
    assert round_trip(RepairPlan, plan) == plan
    assert round_trip(NoRepair, NoRepair("no_spare")) == NoRepair("no_spare")


def test_no_repair_rejects_unknown_reason() -> None:
    with pytest.raises(ValueError):
        NoRepair("bad_luck")


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"north": "N1"}, "missing field 'south'"),
        ({"north": "N1", "south": "S1", "east": "E1"}, "unknown fields"),
        ({"north": "N1", "south": 1}, r"\$\.south: expected str"),
        (["N1", "S1"], "expected dict"),
    ],
)
def test_decode_errors_name_the_path(data: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        from_jsonable(CrossConnect, data)


def test_decode_rejects_bad_enum_value() -> None:
    data = to_jsonable(tiny_spec())
    data["devices"][1]["tier"] = "l3"
    with pytest.raises(ValueError, match=r"\$\.devices\[1\]\.tier"):
        from_jsonable(TopologySpec, data)
