"""The builders reproduce the topology scripts' wiring (SPEC 9.6).

Each script runs with the stubs in reference/stubs first on sys.path. Its edges are collapsed as
in SPEC 3.1; then the multiset of (device, device, kind) and every device's port order must equal
the builder's.
"""

import importlib
import runpy
import sys
from collections import Counter
from typing import Any

import pytest

from sdnctl.model import TopologySpec
from sdnctl.types import LinkKind, device_key
from tests.conftest import REPO_ROOT, built

Edge = tuple[str, str, LinkKind]


def run_script(fname: str, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], Any]:
    """Run one script with the stubs; return its globals and the stub graph it built."""
    monkeypatch.syspath_prepend(str(REPO_ROOT / "reference" / "stubs"))
    monkeypatch.setattr(sys, "argv", [fname, "case"])  # topology 1 needs a dir_name argument
    stub = importlib.import_module("net_sim_builder")
    script_globals = runpy.run_path(
        str(REPO_ROOT / "reference" / "topologies" / fname), run_name="__main__"
    )
    return script_globals, stub.LAST


def collapse(g: dict[str, Any], graph: Any) -> list[Edge]:
    """Script edges as device edges, with kinds by SPEC 3.1; compute-to-IO-die edges vanish."""
    total, grid, sw_base = g["TOTAL_COMPUTES"], g["GRID_START"], g["SW_BASE"]
    hrs, num_hrs = g["HRS_START"], g["NUM_HRS"]

    def device(x: int) -> str:
        if x < total:
            return f"npu-{x}"
        if grid <= x < sw_base:
            return f"npu-{(x - grid) % total}"
        if sw_base <= x < hrs:
            return f"l1-{x}"
        if hrs <= x < hrs + num_hrs:
            return f"l2-{x}"
        raise ValueError(f"node {x} is not an NPU, L1 or L2")

    def kind(a: str, b: str) -> LinkKind:
        ta, tb = a.split("-")[0], b.split("-")[0]
        if ta == tb == "npu":
            same_board = int(a[4:]) // 8 == int(b[4:]) // 8
            return LinkKind.D2D if same_board else LinkKind.OPTICAL
        return LinkKind.OPTICAL if {ta, tb} == {"l1", "l2"} else LinkKind.COPPER

    out = []
    for a, b, *_ in graph.edges:
        da, db = device(a), device(b)
        if da != db:
            out.append((da, db, kind(da, db)))
    return out


def normalized(edges: list[Edge]) -> Counter[Edge]:
    """Multiset of edges with the two ends in device_key order."""
    return Counter(
        (a, b, k) if device_key(a) <= device_key(b) else (b, a, k) for a, b, k in edges
    )


def port_order_from_edges(edges: list[Edge]) -> dict[str, list[str]]:
    """Per device, the far-end device of each port, in edge-creation order."""
    out: dict[str, list[str]] = {}
    for a, b, _ in edges:
        out.setdefault(a, []).append(b)
        out.setdefault(b, []).append(a)
    return out


def builder_edges(spec: TopologySpec) -> list[Edge]:
    """The builder's links as device edges."""
    device_of = {p.id: p.device for p in spec.ports}
    return [(device_of[link.a], device_of[link.b], link.kind) for link in spec.links]


def builder_port_order(spec: TopologySpec) -> dict[str, list[str]]:
    """Per device, the far-end device of each working port, in port order."""
    device_of = {p.id: p.device for p in spec.ports}
    far: dict[str, str] = {}
    for link in spec.links:
        far[link.a] = device_of[link.b]
        far[link.b] = device_of[link.a]
    out: dict[str, list[str]] = {}
    for p in sorted(spec.ports, key=lambda p: (device_key(p.device), p.index)):
        if not p.spare:
            out.setdefault(p.device, []).append(far[p.id])
    return out


@pytest.mark.parametrize(
    ("fname", "name"),
    [("topo1_910d.py", "topology1"), ("topo2_ocs_sure.py", "topology2")],
)
def test_builder_matches_script(fname: str, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    g, graph = run_script(fname, monkeypatch)
    script = collapse(g, graph)
    spec = built(name, True, 1 if name == "topology2" else 0)
    assert normalized(builder_edges(spec)) == normalized(script)
    assert builder_port_order(spec) == port_order_from_edges(script)
