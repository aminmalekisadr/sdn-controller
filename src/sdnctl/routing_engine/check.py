"""Loop and blackhole checker (SPEC 11.0, "Pair counts").

For a destination t, a device's live neighbors are its route neighbors with a non-empty group in
the tables given (each device's tables may be old or new). A (source NPU, destination NPU) pair is
looping if some walk from the source along live neighbors revisits a device, and blackholed if some
walk reaches a device other than t with no live neighbor (no route counts as none).

All destinations are checked at once on one graph whose nodes are (destination, device) pairs.
A node can walk into a loop exactly when it survives repeatedly peeling off nodes that have no
successors left; a node is blackholed when it reaches a drop node.
"""

import numpy as np
import numpy.typing as npt

from sdnctl.model import TopologyView
from sdnctl.tables import ForwardingReport, Tables
from sdnctl.types import DeviceId, Tier, device_key

BoolArray = npt.NDArray[np.bool_]
IntArray = npt.NDArray[np.int64]


def check_tables(tables: Tables, view: TopologyView) -> ForwardingReport:
    """Looping and blackholed ordered NPU pairs for the given tables."""
    spec = view.spec
    routed = sorted((d.id for d in spec.devices if view.routed(d.id)), key=device_key)
    idx = {d: i for i, d in enumerate(routed)}
    dests = sorted(
        (d.id for d in spec.devices if d.tier is Tier.NPU and d.id in idx), key=device_key
    )
    dest_index = {t: i for i, t in enumerate(dests)}
    n, count = len(routed), len(dests)
    drop = np.zeros(count * n, np.bool_)
    src_parts: list[IntArray] = []
    dst_parts: list[IntArray] = []
    for vi, v in enumerate(routed):
        tab = tables.get(v)
        routes = tab.routes if tab is not None else {}
        live = {u for u, g in tab.groups.items() if g.lanes} if tab is not None else set()
        own = dest_index.get(v)
        # group destinations by their live next hops: few distinct sets per device
        by_hops: dict[tuple[int, ...], list[int]] = {}
        filtered: dict[tuple[DeviceId, ...], tuple[int, ...]] = {}
        for t, entry in routes.items():
            ti = dest_index.get(t)
            if ti is None or ti == own:
                continue
            hops = filtered.get(entry.neighbors)
            if hops is None:
                hops = tuple(idx[u] for u in entry.neighbors if u in live and u in idx)
                filtered[entry.neighbors] = hops
            by_hops.setdefault(hops, []).append(ti)
        covered = np.zeros(count, np.bool_)
        if own is not None:
            covered[own] = True  # the destination itself
        for hops, tis in by_hops.items():
            ta = np.array(tis, np.int64)
            covered[ta] = True
            if not hops:
                drop[ta * n + vi] = True
            for u in hops:
                src_parts.append(ta * n + vi)
                dst_parts.append(ta * n + u)
        drop[np.flatnonzero(~covered) * n + vi] = True  # no route at all
    src = np.concatenate(src_parts) if src_parts else np.zeros(0, np.int64)
    dst = np.concatenate(dst_parts) if dst_parts else np.zeros(0, np.int64)
    looping = _reaches_cycle(src, dst, count * n)
    holed = _reaches(drop, src, dst)
    sources = np.array([idx[t] for t in dests], np.int64)
    return ForwardingReport(
        looping_pairs=_count_pairs(looping, count, n, sources),
        blackholed_pairs=_count_pairs(holed, count, n, sources),
    )


def _reaches_cycle(src: IntArray, dst: IntArray, size: int) -> BoolArray:
    """Nodes from which some walk revisits a node: what survives peeling successor-less nodes."""
    outdeg = np.bincount(src, minlength=size)
    alive = np.ones(size, np.bool_)
    frontier = outdeg == 0
    while frontier.any():
        alive[frontier] = False
        hit = frontier[dst]
        if hit.any():
            outdeg -= np.bincount(src[hit], minlength=size)
        frontier = alive & (outdeg == 0)
    return alive


def _reaches(seeds: BoolArray, src: IntArray, dst: IntArray) -> BoolArray:
    """Nodes from which some walk reaches a seed (seeds included)."""
    reach = seeds.copy()
    frontier = seeds.copy()
    while frontier.any():
        new = np.zeros_like(reach)
        new[src[frontier[dst]]] = True
        new &= ~reach
        reach |= new
        frontier = new
    return reach


def _count_pairs(flags: BoolArray, count: int, n: int, sources: IntArray) -> int:
    """Number of (source NPU, destination) pairs flagged, source != destination."""
    per_pair = flags.reshape(count, n)[:, sources]  # [destination, source NPU]
    return int(per_pair.sum() - np.trace(per_pair))
