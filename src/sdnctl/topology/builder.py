"""Topology builders: topology 1 (Huawei 910D, no OCS) and topology 2 (OCS-based SURE).

They re-implement the wiring of the two scripts in reference/topologies (SPEC Appendix A and B):
the same edges in the same order, collapsed to NPU, L1 and L2 devices as in SPEC 3.1. Ports are
numbered per device in edge order; optical ports are then cut into domains (SPEC 3.4).
"""

from dataclasses import dataclass

from sdnctl.model import Device, Domain, Lane, Link, Module, Port, TopologySpec
from sdnctl.types import (
    LANES_PER_PORT,
    CrossConnect,
    DeviceId,
    DomainId,
    LaneId,
    LinkKind,
    ModuleId,
    ModuleMapping,
    OcsPortId,
    PortId,
    Role,
    Tier,
    lane_key,
)

DOMAIN_PORTS = {Tier.NPU: 2, Tier.L1: 4, Tier.L2: 4}  # optical ports per domain


@dataclass(frozen=True)
class _Layout:
    """The script parameters both topologies share; only the rack count differs."""

    racks: int
    boards_per_rack: int = 8
    npus_per_board: int = 8
    planes: int = 4
    unions_per_plane: int = 16
    ext_per_plane: int = 8
    hrs_per_plane: int = 8

    @property
    def npus_per_rack(self) -> int:
        return self.boards_per_rack * self.npus_per_board

    @property
    def total_npus(self) -> int:
        return self.racks * self.npus_per_rack

    @property
    def sw_per_plane(self) -> int:
        return self.unions_per_plane + self.ext_per_plane

    @property
    def sw_per_rack(self) -> int:
        return self.planes * self.sw_per_plane

    @property
    def sw_base(self) -> int:
        # GRID_START = TOTAL_COMPUTES, then 2 IO dies per compute die
        return 3 * self.total_npus

    @property
    def hrs_start(self) -> int:
        return self.sw_base + self.racks * self.sw_per_rack

    @property
    def num_hrs(self) -> int:
        # one switching domain of up to 8 racks
        return self.planes * self.hrs_per_plane

    def plane_base(self, rack: int, plane: int) -> int:
        """First switch id of one plane in one rack."""
        return self.sw_base + rack * self.sw_per_rack + plane * self.sw_per_plane


TOPOLOGY1 = _Layout(racks=8)
TOPOLOGY2 = _Layout(racks=2)


def build_topology1(two_plus_two: bool) -> TopologySpec:
    """Topology 1, the Huawei 910D design: 8 racks, 32 L2 switches, no OCS (SPEC 3.2)."""
    return _assemble(
        name="topology1",
        lay=TOPOLOGY1,
        edges=_script_edges(TOPOLOGY1, sure=False),
        mapping=_mapping(two_plus_two),
        spares=0,
        has_ocs=False,
        routed_tiers=frozenset({Tier.NPU, Tier.L1, Tier.L2}),
    )


def build_topology2(two_plus_two: bool, spare_modules_per_domain: int = 0) -> TopologySpec:
    """Topology 2, the OCS-based SURE design: 2 racks, SURE links through the OCS (SPEC 3.3)."""
    if spare_modules_per_domain < 0:
        raise ValueError("spare_modules_per_domain must be 0 or more")
    return _assemble(
        name="topology2",
        lay=TOPOLOGY2,
        edges=_script_edges(TOPOLOGY2, sure=True),
        mapping=_mapping(two_plus_two),
        spares=spare_modules_per_domain,
        has_ocs=True,
        routed_tiers=frozenset({Tier.NPU, Tier.L1}),
    )


def _mapping(two_plus_two: bool) -> ModuleMapping:
    return ModuleMapping.TWO_PLUS_TWO if two_plus_two else ModuleMapping.TWO_TO_ONE


def _script_edges(lay: _Layout, sure: bool) -> list[tuple[DeviceId, DeviceId]]:
    """The script's edges in creation order, collapsed to devices.

    The compute-to-IO-die edges disappear, because an NPU is its compute die plus its IO dies.
    sure=True is topology 2: no NPU link to plane d % 4, and two SURE links per rack-0 NPU.
    """
    edges: list[tuple[DeviceId, DeviceId]] = []
    boards = lay.racks * lay.boards_per_rack
    # step 7: d2d, every pair of NPUs on a board
    for board in range(boards):
        first = board * lay.npus_per_board
        last = first + lay.npus_per_board
        for i in range(first, last):
            for j in range(i + 1, last):
                edges.append((f"npu-{i}", f"npu-{j}"))
    # step 8: every NPU of board b to the left and right union of b, plane by plane
    for board in range(boards):
        rack, b = divmod(board, lay.boards_per_rack)
        for plane in range(lay.planes):
            left = lay.plane_base(rack, plane) + 2 * b
            for i in range(lay.npus_per_board):
                if sure and plane == i % lay.planes:
                    continue  # topology 2 gives this plane's ports to the optics
                npu = f"npu-{board * lay.npus_per_board + i}"
                edges.append((npu, f"l1-{left}"))
                edges.append((npu, f"l1-{left + 1}"))
    # step 9: left unions to the left EXT, right unions to the right EXT
    half_ext = lay.ext_per_plane // 2
    for rack in range(lay.racks):
        for plane in range(lay.planes):
            base = lay.plane_base(rack, plane)
            for pair in range(lay.unions_per_plane // 2):
                left = base + 2 * pair
                for e in range(half_ext):
                    ext = base + lay.unions_per_plane + e
                    edges.append((f"l1-{left}", f"l1-{ext}"))
                    edges.append((f"l1-{left + 1}", f"l1-{ext + half_ext}"))
    # step 10: left unions to the left L2 switches of their plane, right to the right
    half_hrs = lay.hrs_per_plane // 2
    for rack in range(lay.racks):
        for plane in range(lay.planes):
            base = lay.plane_base(rack, plane)
            hrs_base = lay.hrs_start + plane * lay.hrs_per_plane
            for pair in range(lay.unions_per_plane // 2):
                left = base + 2 * pair
                for h in range(half_hrs):
                    edges.append((f"l1-{left}", f"l2-{hrs_base + h}"))
                    edges.append((f"l1-{left + 1}", f"l2-{hrs_base + h + half_hrs}"))
    # step 11 (topology 2): two SURE links from rack-0 NPU d to its transpose in rack 1
    if sure:
        for d in range(lay.npus_per_rack):
            b, i = divmod(d, lay.npus_per_board)
            peer = lay.npus_per_rack + i * lay.npus_per_board + b
            edges.append((f"npu-{d}", f"npu-{peer}"))
            edges.append((f"npu-{d}", f"npu-{peer}"))
    return edges


def _devices(lay: _Layout) -> dict[DeviceId, Device]:
    """Every device, in device_key order."""
    out: dict[DeviceId, Device] = {}
    for d in range(lay.total_npus):
        out[f"npu-{d}"] = Device(
            id=f"npu-{d}",
            tier=Tier.NPU,
            role=Role.NPU,
            rack=d // lay.npus_per_rack,
            plane=None,
            board=d // lay.npus_per_board,
            script_id=d,
        )
    for s in range(lay.sw_base, lay.hrs_start):
        rack, k = divmod(s - lay.sw_base, lay.sw_per_rack)
        plane, idx = divmod(k, lay.sw_per_plane)
        role = Role.UNION if idx < lay.unions_per_plane else Role.EXT
        out[f"l1-{s}"] = Device(f"l1-{s}", Tier.L1, role, rack, plane, None, s)
    for s in range(lay.hrs_start, lay.hrs_start + lay.num_hrs):
        plane = (s - lay.hrs_start) // lay.hrs_per_plane
        out[f"l2-{s}"] = Device(f"l2-{s}", Tier.L2, Role.HRS, None, plane, None, s)
    return out


def _kind(a: Device, b: Device) -> LinkKind:
    """Link kind by the ends' tiers (SPEC 3.1)."""
    if a.tier is Tier.NPU and b.tier is Tier.NPU:
        return LinkKind.D2D if a.rack == b.rack else LinkKind.OPTICAL
    if {a.tier, b.tier} == {Tier.L1, Tier.L2}:
        return LinkKind.OPTICAL
    return LinkKind.COPPER


def _split_lanes(
    ports: list[PortId], mapping: ModuleMapping
) -> tuple[tuple[LaneId, ...], tuple[LaneId, ...]]:
    """Lanes of modules A and B for a domain's ports (SPEC 3.4)."""
    if mapping is ModuleMapping.TWO_PLUS_TWO:
        return tuple(f"{p}.0" for p in ports), tuple(f"{p}.1" for p in ports)
    half = len(ports) // 2
    a = tuple(f"{p}.{j}" for p in ports[:half] for j in range(LANES_PER_PORT))
    b = tuple(f"{p}.{j}" for p in ports[half:] for j in range(LANES_PER_PORT))
    return a, b


def _assemble(
    name: str,
    lay: _Layout,
    edges: list[tuple[DeviceId, DeviceId]],
    mapping: ModuleMapping,
    spares: int,
    has_ocs: bool,
    routed_tiers: frozenset[Tier],
) -> TopologySpec:
    """Turn device edges into ports, links, lanes, domains, modules and cross-connects."""
    devices = _devices(lay)
    ports: dict[DeviceId, list[Port]] = {d: [] for d in devices}
    port_peer: dict[PortId, PortId] = {}
    links: list[Link] = []
    for a, b in edges:
        kind = _kind(devices[a], devices[b])
        pa = Port(f"{a}.p{len(ports[a])}", a, len(ports[a]), kind, spare=False)
        ports[a].append(pa)
        pb = Port(f"{b}.p{len(ports[b])}", b, len(ports[b]), kind, spare=False)
        ports[b].append(pb)
        links.append(Link(pa.id, pb.id, kind))
        port_peer[pa.id] = pb.id
        port_peer[pb.id] = pa.id

    modules: list[Module] = []
    domains: list[Domain] = []
    module_of: dict[LaneId, ModuleId] = {}
    for dev, device in devices.items():
        optical = [p.id for p in ports[dev] if p.kind is LinkKind.OPTICAL]
        if not optical:
            continue
        size = DOMAIN_PORTS[device.tier]
        if len(optical) % size:
            raise ValueError(f"{dev} has {len(optical)} optical ports, not a multiple of {size}")
        ndom = len(optical) // size
        # spare modules exist only on OCS-attached domains: the NPU domains of topology 2
        dom_spares = spares if has_ocs and device.tier is Tier.NPU else 0
        for k in range(ndom):
            dom: DomainId = f"{dev}.d{k}"
            chunk = optical[k * size : (k + 1) * size]
            working = (f"{dev}.m{2 * k}", f"{dev}.m{2 * k + 1}")
            for m, mod_lanes in zip(working, _split_lanes(chunk, mapping), strict=True):
                modules.append(Module(m, dev, dom, mod_lanes, spare=False))
                module_of.update((x, m) for x in mod_lanes)
            spare_ids: list[ModuleId] = []
            for s in range(dom_spares):
                # numbered after all working modules; ports after all working ports
                m = f"{dev}.m{2 * ndom + k * dom_spares + s}"
                lanes_list: list[LaneId] = []
                for _ in range(size // 2):  # as many lanes as a working module
                    port = Port(f"{dev}.p{len(ports[dev])}", dev, len(ports[dev]),
                                LinkKind.OPTICAL, spare=True)
                    ports[dev].append(port)
                    lanes_list.extend(f"{port.id}.{j}" for j in range(LANES_PER_PORT))
                modules.append(Module(m, dev, dom, tuple(lanes_list), spare=True))
                module_of.update((x, m) for x in lanes_list)
                spare_ids.append(m)
            domains.append(Domain(dom, dev, tuple(chunk), working, tuple(spare_ids), mapping))

    all_ports = [p for dev in devices for p in ports[dev]]
    ocs_port, xconnects = _ocs(devices, all_ports, port_peer) if has_ocs else ({}, [])
    lanes = [
        Lane(
            id=f"{p.id}.{j}",
            device=p.device,
            port=p.id,
            index=j,
            module=module_of.get(f"{p.id}.{j}"),
            ocs_port=ocs_port.get(f"{p.id}.{j}"),
            spare=p.spare,
        )
        for p in all_ports
        for j in range(LANES_PER_PORT)
    ]
    return TopologySpec(
        name=name,
        mapping=mapping,
        spare_modules_per_domain=spares,
        devices=tuple(devices.values()),
        ports=tuple(all_ports),
        links=tuple(links),
        lanes=tuple(lanes),
        modules=tuple(modules),
        domains=tuple(domains),
        xconnects=tuple(xconnects),
        has_ocs=has_ocs,
        routed_tiers=routed_tiers,
    )


def _ocs(
    devices: dict[DeviceId, Device], ports: list[Port], port_peer: dict[PortId, PortId]
) -> tuple[dict[LaneId, OcsPortId], list[CrossConnect]]:
    """OCS port per NPU optical lane, and the cross-connects of the built links (SPEC 3.3).

    North holds rack-0 NPU lanes, South rack-1. Working lanes are numbered first in lane
    order, then spare lanes. Each working North lane is cross-connected to its link peer.
    """
    ocs_port: dict[LaneId, OcsPortId] = {}
    north_working: list[LaneId] = []
    for side, rack in (("N", 0), ("S", 1)):
        side_ports = [
            p
            for p in ports
            if p.kind is LinkKind.OPTICAL
            and devices[p.device].tier is Tier.NPU
            and devices[p.device].rack == rack
        ]
        working = sorted(
            (f"{p.id}.{j}" for p in side_ports if not p.spare for j in range(LANES_PER_PORT)),
            key=lane_key,
        )
        spare = sorted(
            (f"{p.id}.{j}" for p in side_ports if p.spare for j in range(LANES_PER_PORT)),
            key=lane_key,
        )
        for i, x in enumerate(working + spare):
            ocs_port[x] = f"{side}{i + 1}"
        if side == "N":
            north_working = working
    xconnects = []
    for x in north_working:
        port, j = x.rsplit(".", 1)
        xconnects.append(CrossConnect(ocs_port[x], ocs_port[f"{port_peer[port]}.{j}"]))
    return ocs_port, xconnects
