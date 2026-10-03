"""Validator: accepts the 6 generated topologies, rejects one broken case per rule (SPEC 9.7)."""

import dataclasses
import re
from collections.abc import Callable
from typing import TypeVar

import pytest

from sdnctl.config import Features
from sdnctl.model import TopologySpec
from sdnctl.topology import TopologyError, validate
from sdnctl.types import CrossConnect, ModuleMapping
from tests.conftest import VARIANTS, built


def features_of(spec: TopologySpec) -> Features:
    """The features a spec is built for."""
    return Features(two_plus_two=spec.mapping is ModuleMapping.TWO_PLUS_TWO, ocs=spec.has_ocs)


@pytest.mark.parametrize(("name", "two_plus_two", "spares"), VARIANTS)
def test_accepts_generated_topologies(name: str, two_plus_two: bool, spares: int) -> None:
    spec = built(name, two_plus_two, spares)
    validate(spec, features_of(spec))


def t2() -> TopologySpec:
    """Topology 2, 2+2, one spare: the base of most broken cases."""
    return built("topology2", True, 1)


T = TypeVar("T")


def replace_at(items: tuple[T, ...], i: int, new: T) -> tuple[T, ...]:
    """items with items[i] replaced."""
    return items[:i] + (new,) + items[i + 1 :]


def port_in_two_links() -> tuple[TopologySpec, Features]:
    spec = t2()
    return dataclasses.replace(spec, links=spec.links + spec.links[:1]), features_of(spec)


def lane_twice() -> tuple[TopologySpec, Features]:
    spec = t2()
    return dataclasses.replace(spec, lanes=spec.lanes + spec.lanes[:1]), features_of(spec)


def north_to_north() -> tuple[TopologySpec, Features]:
    spec = t2()
    xcs = replace_at(spec.xconnects, 0, CrossConnect("N1", "N300"))
    return dataclasses.replace(spec, xconnects=xcs), features_of(spec)


def spare_cross_connected() -> tuple[TopologySpec, Features]:
    spec = t2()
    xcs = replace_at(spec.xconnects, 0, CrossConnect("N257", "S1"))
    return dataclasses.replace(spec, xconnects=xcs), features_of(spec)


def domain_too_small() -> tuple[TopologySpec, Features]:
    spec = t2()
    i = next(k for k, d in enumerate(spec.domains) if d.id == "l1-384.d0")
    dom = dataclasses.replace(spec.domains[i], ports=spec.domains[i].ports[:3])
    domains = replace_at(spec.domains, i, dom)
    return dataclasses.replace(spec, domains=domains), features_of(spec)


def wiring_breaks_mapping() -> tuple[TopologySpec, Features]:
    # l1-384.d0 is p10-p13; give its modules 2:1 wiring in a 2+2 topology
    spec = t2()
    a_lanes = ("l1-384.p10.0", "l1-384.p10.1", "l1-384.p11.0", "l1-384.p11.1")
    b_lanes = ("l1-384.p12.0", "l1-384.p12.1", "l1-384.p13.0", "l1-384.p13.1")
    new = {"l1-384.m0": a_lanes, "l1-384.m1": b_lanes}
    modules = tuple(
        dataclasses.replace(m, lanes=new[m.id]) if m.id in new else m for m in spec.modules
    )
    owner = {x: m for m, lanes in new.items() for x in lanes}
    lanes = tuple(
        dataclasses.replace(x, module=owner[x.id]) if x.id in owner else x for x in spec.lanes
    )
    return dataclasses.replace(spec, modules=modules, lanes=lanes), features_of(spec)


def ocs_without_ocs() -> tuple[TopologySpec, Features]:
    return built("topology1", True), Features(two_plus_two=True, ocs=True)


def port_with_one_lane() -> tuple[TopologySpec, Features]:
    spec = t2()
    lanes = tuple(x for x in spec.lanes if x.id != "npu-0.p0.1")
    return dataclasses.replace(spec, lanes=lanes), features_of(spec)


def mapping_mismatch() -> tuple[TopologySpec, Features]:
    return t2(), Features(two_plus_two=False, ocs=True)


BROKEN: list[tuple[int, Callable[[], tuple[TopologySpec, Features]], str]] = [
    (1, port_in_two_links, "is in 2 links"),
    (2, lane_twice, "appears 2 times"),
    (3, north_to_north, "must join a North port to a South port"),
    (4, spare_cross_connected, "spare lane npu-0.p15.0"),
    (5, domain_too_small, "l1-384.d0 has 3 ports"),
    (6, wiring_breaks_mapping, "l1-384.d0 is 2+2"),
    (7, ocs_without_ocs, "has no OCS"),
    (8, port_with_one_lane, "npu-0.p0 has lanes [0]"),
    (9, mapping_mismatch, "built with 2+2"),
]


@pytest.mark.parametrize(
    ("rule", "make", "message"), BROKEN, ids=[f"rule{r}" for r, _, _ in BROKEN]
)
def test_rejects_broken_topology(
    rule: int, make: Callable[[], tuple[TopologySpec, Features]], message: str
) -> None:
    spec, features = make()
    with pytest.raises(TopologyError, match=re.escape(message)) as err:
        validate(spec, features)
    assert err.value.rule == rule
