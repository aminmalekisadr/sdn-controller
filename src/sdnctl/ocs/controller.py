"""OCS Matrix Controller (SPEC 7.1): plans OCS changes and tracks the matrix; it never sends."""

from collections.abc import Sequence

from sdnctl.interfaces import NoRepair, RepairPlan
from sdnctl.messages import OcsSet
from sdnctl.model import Incident, TopologySpec, TopologyView
from sdnctl.ocs.matrix import OcsMatrix
from sdnctl.ocs.planner import plan_repair
from sdnctl.types import CrossConnect


class OcsMatrixController:
    """IOcsMatrixController with the OCS on: matrix, spare picker, repair planner, read-back."""

    def __init__(self, spec: TopologySpec) -> None:
        if not spec.has_ocs:
            raise ValueError(f"{spec.name} has no OCS")
        self.matrix = OcsMatrix(spec)
        self._modules = {m.id: m for m in spec.modules}
        self._domains = {d.id: d for d in spec.domains}

    def enabled(self) -> bool:
        """True: this controller drives a real OCS."""
        return True

    def plan_repair(self, incident: Incident, view: TopologyView) -> RepairPlan | NoRepair:
        """Pick a spare and the OCS changes for an incident, or say why there is none."""
        return plan_repair(incident, view, self.matrix, self._modules, self._domains)

    def on_applied(self, cmd: OcsSet, read_back: Sequence[CrossConnect]) -> bool:
        """Take the read-back as the matrix; True if it equals the matrix cmd should give."""
        expected = self.matrix.after(cmd.disconnect, cmd.connect)
        self.matrix.replace(read_back)
        return set(read_back) == expected


class NullOcsController:
    """IOcsMatrixController with the OCS off: there is never a repair."""

    def enabled(self) -> bool:
        """False: no OCS to drive."""
        return False

    def plan_repair(self, incident: Incident, view: TopologyView) -> RepairPlan | NoRepair:
        """Always NoRepair('no_ocs')."""
        return NoRepair("no_ocs")

    def on_applied(self, cmd: OcsSet, read_back: Sequence[CrossConnect]) -> bool:
        """Never called: nothing is sent to an OCS that is off."""
        raise RuntimeError("the OCS is off; no OCS_SET was sent")
