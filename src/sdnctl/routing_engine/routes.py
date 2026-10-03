"""Route builder (SPEC 5.1): BFS by hop count from every destination NPU over the routing domain.

A device's route to destination t lists every routed neighbor one hop closer to t (ECMP), sorted by
device_key. Lanes never enter the BFS: neighbors are devices with at least one usable lane. The BFS
runs for all destinations at once, one hop per step, as a frontier-times-adjacency matrix product.
"""

import numpy as np
import numpy.typing as npt

from sdnctl.model import TopologyView
from sdnctl.tables import RouteEntry
from sdnctl.types import DeviceId, Tier, device_key

IntArray = npt.NDArray[np.int64]


def route_tables(view: TopologyView) -> dict[DeviceId, dict[DeviceId, RouteEntry]]:
    """dest NPU -> RouteEntry, for every device in the routing domain."""
    spec = view.spec
    routed = sorted((d.id for d in spec.devices if view.routed(d.id)), key=device_key)
    idx = {d: i for i, d in enumerate(routed)}
    dests = [d.id for d in spec.devices if d.tier is Tier.NPU and d.id in idx]
    dests.sort(key=device_key)
    # neighbors(v) is sorted by device_key and idx follows device_key, so these stay sorted
    nbr = [np.array([idx[u] for u in view.neighbors(v) if u in idx], np.int64) for v in routed]
    dist = hop_distances(nbr, [idx[t] for t in dests])
    return {v: _routes_of(vi, nbr[vi], dist, dests, routed) for vi, v in enumerate(routed)}


def hop_distances(nbr: list[IntArray], sources: list[int]) -> npt.NDArray[np.int32]:
    """dist[i, v] = hops from sources[i] to v along neighbor edges; -1 if unreachable."""
    n, count = len(nbr), len(sources)
    adj = np.zeros((n, n), np.float32)
    for v, ns in enumerate(nbr):
        adj[v, ns] = 1.0
    rows = np.arange(count)
    dist = np.full((count, n), -1, np.int32)
    dist[rows, sources] = 0
    frontier = np.zeros((count, n), np.float32)
    frontier[rows, sources] = 1.0
    level = 0
    while True:
        level += 1
        new = ((frontier @ adj) > 0) & (dist < 0)
        if not new.any():
            return dist
        dist[new] = level
        frontier = new.astype(np.float32)


def _routes_of(
    vi: int,
    ns: IntArray,
    dist: npt.NDArray[np.int32],
    dests: list[DeviceId],
    routed: list[DeviceId],
) -> dict[DeviceId, RouteEntry]:
    """Routes of one device: for each reachable destination, the neighbors one hop closer."""
    dv = dist[:, vi]
    reachable = dv > 0  # 0 is the device itself, -1 unreachable
    if len(ns) == 0 or not reachable.any():
        return {}
    closer = dist[:, ns] == (dv - 1)[:, None]
    # few distinct next-hop sets per device: build each tuple once
    patterns, inverse = np.unique(closer, axis=0, return_inverse=True)
    hop_sets = [tuple(routed[u] for u in ns[p]) for p in patterns]
    which = inverse.reshape(-1).tolist()
    hops = dv.tolist()
    keep = reachable.tolist()
    return {
        t: RouteEntry(t, hop_sets[which[ti]], hops[ti])
        for ti, t in enumerate(dests)
        if keep[ti]
    }
