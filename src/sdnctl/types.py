"""Ids, enums, time and version types, unit constants and the sort keys of SPEC Section 3.1."""

import re
from dataclasses import dataclass
from enum import Enum

DeviceId = str  # "npu-0", "l1-1536", "l2-2304"
PortId = str  # "l1-1536.p12"
LaneId = str  # "l1-1536.p12.0"
ModuleId = str  # "l1-1536.m0"
DomainId = str  # "l1-1536.d0"
OcsPortId = str  # "N1", "S257"
SimTime = int  # microseconds
Version = int
IncidentId = int

LANE_GBPS = 200
LANES_PER_PORT = 2
PORT_GBPS = LANE_GBPS * LANES_PER_PORT


class Tier(Enum):
    """Device tier; also the first part of a device id."""

    NPU = "npu"
    L1 = "l1"
    L2 = "l2"


class Role(Enum):
    """Device role: the name a device had in the topology scripts."""

    NPU = "npu"
    UNION = "union"
    EXT = "ext"
    HRS = "hrs"


class LinkKind(Enum):
    """Physical kind of a link; only optical links carry modules."""

    D2D = "d2d"
    COPPER = "copper"
    OPTICAL = "optical"


class ModuleMapping(Enum):
    """How the lanes of a domain's ports are spread over its two working modules."""

    TWO_PLUS_TWO = "2+2"
    TWO_TO_ONE = "2:1"


class LaneState(Enum):
    """Lane state machine of SPEC Section 5.2."""

    IDLE = "idle"
    DOWN = "down"
    CONNECTING = "connecting"
    VERIFYING = "verifying"
    ACTIVE = "active"
    FAILED = "failed"


class DownCause(Enum):
    """Why a device reported lanes down."""

    LOSS_OF_LIGHT = "los"
    MODULE_FAULT = "module_fault"
    HELLO_TIMEOUT = "hello_timeout"
    ERRORS = "errors"


class IncidentState(Enum):
    """Phase or end state of an incident."""

    OPEN = "open"
    REROUTING = "rerouting"
    REPAIRING = "repairing"
    ACTIVATING = "activating"
    CLOSED = "closed"
    DEGRADED = "degraded"
    DEGRADED_WAITING_REPAIR = "degraded_waiting_repair"
    DEGRADED_NO_SPARE = "degraded_no_spare"
    FAILED_NEEDS_OPERATOR = "failed_needs_operator"


class MsgType(Enum):
    """The 10 protocol message types of SPEC Section 9.3."""

    HELLO = "HELLO"
    LANE_DOWN = "LANE_DOWN"
    LANE_UP = "LANE_UP"
    PREPARE = "PREPARE"
    OCS_SET = "OCS_SET"
    OCS_DONE = "OCS_DONE"
    GROUP_SET = "GROUP_SET"
    ROUTE_SET = "ROUTE_SET"
    ACK = "ACK"
    KEEPALIVE = "KEEPALIVE"


@dataclass(frozen=True)
class CrossConnect:
    """One OCS cross-connect: a North port joined to a South port."""

    north: OcsPortId
    south: OcsPortId


_TIER_RANK = {Tier.NPU.value: 0, Tier.L1.value: 1, Tier.L2.value: 2}
_DEVICE_RE = re.compile(r"(npu|l1|l2)-(\d+)")
_PORT_RE = re.compile(r"((?:npu|l1|l2)-\d+)\.p(\d+)")
_LANE_RE = re.compile(r"((?:npu|l1|l2)-\d+\.p\d+)\.(\d+)")


def device_key(d: DeviceId) -> tuple[int, int]:
    """Sort key of a device: (tier rank with npu 0, l1 1, l2 2; device number)."""
    m = _DEVICE_RE.fullmatch(d)
    if m is None:
        raise ValueError(f"bad device id {d!r}")
    return _TIER_RANK[m.group(1)], int(m.group(2))


def port_key(p: PortId) -> tuple[tuple[int, int], int]:
    """Sort key of a port: (device_key, port number)."""
    m = _PORT_RE.fullmatch(p)
    if m is None:
        raise ValueError(f"bad port id {p!r}")
    return device_key(m.group(1)), int(m.group(2))


def lane_key(x: LaneId) -> tuple[tuple[int, int], int, int]:
    """Sort key of a lane: (device_key, port number, lane number)."""
    m = _LANE_RE.fullmatch(x)
    if m is None:
        raise ValueError(f"bad lane id {x!r}")
    device, port = port_key(m.group(1))
    return device, port, int(m.group(2))
