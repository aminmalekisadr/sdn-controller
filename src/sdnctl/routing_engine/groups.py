"""Group builder (SPEC 5.1): every device's usable lanes toward each neighbor, equal weights."""

from sdnctl.model import TopologyView
from sdnctl.tables import GroupEntry
from sdnctl.types import DeviceId


def group_tables(view: TopologyView) -> dict[DeviceId, dict[DeviceId, GroupEntry]]:
    """neighbor -> GroupEntry for every device (routed or not); no empty groups."""
    out: dict[DeviceId, dict[DeviceId, GroupEntry]] = {}
    for d in view.spec.devices:
        groups: dict[DeviceId, GroupEntry] = {}
        for n in view.neighbors(d.id):
            lanes = view.usable_lanes(d.id, n)
            if lanes:
                groups[n] = GroupEntry(n, lanes)
        out[d.id] = groups
    return out
