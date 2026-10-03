"""The 10 protocol messages of SPEC Section 9.3, their common header and the command batch."""

from dataclasses import dataclass
from typing import ClassVar, TypeAlias

from sdnctl.types import (
    CrossConnect,
    DeviceId,
    DownCause,
    IncidentId,
    LaneId,
    ModuleId,
    MsgType,
    SimTime,
    Version,
)

CONTROLLER = "controller"  # Header.src / Header.dst of the controller
OCS = "ocs"  # Header.src / Header.dst of the OCS


@dataclass(frozen=True)
class Header:
    """Common header of every message."""

    type: MsgType
    seq: int
    version: Version
    incident: IncidentId | None
    src: str  # a device id, CONTROLLER or OCS
    dst: str
    time: SimTime


class _Message:
    """Checks that a message's header type matches its class."""

    TYPE: ClassVar[MsgType]
    header: Header

    def __post_init__(self) -> None:
        if self.header.type is not self.TYPE:
            raise ValueError(
                f"{type(self).__name__} needs header type {self.TYPE.value}, "
                f"got {self.header.type.value}"
            )


@dataclass(frozen=True)
class Hello(_Message):
    """HELLO, device to device: the sender's lane, checked against the expected peer lane."""

    TYPE: ClassVar[MsgType] = MsgType.HELLO
    header: Header
    device: DeviceId
    lane: LaneId


@dataclass(frozen=True)
class LaneDown(_Message):
    """LANE_DOWN, device to controller: every lane lost in one fault event."""

    TYPE: ClassVar[MsgType] = MsgType.LANE_DOWN
    header: Header
    device: DeviceId
    lanes: tuple[LaneId, ...]
    cause: DownCause
    modules: tuple[ModuleId, ...] = ()  # the failed modules, for MODULE_FAULT


@dataclass(frozen=True)
class LaneUp(_Message):
    """LANE_UP, device to controller: verified lanes and the peer lanes seen in HELLO."""

    TYPE: ClassVar[MsgType] = MsgType.LANE_UP
    header: Header
    device: DeviceId
    lanes: tuple[LaneId, ...]
    peer_lanes: tuple[LaneId, ...]  # peer_lanes[i] faces lanes[i]

    def __post_init__(self) -> None:
        super().__post_init__()
        if len(self.lanes) != len(self.peer_lanes):
            raise ValueError("LaneUp needs one peer lane per lane")


@dataclass(frozen=True)
class Prepare(_Message):
    """PREPARE, controller to device: open spare lanes and expect these peer lanes."""

    TYPE: ClassVar[MsgType] = MsgType.PREPARE
    header: Header
    open_lanes: tuple[LaneId, ...]
    expected_peer: dict[LaneId, LaneId]  # own lane -> expected peer lane


@dataclass(frozen=True)
class OcsSet(_Message):
    """OCS_SET, controller to OCS: cross-connects to remove, then cross-connects to add."""

    TYPE: ClassVar[MsgType] = MsgType.OCS_SET
    header: Header
    disconnect: tuple[CrossConnect, ...]
    connect: tuple[CrossConnect, ...]


@dataclass(frozen=True)
class OcsPairResult:
    """Result of one cross-connect change in an OCS_SET."""

    xconnect: CrossConnect
    ok: bool
    error: str = ""


@dataclass(frozen=True)
class OcsDone(_Message):
    """OCS_DONE, OCS to controller: per-pair results and the applied matrix version."""

    TYPE: ClassVar[MsgType] = MsgType.OCS_DONE
    header: Header
    disconnected: tuple[OcsPairResult, ...]
    connected: tuple[OcsPairResult, ...]
    matrix_version: int

    @property
    def ok(self) -> bool:
        """True when every pair was applied."""
        return all(r.ok for r in self.disconnected + self.connected)


@dataclass(frozen=True)
class GroupSet(_Message):
    """GROUP_SET, controller to device: neighbor -> lanes; an empty list removes the group."""

    TYPE: ClassVar[MsgType] = MsgType.GROUP_SET
    header: Header
    entries: dict[DeviceId, tuple[LaneId, ...]]


@dataclass(frozen=True)
class RouteSetEntry:
    """One route in a ROUTE_SET: next-hop neighbors and hop count; no neighbors removes it."""

    neighbors: tuple[DeviceId, ...]
    hops: int


@dataclass(frozen=True)
class RouteSet(_Message):
    """ROUTE_SET, controller to device: destination NPU -> route."""

    TYPE: ClassVar[MsgType] = MsgType.ROUTE_SET
    header: Header
    entries: dict[DeviceId, RouteSetEntry]


@dataclass(frozen=True)
class Ack(_Message):
    """ACK, device to controller: the result of one command (or batch)."""

    TYPE: ClassVar[MsgType] = MsgType.ACK
    header: Header
    acked_seq: int
    ok: bool
    error: str = ""


@dataclass(frozen=True)
class Keepalive(_Message):
    """KEEPALIVE, controller and device: liveness of the controller session."""

    TYPE: ClassVar[MsgType] = MsgType.KEEPALIVE
    header: Header
    controller_id: str
    epoch: int


Message: TypeAlias = (
    Hello | LaneDown | LaneUp | Prepare | OcsSet | OcsDone | GroupSet | RouteSet | Ack | Keepalive
)
DeviceEvent: TypeAlias = LaneDown | LaneUp | Ack
CommandPart: TypeAlias = Prepare | GroupSet | RouteSet | Keepalive

_ALL_MESSAGES: tuple[type[Message], ...] = (
    Hello,
    LaneDown,
    LaneUp,
    Prepare,
    OcsSet,
    OcsDone,
    GroupSet,
    RouteSet,
    Ack,
    Keepalive,
)
MESSAGE_CLASSES: dict[MsgType, type[Message]] = {cls.TYPE: cls for cls in _ALL_MESSAGES}


@dataclass(frozen=True)
class DeviceCommand:
    """One command to one device; several parts form a batch that counts as one message."""

    prepare: Prepare | None = None
    group_set: GroupSet | None = None
    route_set: RouteSet | None = None
    keepalive: Keepalive | None = None

    def __post_init__(self) -> None:
        parts = self.parts()
        if not parts:
            raise ValueError("DeviceCommand needs at least one part")
        for field_name in ("seq", "version", "dst"):
            values = {getattr(p.header, field_name) for p in parts}
            if len(values) != 1:
                raise ValueError(f"DeviceCommand parts disagree on {field_name}: {sorted(values)}")

    def parts(self) -> tuple[CommandPart, ...]:
        """The parts present, in the order the device applies them (groups before routes)."""
        candidates: tuple[CommandPart | None, ...] = (
            self.prepare,
            self.group_set,
            self.route_set,
            self.keepalive,
        )
        return tuple(p for p in candidates if p is not None)

    @property
    def seq(self) -> int:
        """Sequence number shared by all parts; the device's ACK acknowledges it."""
        return self.parts()[0].header.seq

    @property
    def version(self) -> Version:
        """Table version shared by all parts."""
        return self.parts()[0].header.version

    @property
    def device(self) -> DeviceId:
        """The device this command goes to."""
        return self.parts()[0].header.dst
