"""SdnController (SPEC 7): the facade that wires the four modules to the adapters.

It builds the Topology Manager, the Routing Engine, the OCS Matrix Controller (or the null one when
features.ocs is off) and the Scheduler, computes the startup tables (version 1) and keeps the
controller's copy of them. Only the Scheduler gets the adapters, so only it can send (rule 5).
"""

from sdnctl.interfaces import (
    ControllerContext,
    IncidentOutcome,
    IOcsMatrixController,
)
from sdnctl.ocs import NullOcsController, OcsMatrixController
from sdnctl.routing_engine import RoutingEngine
from sdnctl.scheduler import Scheduler
from sdnctl.tables import DeviceTables, Tables
from sdnctl.topology_manager import TopologyManager


class SdnController:
    """The controller a host runs (HostedController): one instance per run."""

    def __init__(self, ctx: ControllerContext) -> None:
        self.tm = TopologyManager(ctx.config.timing().correlation_window, ctx.trace)
        self.tm.load(ctx.spec)
        self.routing = RoutingEngine()
        self.ocs: IOcsMatrixController = (
            OcsMatrixController(ctx.spec) if ctx.config.features.ocs else NullOcsController()
        )
        self.scheduler = Scheduler(
            ctx, self.tm, self.routing, self.ocs, self.routing.compute(self.tm.view())
        )
        self.scheduler.start()

    @property
    def installed(self) -> Tables:
        """The controller's copy of every device's tables."""
        return self.scheduler.installed

    def initial_tables(self) -> Tables:
        """The startup tables (version 1), as copies the host can install."""
        return {
            d: DeviceTables(d, t.version, dict(t.routes), dict(t.groups))
            for d, t in self.installed.items()
        }

    def outcomes(self) -> tuple[IncidentOutcome, ...]:
        """Each incident's end state, or its state so far."""
        return self.scheduler.outcomes()
