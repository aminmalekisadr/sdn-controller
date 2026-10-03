"""Spot checks of SPEC 3.2-3.4: port lists, domains, module lanes, OCS ports, cross-connects."""

from typing import Any

import pytest

from sdnctl.model import Domain, TopologySpec
from sdnctl.types import CrossConnect
from tests.conftest import built


def far_ends(spec: TopologySpec, device: str) -> list[str]:
    """Far-end device of each working port of device, in port order."""
    device_of = {p.id: p.device for p in spec.ports}
    far: dict[str, str] = {}
    for link in spec.links:
        far[link.a] = device_of[link.b]
        far[link.b] = device_of[link.a]
    ports = sorted((p for p in spec.ports if p.device == device), key=lambda p: p.index)
    return [far[p.id] for p in ports if not p.spare]


def module_lanes(spec: TopologySpec, module: str) -> tuple[str, ...]:
    """Lanes of one module."""
    return next(m.lanes for m in spec.modules if m.id == module)


def domain(spec: TopologySpec, dom: str) -> Domain:
    """One domain by id."""
    return next(d for d in spec.domains if d.id == dom)


def npus(*ids: int) -> list[str]:
    """NPU device ids."""
    return [f"npu-{i}" for i in ids]


def l1(*ids: int) -> list[str]:
    """L1 device ids."""
    return [f"l1-{i}" for i in ids]


def test_topology1_reference_devices() -> None:
    spec = built("topology1", True)
    assert far_ends(spec, "l1-1536") == (
        npus(*range(8)) + l1(*range(1552, 1556)) + [f"l2-{i}" for i in range(2304, 2308)]
    )
    assert domain(spec, "l1-1536.d0").ports == tuple(f"l1-1536.p{k}" for k in range(12, 16))
    l2_far = far_ends(spec, "l2-2304")
    assert len(l2_far) == 64
    assert sum(1 for d in spec.domains if d.device == "l2-2304") == 16
    assert domain(spec, "l2-2304.d0").ports == tuple(f"l2-2304.p{k}" for k in range(4))
    assert l2_far[:4] == l1(1536, 1538, 1540, 1542)
    assert far_ends(spec, "npu-0") == npus(*range(1, 8)) + l1(
        1536, 1537, 1560, 1561, 1584, 1585, 1608, 1609
    )


def test_topology2_reference_devices() -> None:
    spec = built("topology2", True, 1)
    assert far_ends(spec, "npu-0") == (
        npus(*range(1, 8)) + l1(408, 409, 432, 433, 456, 457) + ["npu-64", "npu-64"]
    )
    ports = {p.id: p for p in spec.ports}
    assert ports["npu-0.p15"].spare
    assert far_ends(spec, "npu-1")[-2:] == ["npu-72", "npu-72"]
    assert far_ends(spec, "npu-64")[-2:] == ["npu-0", "npu-0"]
    assert domain(spec, "npu-0.d0").spare_modules == ("npu-0.m2",)


@pytest.mark.parametrize(
    ("two_plus_two", "l1_m0", "npu_m0"),
    [
        (
            True,
            ("l1-1536.p12.0", "l1-1536.p13.0", "l1-1536.p14.0", "l1-1536.p15.0"),
            ("npu-0.p13.0", "npu-0.p14.0"),
        ),
        (
            False,
            ("l1-1536.p12.0", "l1-1536.p12.1", "l1-1536.p13.0", "l1-1536.p13.1"),
            ("npu-0.p13.0", "npu-0.p13.1"),
        ),
    ],
)
def test_module_lanes(
    two_plus_two: bool, l1_m0: tuple[str, ...], npu_m0: tuple[str, ...]
) -> None:
    assert module_lanes(built("topology1", two_plus_two), "l1-1536.m0") == l1_m0
    t2 = built("topology2", two_plus_two, 1)
    assert module_lanes(t2, "npu-0.m0") == npu_m0
    assert module_lanes(t2, "npu-0.m2") == ("npu-0.p15.0", "npu-0.p15.1")


def test_module_lanes_match_golden(golden: dict[str, Any]) -> None:
    assert list(module_lanes(built("topology1", True), "l1-1536.m0")) == golden["G1"][
        "module_lanes"
    ]
    assert list(module_lanes(built("topology1", False), "l1-1536.m0")) == golden["G2"][
        "module_lanes"
    ]
    for key, two_plus_two in (("G4a", True), ("G4b", False)):
        spec = built("topology2", two_plus_two, 1)
        assert list(module_lanes(spec, "npu-0.m0")) == golden[key]["module_lanes"]
        assert list(module_lanes(spec, golden[key]["spare"])) == golden[key]["spare_lanes"]


def test_ocs_ports() -> None:
    lanes = {x.id: x for x in built("topology2", True, 1).lanes}
    for side, npu in (("N", "npu-0"), ("S", "npu-64")):
        working = [f"{npu}.p{p}.{j}" for p in (13, 14) for j in (0, 1)]
        assert [lanes[x].ocs_port for x in working] == [f"{side}{k}" for k in range(1, 5)]
        spare = [f"{npu}.p15.0", f"{npu}.p15.1"]
        assert [lanes[x].ocs_port for x in spare] == [f"{side}257", f"{side}258"]
    assert lanes["npu-1.p13.0"].ocs_port == "N5"
    assert lanes["npu-1.p14.1"].ocs_port == "N8"
    assert lanes["npu-0.p0.0"].ocs_port is None
    assert lanes["l1-384.p12.0"].ocs_port is None


@pytest.mark.parametrize(("two_plus_two", "spares"), [(True, 1), (False, 0)])
def test_cross_connects(two_plus_two: bool, spares: int) -> None:
    xconnects = built("topology2", two_plus_two, spares).xconnects
    assert xconnects[:6] == (
        CrossConnect("N1", "S1"),
        CrossConnect("N2", "S2"),
        CrossConnect("N3", "S3"),
        CrossConnect("N4", "S4"),
        CrossConnect("N5", "S33"),
        CrossConnect("N6", "S34"),
    )
    assert len(xconnects) == 256
