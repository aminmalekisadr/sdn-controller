"""The simulator API (SPEC 7.6): call order, validation, determinism, and the event clock."""

from typing import Any

import pytest

from sdnctl.interfaces import IClock, ISimulator
from sdnctl.sim import Simulator
from sdnctl.sim.clock import EventClock
from sdnctl.topology import TopologyError


def test_simulator_satisfies_the_protocol() -> None:
    sim: ISimulator = Simulator()
    clock: IClock = EventClock()
    assert sim is not None and clock.now() == 0


def test_clock_runs_in_time_then_schedule_order() -> None:
    clock = EventClock()
    seen: list[tuple[int, str]] = []
    clock.schedule(5, lambda: seen.append((clock.now(), "b")))
    clock.schedule(1, lambda: seen.append((clock.now(), "a")))
    clock.schedule(5, lambda: seen.append((clock.now(), "c")))

    def nested() -> None:
        seen.append((clock.now(), "d"))
        clock.schedule(0, lambda: seen.append((clock.now(), "e")))

    clock.schedule(5, nested)
    clock.run_until(4)
    assert seen == [(1, "a")] and clock.now() == 4
    clock.run_until(5)
    assert seen == [(1, "a"), (5, "b"), (5, "c"), (5, "d"), (5, "e")]
    with pytest.raises(ValueError):
        clock.schedule(-1, lambda: None)


def test_calls_need_topology_and_configuration() -> None:
    sim = Simulator()
    with pytest.raises(RuntimeError):
        sim.run(1)
    sim.configure(ocs=False)  # either order works
    sim.load_topology("topology2", True)
    sim.run(1)
    assert sim.now_us() == 1000


def test_ocs_on_a_topology_without_ocs_is_rejected() -> None:
    sim = Simulator()
    sim.load_topology("topology1", True)
    with pytest.raises(TopologyError) as err:
        sim.configure(ocs=True)
    assert err.value.rule == 7


def test_unknown_timing_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="fast"):
        Simulator().configure(ocs=False, timing="fast")


@pytest.mark.parametrize(
    ("event", "args", "message"),
    [
        ("meteor", {}, "unknown event"),
        ("module_fail", {}, "takes args: modules"),
        ("module_fail", {"modules": "npu-0.m0"}, "non-empty list"),
        ("module_fail", {"modules": ["npu-0.m9"]}, "unknown modules"),
        ("repair", {"modules": []}, "non-empty list"),
        ("drop_ack", {"device": "npu-0", "command": "ACK", "count": 1}, "command must be"),
        ("drop_ack", {"device": "npu-999", "command": "GROUP_SET", "count": 1}, "no device"),
        ("drop_ack", {"device": "npu-0", "command": "GROUP_SET", "count": 0}, "positive"),
        ("ocs_never_answers", {"extra": 1}, "takes args: none"),
    ],
)
def test_bad_injections_are_rejected(event: str, args: dict[str, Any], message: str) -> None:
    sim = Simulator()
    sim.load_topology("topology2", True, 1)
    sim.configure(ocs=True)
    with pytest.raises(ValueError, match=message):
        sim.inject(0, event, **args)


def test_ocs_fault_needs_an_ocs_and_time_moves_forward() -> None:
    sim = Simulator()
    sim.load_topology("topology1", True)
    sim.configure(ocs=False)
    with pytest.raises(ValueError, match="no OCS"):
        sim.inject(0, "ocs_never_answers")
    sim.run(10)
    with pytest.raises(ValueError, match="already"):
        sim.inject(5, "module_fail", modules=["l1-1536.m0"])


def test_runs_are_deterministic() -> None:
    def one_run() -> Any:
        sim = Simulator()
        sim.load_topology("topology2", False, 1)
        sim.configure(ocs=False)
        sim.inject(0, "module_fail", modules=["npu-0.m0", "npu-0.m1"])
        sim.inject(100, "repair", modules=["npu-0.m0", "npu-0.m1"])
        sim.run(300)
        return sim.report()

    first, second = one_run(), one_run()
    assert first.trace == second.trace
    assert first.capacity == second.capacity
    assert first.messages == second.messages
    assert len(first.trace) > 20
