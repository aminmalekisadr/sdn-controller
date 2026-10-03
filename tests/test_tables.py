"""Table types: diff counting and JSON round trips."""

import json

from sdnctl.jsonio import from_jsonable, to_jsonable
from sdnctl.tables import (
    DeviceTables,
    GroupChange,
    GroupEntry,
    RouteChange,
    RouteEntry,
    TableDiff,
)

L2 = ("l2-2304", "l2-2305", "l2-2306", "l2-2307")


def sample_diff() -> TableDiff:
    """A G2-like diff: one union loses two L2 next hops, one L2 reroutes two NPUs."""
    return TableDiff(
        routes={
            "l1-1536": [
                RouteChange("npu-64", RouteEntry("npu-64", L2, 4), RouteEntry("npu-64", L2[2:], 4))
            ],
            "l2-2304": [
                RouteChange("npu-0", RouteEntry("npu-0", ("l1-1536",), 2), None),
                RouteChange("npu-1", RouteEntry("npu-1", ("l1-1536",), 2), None),
            ],
            "l2-2305": [],
        },
        groups={
            "l1-1536": [
                GroupChange(
                    "l2-2304",
                    GroupEntry("l2-2304", ("l1-1536.p12.0", "l1-1536.p12.1")),
                    None,
                )
            ],
            "l2-2306": [],
        },
    )


def test_diff_counts() -> None:
    diff = sample_diff()
    assert diff.route_entries() == 3
    assert diff.group_entries() == 1
    assert diff.devices() == {"l1-1536", "l2-2304"}


def test_diff_json_round_trip() -> None:
    diff = sample_diff()
    assert from_jsonable(TableDiff, json.loads(json.dumps(to_jsonable(diff)))) == diff


def test_device_tables_json_round_trip() -> None:
    tables = DeviceTables(
        device="l1-1536",
        version=1,
        routes={"npu-64": RouteEntry("npu-64", L2, 4)},
        groups={"l2-2304": GroupEntry("l2-2304", ("l1-1536.p12.0", "l1-1536.p12.1"))},
    )
    assert from_jsonable(DeviceTables, json.loads(json.dumps(to_jsonable(tables)))) == tables
