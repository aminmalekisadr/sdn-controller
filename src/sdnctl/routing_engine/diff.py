"""Diff and wave order (SPEC 5.1, 7.4)."""

from sdnctl.tables import (
    AdjacencyChange,
    GroupChange,
    GroupEntry,
    RouteChange,
    RouteEntry,
    TableDiff,
    Tables,
    Wave,
)
from sdnctl.types import DeviceId, device_key


def diff_tables(installed: Tables, target: Tables) -> TableDiff:
    """Every route and group entry that differs, per device; unchanged devices are left out."""
    routes: dict[DeviceId, list[RouteChange]] = {}
    groups: dict[DeviceId, list[GroupChange]] = {}
    for d in sorted(set(installed) | set(target), key=device_key):
        old, new = installed.get(d), target.get(d)
        old_routes: dict[DeviceId, RouteEntry] = old.routes if old is not None else {}
        new_routes: dict[DeviceId, RouteEntry] = new.routes if new is not None else {}
        if old_routes != new_routes:
            routes[d] = [
                RouteChange(t, old_routes.get(t), new_routes.get(t))
                for t in sorted(set(old_routes) | set(new_routes), key=device_key)
                if old_routes.get(t) != new_routes.get(t)
            ]
        old_groups: dict[DeviceId, GroupEntry] = old.groups if old is not None else {}
        new_groups: dict[DeviceId, GroupEntry] = new.groups if new is not None else {}
        if old_groups != new_groups:
            groups[d] = [
                GroupChange(n, old_groups.get(n), new_groups.get(n))
                for n in sorted(set(old_groups) | set(new_groups), key=device_key)
                if old_groups.get(n) != new_groups.get(n)
            ]
    return TableDiff(routes=routes, groups=groups)


def order_waves(diff: TableDiff, change: AdjacencyChange) -> list[Wave]:
    """Safe install order (SPEC 7.4).

    Neighbor lost: wave 1 = changed devices that are not endpoints of a lost adjacency (transit),
    wave 2 = those endpoints. Neighbor restored: the endpoints first, then transit. No adjacency
    change (groups only): one wave. Empty waves are dropped; devices are in device_key order.
    """
    if change.lost and change.restored:
        raise ValueError("v1 orders either a loss or a restore of neighbors, not both at once")
    changed = diff.devices()
    if not change.lost and not change.restored:
        steps = [changed]
    else:
        ends = {v for v, _ in change.lost | change.restored}
        transit, endpoints = changed - ends, changed & ends
        steps = [transit, endpoints] if change.lost else [endpoints, transit]
    waves = [step for step in steps if step]
    return [
        Wave(index=i, devices=tuple(sorted(step, key=device_key)))
        for i, step in enumerate(waves, start=1)
    ]
