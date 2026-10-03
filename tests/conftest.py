"""Shared test fixtures."""

import functools
import json
from pathlib import Path
from typing import Any

import pytest

from sdnctl.model import TopologySpec
from sdnctl.topology import build_topology

REPO_ROOT = Path(__file__).resolve().parents[1]

# The 6 generated topologies of SPEC 9.8: (name, two_plus_two, spares)
VARIANTS = [
    ("topology1", True, 0),
    ("topology1", False, 0),
    ("topology2", True, 0),
    ("topology2", True, 1),
    ("topology2", False, 0),
    ("topology2", False, 1),
]


@functools.cache
def built(name: str, two_plus_two: bool, spares: int = 0) -> TopologySpec:
    """A built topology, cached for the whole test session."""
    return build_topology(name, two_plus_two, spares)


@pytest.fixture(scope="session")
def golden() -> dict[str, Any]:
    """The golden numbers of reference/golden.json (read-only)."""
    data: dict[str, Any] = json.loads(
        (REPO_ROOT / "reference" / "golden.json").read_text(encoding="utf-8")
    )
    return data
