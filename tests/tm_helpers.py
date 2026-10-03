"""Feed a TopologyManager the reports a module failure and its repair produce (SPEC 7.5)."""

from sdnctl.messages import CONTROLLER, Header, LaneDown, LaneUp
from sdnctl.model import Incident, TopologySpec
from sdnctl.topology.view import SpecIndex
from sdnctl.topology_manager import TopologyManager
from sdnctl.types import DeviceId, DownCause, LaneId, MsgType, device_key, lane_key


def _header(t: MsgType, device: str, now: int) -> Header:
    return Header(t, 1, 1, None, device, CONTROLLER, now)


def report_failure(
    tm: TopologyManager, spec: TopologySpec, modules: tuple[str, ...], now: int = 1000
) -> tuple[Incident, dict[DeviceId, list[LaneId]]]:
    """Modules of one device fail: MODULE_FAULT from it, LOSS_OF_LIGHT from each peer device.

    Returns the incident and the lanes each device lost.
    """
    index = SpecIndex(spec)
    failed_device = index.modules[modules[0]].device
    view = tm.view()
    lost: dict[DeviceId, list[LaneId]] = {}
    for m in modules:
        for x in index.modules[m].lanes:
            lost.setdefault(failed_device, []).append(x)
            y = view.peer_of(x)
            if y is not None:
                lost.setdefault(index.lanes[y].device, []).append(y)
    for dev in sorted(lost, key=device_key):
        own = dev == failed_device
        tm.on_lane_down(
            LaneDown(
                _header(MsgType.LANE_DOWN, dev, now),
                dev,
                tuple(sorted(lost[dev], key=lane_key)),
                DownCause.MODULE_FAULT if own else DownCause.LOSS_OF_LIGHT,
                modules if own else (),
            ),
            now,
        )
    [incident] = tm.poll_incidents(now)
    return incident, lost


def report_repair(
    tm: TopologyManager, lost: dict[DeviceId, list[LaneId]], now: int = 10_063_000
) -> None:
    """The lost lanes come back on the same peers: one LANE_UP per device."""
    view = tm.view()
    for dev in sorted(lost, key=device_key):
        lanes = tuple(sorted(lost[dev], key=lane_key))
        peers = tuple(view.peer_of(x) or "" for x in lanes)
        tm.on_lane_up(LaneUp(_header(MsgType.LANE_UP, dev, now), dev, lanes, peers), now)
