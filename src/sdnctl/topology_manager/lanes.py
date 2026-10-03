"""The controller's lane state machine (SPEC 5.2).

Spare lanes start IDLE; working lanes move DOWN -> CONNECTING -> VERIFYING -> ACTIVE.
- DOWN or IDLE -> CONNECTING: OCS_DONE confirms a new cross-connect on the lane.
- CONNECTING or DOWN -> VERIFYING: LANE_UP (a repair without the OCS is invisible until then).
- VERIFYING -> ACTIVE: the device installed a group with the lane (it was usable).
- ACTIVE -> VERIFYING: the controller took the lane out of its group.
- any live state -> DOWN: LANE_DOWN (loss of light, module fault, HELLO timeout, errors).
- DOWN -> FAILED: a spare replaced the lane's failed module. FAILED is final.
"""

from sdnctl.types import LaneId, LaneState

S = LaneState
ALLOWED: dict[LaneState, frozenset[LaneState]] = {
    S.IDLE: frozenset({S.CONNECTING, S.DOWN}),
    S.DOWN: frozenset({S.CONNECTING, S.VERIFYING, S.FAILED}),
    S.CONNECTING: frozenset({S.VERIFYING, S.DOWN}),
    S.VERIFYING: frozenset({S.ACTIVE, S.DOWN}),
    S.ACTIVE: frozenset({S.DOWN, S.VERIFYING}),
    S.FAILED: frozenset(),
}


class LaneStateError(ValueError):
    """A lane was asked to make a transition the state machine does not allow."""


def check_transition(lane: LaneId, old: LaneState, new: LaneState) -> None:
    """Raise LaneStateError unless old -> new is allowed (staying put always is)."""
    if old is not new and new not in ALLOWED[old]:
        raise LaneStateError(f"lane {lane} cannot go from {old.value} to {new.value}")
