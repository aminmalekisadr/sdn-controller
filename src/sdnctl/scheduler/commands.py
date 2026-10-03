"""Turning table changes into command payloads, and applying acknowledged commands to the copy."""

from sdnctl.messages import DeviceCommand, RouteSetEntry
from sdnctl.tables import DeviceTables, GroupChange, GroupEntry, RouteChange, RouteEntry
from sdnctl.types import DeviceId, LaneId

GroupEntries = dict[DeviceId, tuple[LaneId, ...]]
RouteEntries = dict[DeviceId, RouteSetEntry]


def group_entries(changes: list[GroupChange]) -> GroupEntries:
    """GROUP_SET entries for some group changes; an empty lane list removes the group."""
    return {c.neighbor: c.new.lanes if c.new is not None else () for c in changes}


def route_entries(changes: list[RouteChange]) -> RouteEntries:
    """ROUTE_SET entries for some route changes; no neighbors removes the route."""
    return {
        c.dest: RouteSetEntry(c.new.neighbors, c.new.hops)
        if c.new is not None
        else RouteSetEntry((), 0)
        for c in changes
    }


def full_group_entries(target: DeviceTables, installed: DeviceTables | None) -> GroupEntries:
    """Every target group, plus removals of groups only the copy has: a full replacement."""
    entries: GroupEntries = {n: g.lanes for n, g in target.groups.items()}
    if installed is not None:
        entries.update({n: () for n in installed.groups if n not in target.groups})
    return entries


def full_route_entries(target: DeviceTables, installed: DeviceTables | None) -> RouteEntries:
    """Every target route, plus removals of routes only the copy has: a full replacement."""
    entries: RouteEntries = {
        t: RouteSetEntry(r.neighbors, r.hops) for t, r in target.routes.items()
    }
    if installed is not None:
        gone = [t for t in installed.routes if t not in target.routes]
        entries.update({t: RouteSetEntry((), 0) for t in gone})
    return entries


def apply_to_copy(tables: DeviceTables, cmd: DeviceCommand) -> None:
    """Apply an acknowledged command to the controller's copy, groups before routes."""
    if cmd.group_set is not None:
        for neighbor, lanes in cmd.group_set.entries.items():
            if lanes:
                tables.groups[neighbor] = GroupEntry(neighbor, tuple(lanes))
            else:
                tables.groups.pop(neighbor, None)
    if cmd.route_set is not None:
        for dest, entry in cmd.route_set.entries.items():
            if entry.neighbors:
                tables.routes[dest] = RouteEntry(dest, entry.neighbors, entry.hops)
            else:
                tables.routes.pop(dest, None)
    if cmd.group_set is not None or cmd.route_set is not None:
        tables.version = max(tables.version, cmd.version)
