"""Topology validator: the 9 rules of SPEC 9.7."""

import re
from collections import Counter

from sdnctl.config import Features
from sdnctl.model import TopologySpec
from sdnctl.topology.builder import DOMAIN_PORTS
from sdnctl.types import LaneId, ModuleId, ModuleMapping, OcsPortId, PortId, port_key

_NORTH = re.compile(r"N[1-9]\d*")
_SOUTH = re.compile(r"S[1-9]\d*")


class TopologyError(ValueError):
    """A topology breaks one of the validator rules; rule is its number in SPEC 9.7."""

    def __init__(self, rule: int, message: str) -> None:
        super().__init__(f"rule {rule}: {message}")
        self.rule = rule


def validate(spec: TopologySpec, features: Features) -> None:
    """Check the rules of SPEC 9.7 in order; raise TopologyError for the first one broken."""
    _check_links(spec)
    _check_lanes_unique(spec)
    _check_xconnects(spec)
    _check_domains(spec)
    if features.ocs and not spec.has_ocs:
        raise TopologyError(7, f"features.ocs is true, but {spec.name} has no OCS")
    _check_port_lanes(spec)
    if features.two_plus_two != (spec.mapping is ModuleMapping.TWO_PLUS_TWO):
        raise TopologyError(
            9,
            f"features.two_plus_two is {str(features.two_plus_two).lower()}, "
            f"but {spec.name} is built with {spec.mapping.value}",
        )


def _check_links(spec: TopologySpec) -> None:
    """Rule 1: a port is in at most one link."""
    uses = Counter(p for link in spec.links for p in (link.a, link.b))
    for port in sorted(uses, key=port_key):
        if uses[port] > 1:
            raise TopologyError(1, f"port {port} is in {uses[port]} links")


def _check_lanes_unique(spec: TopologySpec) -> None:
    """Rule 2: every lane appears once, and in at most one module."""
    ids = Counter(x.id for x in spec.lanes)
    for lane, n in ids.items():
        if n > 1:
            raise TopologyError(2, f"lane {lane} appears {n} times")
    owner: dict[LaneId, ModuleId] = {}
    for m in spec.modules:
        for x in m.lanes:
            if x in owner:
                raise TopologyError(2, f"lane {x} is in modules {owner[x]} and {m.id}")
            owner[x] = m.id


def _check_xconnects(spec: TopologySpec) -> None:
    """Rules 3 and 4: North-South cross-connects, one per OCS port, none on a spare lane."""
    used: set[OcsPortId] = set()
    for xc in spec.xconnects:
        if not (_NORTH.fullmatch(xc.north) and _SOUTH.fullmatch(xc.south)):
            raise TopologyError(
                3, f"cross-connect {xc.north}-{xc.south} must join a North port to a South port"
            )
        for port in (xc.north, xc.south):
            if port in used:
                raise TopologyError(3, f"OCS port {port} is in two cross-connects")
            used.add(port)
    for x in spec.lanes:
        if x.spare and x.ocs_port in used:
            raise TopologyError(4, f"spare lane {x.id} (OCS port {x.ocs_port}) is cross-connected")


def _check_domains(spec: TopologySpec) -> None:
    """Rules 5 and 6: domain size, two working modules, and module wiring per mapping."""
    tier = {d.id: d.tier for d in spec.devices}
    modules = {m.id: m for m in spec.modules}
    working: dict[str, list[ModuleId]] = {}
    for m in spec.modules:
        if not m.spare:
            working.setdefault(m.domain, []).append(m.id)
    for dom in spec.domains:
        want = DOMAIN_PORTS[tier[dom.device]]
        if len(dom.ports) != want:
            raise TopologyError(5, f"domain {dom.id} has {len(dom.ports)} ports; it needs {want}")
        found = sorted(working.get(dom.id, []))
        if len(dom.modules) != 2 or sorted(dom.modules) != found:
            raise TopologyError(
                5, f"domain {dom.id} needs exactly 2 working modules, has {found}"
            )

    lane_port = {x.id: x.port for x in spec.lanes}
    lane_module = {x.id: x.module for x in spec.lanes}
    for dom in spec.domains:
        if dom.mapping is not spec.mapping:
            raise TopologyError(
                6, f"domain {dom.id} is {dom.mapping.value} in a {spec.mapping.value} topology"
            )
        a, b = (modules[m] for m in dom.modules)
        for m in (a, b):
            for x in m.lanes:
                if lane_module.get(x) != m.id:
                    raise TopologyError(6, f"module {m.id} lists lane {x}, which is not on it")
        on_a: Counter[PortId] = Counter(lane_port[x] for x in a.lanes)
        on_b: Counter[PortId] = Counter(lane_port[x] for x in b.lanes)
        outside = sorted((set(on_a) | set(on_b)) - set(dom.ports))
        if outside:
            raise TopologyError(6, f"domain {dom.id} modules use ports outside it: {outside}")
        if dom.mapping is ModuleMapping.TWO_PLUS_TWO:
            for p in dom.ports:
                if on_a[p] != 1 or on_b[p] != 1:
                    raise TopologyError(
                        6,
                        f"domain {dom.id} is 2+2, but port {p} has {on_a[p]} lane(s) on "
                        f"{a.id} and {on_b[p]} on {b.id}; it needs one on each",
                    )
        else:
            for p in dom.ports:
                if sorted((on_a[p], on_b[p])) != [0, 2]:
                    raise TopologyError(
                        6, f"domain {dom.id} is 2:1, but port {p} does not have both lanes "
                        f"on one module"
                    )
            if len(on_a) != len(dom.ports) // 2:
                raise TopologyError(
                    6, f"domain {dom.id} is 2:1, but {a.id} serves {len(on_a)} of "
                    f"{len(dom.ports)} ports"
                )


def _check_port_lanes(spec: TopologySpec) -> None:
    """Rule 8: every port has exactly lanes 0 and 1."""
    indexes: dict[PortId, list[int]] = {p.id: [] for p in spec.ports}
    for x in spec.lanes:
        indexes.setdefault(x.port, []).append(x.index)
    for port, idx in indexes.items():
        if sorted(idx) != [0, 1]:
            raise TopologyError(8, f"port {port} has lanes {sorted(idx)}; it needs exactly 0 and 1")
