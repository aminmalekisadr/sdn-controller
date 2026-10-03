"""The OCS matrix as the controller knows it: one cross-connect per OCS port, one port per lane.

North holds rack-0 NPU optical lanes and South rack-1 (numbered by the topology builder, SPEC 3.3).
A cross-connect always joins one North port to one South port.
"""

from collections.abc import Iterable

from sdnctl.model import TopologySpec
from sdnctl.types import CrossConnect, LaneId, OcsPortId


def port_number(port: OcsPortId) -> int:
    """The number of an OCS port: N257 -> 257."""
    return int(port[1:])


def is_north(port: OcsPortId) -> bool:
    """True for a North port."""
    return port.startswith("N")


def joining(a: OcsPortId, b: OcsPortId) -> CrossConnect:
    """The cross-connect joining two ports, whichever side each is on."""
    return CrossConnect(a, b) if is_north(a) else CrossConnect(b, a)


class OcsMatrix:
    """Current cross-connects and the lane on each OCS port."""

    def __init__(self, spec: TopologySpec) -> None:
        self.lane_at: dict[OcsPortId, LaneId] = {
            x.ocs_port: x.id for x in spec.lanes if x.ocs_port is not None
        }
        self.port_of: dict[LaneId, OcsPortId] = {x: p for p, x in self.lane_at.items()}
        self._peer: dict[OcsPortId, OcsPortId] = {}
        self.replace(spec.xconnects)

    def replace(self, xconnects: Iterable[CrossConnect]) -> None:
        """Take xconnects as the whole matrix (for example a read-back)."""
        self._peer = {}
        for xc in xconnects:
            self._peer[xc.north] = xc.south
            self._peer[xc.south] = xc.north

    def peer_port(self, port: OcsPortId) -> OcsPortId | None:
        """The port cross-connected to port, or None."""
        return self._peer.get(port)

    def xconnects(self) -> set[CrossConnect]:
        """Every current cross-connect."""
        return {CrossConnect(n, s) for n, s in self._peer.items() if is_north(n)}

    def after(
        self, disconnect: Iterable[CrossConnect], connect: Iterable[CrossConnect]
    ) -> set[CrossConnect]:
        """The matrix expected once disconnect and then connect are applied."""
        return (self.xconnects() - set(disconnect)) | set(connect)
