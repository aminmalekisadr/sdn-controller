"""The 10 protocol messages and DeviceCommand: construction rules and JSON round trips."""

import json

import pytest

from sdnctl.jsonio import decode_message, encode_message, from_jsonable, to_jsonable
from sdnctl.messages import (
    CONTROLLER,
    MESSAGE_CLASSES,
    OCS,
    Ack,
    DeviceCommand,
    GroupSet,
    Header,
    Hello,
    Keepalive,
    LaneDown,
    LaneUp,
    Message,
    OcsDone,
    OcsPairResult,
    OcsSet,
    Prepare,
    RouteSet,
    RouteSetEntry,
)
from sdnctl.types import CrossConnect, DownCause, MsgType


def hdr(t: MsgType, src: str = CONTROLLER, dst: str = "npu-0", seq: int = 7) -> Header:
    """A header with fixed values except type, ends and seq."""
    return Header(type=t, seq=seq, version=2, incident=1, src=src, dst=dst, time=95_000)


ALL_MESSAGES: list[Message] = [
    Hello(hdr(MsgType.HELLO, "npu-0", "npu-64"), device="npu-0", lane="npu-0.p15.0"),
    LaneDown(
        hdr(MsgType.LANE_DOWN, "npu-0", CONTROLLER),
        device="npu-0",
        lanes=("npu-0.p13.0", "npu-0.p14.0"),
        cause=DownCause.MODULE_FAULT,
        modules=("npu-0.m0",),
    ),
    LaneUp(
        hdr(MsgType.LANE_UP, "npu-64", CONTROLLER),
        device="npu-64",
        lanes=("npu-64.p13.0",),
        peer_lanes=("npu-0.p15.0",),
    ),
    Prepare(
        hdr(MsgType.PREPARE),
        open_lanes=("npu-0.p15.0", "npu-0.p15.1"),
        expected_peer={"npu-0.p15.0": "npu-64.p13.0", "npu-0.p15.1": "npu-64.p14.0"},
    ),
    OcsSet(
        hdr(MsgType.OCS_SET, dst=OCS),
        disconnect=(CrossConnect("N1", "S1"), CrossConnect("N3", "S3")),
        connect=(CrossConnect("N257", "S1"), CrossConnect("N258", "S3")),
    ),
    OcsDone(
        hdr(MsgType.OCS_DONE, OCS, CONTROLLER),
        disconnected=(OcsPairResult(CrossConnect("N1", "S1"), ok=True),),
        connected=(OcsPairResult(CrossConnect("N257", "S1"), ok=False, error="mirror stuck"),),
        matrix_version=3,
    ),
    GroupSet(
        hdr(MsgType.GROUP_SET),
        entries={"npu-64": ("npu-0.p13.1", "npu-0.p14.1"), "npu-1": ()},
    ),
    RouteSet(
        hdr(MsgType.ROUTE_SET),
        entries={
            "npu-8": RouteSetEntry(neighbors=("l1-408", "l1-409"), hops=4),
            "npu-9": RouteSetEntry(neighbors=(), hops=0),
        },
    ),
    Ack(hdr(MsgType.ACK, "npu-0", CONTROLLER), acked_seq=7, ok=True),
    Keepalive(hdr(MsgType.KEEPALIVE), controller_id="ctl-0", epoch=1),
]


def test_every_message_type_is_covered() -> None:
    assert {m.header.type for m in ALL_MESSAGES} == set(MsgType)
    assert all(MESSAGE_CLASSES[t].TYPE is t for t in MsgType)


@pytest.mark.parametrize("msg", ALL_MESSAGES, ids=lambda m: m.header.type.value)
def test_message_json_round_trip(msg: Message) -> None:
    text = json.dumps(encode_message(msg))
    assert decode_message(json.loads(text)) == msg
    assert from_jsonable(type(msg), json.loads(text)) == msg


def test_header_type_must_match_class() -> None:
    with pytest.raises(ValueError, match="HELLO"):
        Hello(hdr(MsgType.ACK), device="npu-0", lane="npu-0.p0.0")


def test_lane_up_needs_one_peer_per_lane() -> None:
    with pytest.raises(ValueError):
        LaneUp(hdr(MsgType.LANE_UP), device="npu-0", lanes=("npu-0.p0.0",), peer_lanes=())


def test_batched_command_round_trip_and_order() -> None:
    groups = GroupSet(hdr(MsgType.GROUP_SET), entries={"npu-64": ("npu-0.p13.0",)})
    routes = RouteSet(hdr(MsgType.ROUTE_SET), entries={"npu-64": RouteSetEntry(("npu-64",), 1)})
    cmd = DeviceCommand(group_set=groups, route_set=routes)
    assert cmd.parts() == (groups, routes)
    assert (cmd.seq, cmd.version, cmd.device) == (7, 2, "npu-0")
    assert from_jsonable(DeviceCommand, json.loads(json.dumps(to_jsonable(cmd)))) == cmd


def test_command_rejects_empty_and_mixed_batches() -> None:
    with pytest.raises(ValueError, match="at least one"):
        DeviceCommand()
    groups = GroupSet(hdr(MsgType.GROUP_SET, seq=1), entries={})
    with pytest.raises(ValueError, match="seq"):
        DeviceCommand(group_set=groups, route_set=RouteSet(hdr(MsgType.ROUTE_SET, seq=2), {}))
    with pytest.raises(ValueError, match="dst"):
        DeviceCommand(
            group_set=groups,
            route_set=RouteSet(hdr(MsgType.ROUTE_SET, dst="npu-1", seq=1), {}),
        )
