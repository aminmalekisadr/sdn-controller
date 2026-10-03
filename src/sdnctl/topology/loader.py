"""Build a topology by name, and write specs to JSON files and read them back."""

from pathlib import Path

from sdnctl.jsonio import dump_json, load_json
from sdnctl.model import TopologySpec
from sdnctl.topology.builder import build_topology1, build_topology2
from sdnctl.types import ModuleMapping

TOPOLOGY_NAMES = ("topology1", "topology2")


def build_topology(name: str, two_plus_two: bool, spares: int = 0) -> TopologySpec:
    """Build topology1 or topology2; topology1 has no OCS, so it takes no spares."""
    if name == "topology1":
        if spares:
            raise ValueError("topology1 has no OCS, so it has no spare modules")
        return build_topology1(two_plus_two)
    if name == "topology2":
        return build_topology2(two_plus_two, spares)
    raise ValueError(f"unknown topology {name!r}; known: {', '.join(TOPOLOGY_NAMES)}")


def spec_filename(spec: TopologySpec) -> str:
    """File name of a spec, e.g. topology2_2p2_spare1.json."""
    mapping = "2p2" if spec.mapping is ModuleMapping.TWO_PLUS_TWO else "2to1"
    return f"{spec.name}_{mapping}_spare{spec.spare_modules_per_domain}.json"


def write_spec(spec: TopologySpec, out_dir: str | Path) -> Path:
    """Write a spec to out_dir/spec_filename(spec) and return the path."""
    path = Path(out_dir) / spec_filename(spec)
    dump_json(spec, path)
    return path


def read_spec(path: str | Path) -> TopologySpec:
    """Read a spec written by write_spec."""
    return load_json(TopologySpec, path)
