"""The Routing Engine (SPEC 7.1): pure computation, no side effects."""

from sdnctl.model import TopologyView
from sdnctl.routing_engine.check import check_tables
from sdnctl.routing_engine.diff import diff_tables
from sdnctl.routing_engine.diff import order_waves as _order_waves
from sdnctl.routing_engine.groups import group_tables
from sdnctl.routing_engine.routes import route_tables
from sdnctl.tables import (
    AdjacencyChange,
    DeviceTables,
    ForwardingReport,
    TableDiff,
    Tables,
    Wave,
)


class RoutingEngine:
    """IRoutingEngine: BFS routes with ECMP, groups from usable lanes, diff, waves, loop check."""

    def compute(self, view: TopologyView) -> Tables:
        """Target route and group tables for every device, from usable lanes, at view.version."""
        routes = route_tables(view)
        groups = group_tables(view)
        return {
            d.id: DeviceTables(d.id, view.version, routes.get(d.id, {}), groups[d.id])
            for d in view.spec.devices
        }

    def diff(self, installed: Tables, target: Tables) -> TableDiff:
        """Only the entries that differ between installed and target tables."""
        return diff_tables(installed, target)

    def order_waves(self, diff: TableDiff, change: AdjacencyChange) -> list[Wave]:
        """Safe install order: transit first when a neighbor is lost, endpoints first when back."""
        return _order_waves(diff, change)

    def check_state(self, tables: Tables, view: TopologyView) -> ForwardingReport:
        """Looping and blackholed NPU pairs; tables may mix old and new per device."""
        return check_tables(tables, view)
