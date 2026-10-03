# Design decisions

One short entry per decision, with the reason. Open items wait for confirmation.

## Decisions

**D1. `CrossConnect` lives in `types.py` and is re-exported from `model.py`.**
`model.Incident.reports` needs `messages.LaneDown`, and `messages.OcsSet` needs `CrossConnect`. With `CrossConnect` in `model.py` the two modules would import each other. `from sdnctl.model import CrossConnect` still works.

**D2. `TopologyView` is a `typing.Protocol` in `model.py`, and it has one extra helper, `usable_lanes(device, neighbor)`.**
Modules depend only on the Protocol; the concrete class is `TopologySnapshot` (D12). `usable_lanes` was added because `compute()` builds target groups from usable lanes (SPEC 5.1), and the listed helpers only give ACTIVE lanes. The view also exposes `spec`, `version`, `lane_state()`, `xconnects()` and `failed_modules()`, which the 9.1 text names as its contents.

**D3. `Header.src` and `Header.dst` are plain strings: a device id, `"controller"` or `"ocs"`. `Header.incident` is optional.**
These are the only three kinds of endpoint. Messages outside an incident, such as KEEPALIVE or the startup install, have no incident id.

**D4. `GroupSet.entries` is `dict[neighbor, tuple[lane, ...]]` and `RouteSet.entries` is `dict[dest, RouteSetEntry(neighbors, hops)]`. An empty tuple removes the entry.**
This follows the 9.3 table. `tables.GroupEntry` is not reused, because a `GroupEntry` is never empty. `RouteEntry` is not reused, because it repeats `dest`.

**D5. `OcsDone` has `disconnected` and `connected`, each a tuple of `OcsPairResult(xconnect, ok, error)`, plus `matrix_version` and an `ok` property.**
This mirrors `OcsSet`, so the read-back check can match results to requests pair by pair.

**D6. Every message checks in `__post_init__` that its header type matches its class.**
A `DeviceCommand` needs at least one part, and all its parts must share seq, version and destination. The device's ACK acknowledges that shared seq, and the batch counts as one message. `parts()` returns the parts in apply order: prepare, groups, routes, keepalive.

**D7. `SimReport` = `trace`, `capacity`, `messages` and `incidents`.**
- `trace`: the JSON-lines records.
- `capacity`: a tuple of `CapacitySample(time, key, value)`. Keys use golden.json's naming: domain `l1-1536.d0`, pair `l1-1536/l2-2304`, device `l1-1536`.
- `messages`: counts by type, with a `messages_total()` method.
- `incidents`: one `IncidentOutcome(incident, state, ended_at, reason)` per incident.

The fields are what Section 7.6 lists. Their finer split, such as `ACK_prepare`, is a Phase 2 decision.

**D8. `jsonio` is one generic codec driven by dataclass type hints, not a hand-written encoder per type.**
It supports enums, `X | None`, tuple (variadic and fixed-length), list, frozenset, dict and `Mapping`. Frozensets are written as lists sorted by their canonical JSON, so output is deterministic. Unknown fields, missing fields, wrong JSON types and bad enum values raise `ValueError` with a path such as `$.devices[1].tier`. Messages are decoded by dispatching on `header.type`. One codec keeps all API types round-trippable with no per-type code to maintain.

**D9. Messages and configs with dict fields are frozen but not hashable.**
`Prepare.expected_peer`, `GroupSet.entries`, `RouteSet.entries`, `ControllerConfig.timing_us` and `RepairPlan.prepares` are dicts, as the spec shows them. Nothing needs to hash these values.

**D10. Environment: Python 3.14.3 locally, with `requires-python >= 3.11`. The venv is made with `uv venv .venv` and filled with `uv pip install -e ".[dev]"`.**
mypy runs in `strict` mode over `src/` and `tests/`. ruff and mypy skip `reference/` and `build/`. `scripts/check.sh` prefers `.venv` when it exists.

**D11. The default config values sit in one dict, `config.DEFAULTS`, which is a copy of SPEC 9.5. `load_config()` deep-merges a file or dict over it and rejects unknown keys and unknown profiles.**
This is the single place for the timing numbers. Every module must read `ControllerConfig`, never these values directly. `tests/test_config.py` checks the defaults against an independent copy of 9.5, and checks that the profiles reproduce G4a's typical (98 ms) and worst (4,975 ms) timelines from golden.json.

**D12. The concrete view is `TopologySnapshot` in `sdnctl/topology/view.py`, a file not in the SPEC 8 layout. It runs over a `SpecIndex` that is built once per spec.**
A view is a read-only snapshot, so the Topology Manager (Phase 3) will own the mutable state and hand out a new snapshot per version. The per-spec lookups take about 0.1 s on topology 1, so they are shared, and each new snapshot only copies lane states and recomputes peers.

**D13. Peers.**
A lane with an OCS port faces whichever lane its current cross-connect points to. Any other lane faces the lane with the same index on the far end of its link. A spare lane faces nothing until it is cross-connected.

**D14. Usable, active and steady state.**
- A lane is **usable** when it is ACTIVE, or when it is VERIFYING and LANE_UP has arrived for both it and the lane it faces, as SPEC 5.1 says.
- `active_lanes` checks only the lane's own state, since a lane is ACTIVE exactly when it is in its device's group.
- `neighbors` uses usable lanes.
- **Steady state:** spare lanes are IDLE, working lanes that face a lane are ACTIVE, and any other lane is DOWN (none exist in the built topologies).

**D15. Capacity uses the formulas of `capacities()` in ref_model.**
- A lane is in service when it and the lane it faces are both ACTIVE.
- The startup count is the working lanes that faced a lane when the topology was built.
- Pair capacity counts lanes from a's side.
- Domain capacity includes the domain's spare module lanes in the numerator.

`tests/test_view.py` checks all of this against the G1 capacities and the G4a/G4b during and final capacities in golden.json.

**D16. `TopologyError` (a `ValueError`) carries the rule number (`.rule`, 1–9).**
`validate()` checks the rules in order and stops at the first broken one. Two rules check a little more than their wording:
- **Rule 2** also rejects a lane listed by two modules.
- **Rule 6** also checks that `Lane.module` agrees with `Module.lanes`, and that each domain's mapping equals the topology's.

**D17. Device attributes.**
- An NPU's `board` is the global board index `d // 8` (SPEC 3.2), not its board within the rack.
- L1 `role` comes from the switch's position in its plane (the first 16 are unions, the next 8 EXT).
- An L2's `plane` comes from its id.

These are the same rules ref_model uses.

**D18. The Phase 1 count test checks every structural count in golden.json, plus group entries.**
Group entries are computed from the steady-state view. These counts are checked on all 6 variants; the 2:1 builds must equal golden's 2+2 numbers, because 2:1 changes only module wiring. The routing numbers (`route_entries`, `npu_pair_hops`, `nexthop_set_sizes`, steady loops and blackholes) need the Routing Engine, so they are checked in Phase 4.

**D19. `loader.build_topology(name, two_plus_two, spares)` is the entry point for the simulator's `load_topology`.**
`topology1` rejects `spares > 0`. Spec files are named `<name>_<2p2|2to1>_spare<n>.json`. The tests write all 6 to `build/specs/` and read them back; a topology 1 file is about 8 MB.

**D20. Domain sizes (`DOMAIN_PORTS`: 2 ports on NPUs, 4 on L1 and L2) are defined once in `builder.py`, and the validator imports them.**

### Phase 2: simulator

**D21. The simulator hosts a controller through a small seam.**
- A factory receives a `ControllerContext`: spec, config, clock, device adapter, OCS adapter (None without an OCS) and trace.
- It returns a `HostedController`, which has `initial_tables()` and `outcomes()` and registers its own event sinks and timers.
- The startup install of `initial_tables()` at version 1 is not counted.
- Without a controller (Phase 2), startup groups come straight from the topology, there are no routes, and device events are counted and traced but not acted on.

**D22. `load_topology()` and `configure()` work in either order.**
The run (devices, OCS, controller, version-1 tables) is built once both are known, and calling either again starts a fresh run. `configure()` validates the topology against the features, so `ocs=True` on topology 1 raises `TopologyError` (rule 7).

**D23. The adapters apply the latencies of SPEC 11.0 exactly.**
- A device command is applied, and its ACK reaches the controller, at send + `device_write`.
- LANE_DOWN takes `report` and LANE_UP takes `lane_up_report`.
- The OCS applies after `ocs_command`, and OCS_DONE reaches the controller when the mirrors settle, `mirror_move` later.
- A zero-latency delivery is scheduled with delay 0, so it runs after everything else due at that instant. All commands of a wave are therefore applied before the controller sees any of their ACKs.

**D24. Physical layer.**
A lane pair has light when both lanes transmit (module working, lane not an unopened spare) and a path exists (a link, or a settled cross-connect). Light returns `lane_bringup` after the last of these conditions is met, and goes out at once when one breaks. An OCS disconnect takes effect when the OCS applies the command; a connect takes effect when the mirrors settle.

**D25. Device side.**
- **Lane states:** IDLE, DOWN, CONNECTING (opened or repaired, waiting for light), VERIFYING, and ACTIVE (in a group).
- **LANE_UP:** batched per device, for the lanes verified at the same instant.
- **GROUP_SET safety:** the whole command is rejected (ACK `ok=false`) if it names a lane that is neither ACTIVE nor verified, or a lane that does not face the named neighbor (safety rule 1).
- **Versions:** an older version is ignored, with ACK `ok=false` and the error "stale version".

**D26. HELLO.**
- When light returns, each end learns the far lane's id.
- If it equals the expected peer (the startup peer, or PREPARE's `expected_peer`), the checks pass after `hello_check` and `error_check`.
- Otherwise the lane goes DOWN with `HELLO_TIMEOUT` after 3 × `hello_check`. The config has no HELLO interval, so one interval is taken to equal `hello_check` (open item).
- The error check is always clean in v1.

**D27. A KEEPALIVE-only command gets no ACK.**
KEEPALIVE is not counted, but an ACK would be.

**D28. Messages are counted when sent, including lost ones.**
A batch counts once, under the key `GROUP_SET+ROUTE_SET`. ACKs are counted as `ACK`; the golden split into `ACK_prepare` and `ACK_group` can be derived from the trace.

**D29. Capacity measured on the devices.**
- A lane is in service when it and the lane it faces are lit and both are in their devices' installed groups.
- A sample is recorded only when a key's value changes, and every key starts at 1.0, which the Phase 1 view test confirms for all built topologies.
- Pair keys put the two ends in `device_key` order: `l1-1536/l2-2304`.

**D30. Test faults.**
- `drop_ack` matches a command that contains the given type, so a batch with GROUP_SET counts. It is armed from its injection time.
- `ocs_never_answers` makes the OCS ignore every later OCS_SET: no apply, no OCS_DONE.

**D31. The forwarding hash is `zlib.crc32`.**
It hashes `"flow|device"` to pick the neighbor and `"flow|device|neighbor"` to pick the lane.

### Phase 3: Topology Manager

**D32. Two interface additions.**
- `TopologyView.spare_pool()`: the OCS planner needs to see which spares are still free, and the view is all it gets.
- `ITopologyManager.correlation_deadline()`: tells the scheduler when to call `poll_incidents()`, so rule 2 lives in one place.

**D33. The controller's lane state machine is a table in `topology_manager/lanes.py`. Illegal transitions raise `LaneStateError`.**
- **DOWN → CONNECTING** happens when OCS_DONE's read-back shows the new cross-connect. The Topology Manager never hears that OCS_SET was sent, because only the scheduler sends it.
- **DOWN → VERIFYING:** a repair without the OCS is invisible to the controller until LANE_UP, so those lanes skip CONNECTING.
- **ACTIVE → VERIFYING** when the controller leaves a live lane out of a group.
- **FAILED** is final.

**D34. Bad input from the network is ignored and traced; a broken internal assumption raises.**
- A LANE_UP is accepted per lane only if the HELLO peer it reports equals the controller's current peer for that lane, from the read-back matrix. Otherwise the lane is ignored and traced as `tm_ignored`.
- A LANE_UP for an IDLE or FAILED lane is ignored the same way.
- A report naming another device's lane raises.
- Reporting a GROUP_SET as installed with a lane that was not usable raises (safety rule 1).

**D35. Verified lanes.**
A lane is verified once LANE_UP arrives for it; every lane ACTIVE at load also counts. The view uses this set for "usable", and LANE_DOWN clears it.

**D36. The read-back matrix is the truth for cross-connects.**
`on_ocs_done` uses its `matrix` argument, not OCS_DONE's per-pair results. A removed cross-connect takes its live lanes DOWN.

**D37. Incident fields.**
- `failed_device` is the one device that reported MODULE_FAULT. It is None if no device did, or if several did.
- `failed_modules` are all modules named by MODULE_FAULT reports, sorted.
- `lanes` are all reported lanes.
- `lost` holds each (device, neighbor) left with no ACTIVE lane toward that neighbor, among the neighbors of the reported lanes.

**D38. Versions and trace.**
Every Topology Manager event bumps the topology version by 1, and `view()` is cached per version. Trace records are `tm_lane`, `tm_matrix`, `tm_incident`, `tm_repair` and `tm_ignored`, stamped with the event's time (OCS_DONE uses its header time).

**D39. `record_repair` marks only the DOWN lanes of the failed modules FAILED, and removes the used spare from the pool.**
Spares in use but not yet recorded stay in the pool; v1 has one incident at a time.

### Phase 4: Routing Engine

**D40. numpy is a runtime dependency, used only by the Routing Engine (SPEC 2 allows it).**
The BFS runs for every destination at once, as dense frontier × adjacency matrix products (n ≤ 1,312), and gives the same distances as a per-destination BFS. `compute()` on topology 1 takes about 1.6–1.7 s on this machine (limit: 5 s).

**D41. `compute()` builds routes only for routed devices, over routed neighbors, and groups for every device from usable lanes.**
- Each route entry lists every routed neighbor one hop closer, in `device_key` order.
- The output tables carry `version = view.version`.
- Lanes never enter the BFS.

**D42. `diff()` leaves unchanged devices out and sorts each device's changes by destination or neighbor.**
`TableDiff.devices()` includes devices whose only changes are group changes.

**D43. `order_waves()`.**
- **Neighbor lost:** [transit, endpoints].
- **Neighbor restored:** [endpoints, transit].
- **No adjacency change:** one wave with every changed device.
- **Lost and restored at once:** `ValueError` (v1 handles one direction at a time).

Endpoints are the first device of each lost or restored adjacency. Empty waves are dropped, and `Wave.index` starts at 1.

**D44. `check_state()` takes each device's live neighbors from that device's own groups in the tables it is given.**
- So a mixed state carries its groups with it.
- A missing route counts as having no live neighbor.
- Only routed devices take part; the view supplies the routing domain and the NPU set.

It checks all destinations on one graph of (destination, device) nodes. A node can loop if it survives repeatedly peeling off successor-less nodes, and it is blackholed if it reaches a drop node. One check takes about 0.4 s on topology 1.

**D45. Mixed states between waves (from SPEC 11.0 and `ref_model.py`).**
- **Going down:** every device has its post-failure groups, and updated devices use their new routes.
- **Going up:** an updated device gets its restored groups and routes together, as one batch.

The tests build these states in `tests/routing_helpers.py`. The scheduler will produce the same states in Phase 7.

### Phase 5: OCS Matrix Controller

**D46. Interface change: `IOcsMatrixController.on_applied()` returns `bool`.**
It returns True when the read-back equals the matrix the command should produce. Either way, the controller takes the read-back as its matrix. The scheduler needs this answer both at OCS_DONE and when the OCS_DONE timeout expires (SPEC 7.3, G7). After a success, calling it again also returns True.

**D47. The OCS controller keeps its own `OcsMatrix`.**
- It starts from `spec.xconnects` and is replaced by each read-back. The Topology Manager takes its cross-connects from the same read-backs (`on_ocs_done`).
- `OcsMatrixController` requires a topology with an OCS.
- `NullOcsController` is used otherwise, and whenever `features.ocs` is false. Its `on_applied` raises, because nothing is ever sent to an OCS that is off.

**D48. Repair plan details.**
- **Failed modules:** the incident's MODULE_FAULT modules on the failed device, in module-number order. The spare comes from the domain of the first one.
- **Dead lanes:** those modules' lanes that are cross-connected now, in module order and then lane order. They are paired with the spare's lanes in lane order; if the spare has more lanes than needed, only the paired ones are opened.
- **Check order:** `failed_end_unclear`, then `no_spare` (no free spare, or no cross-connected dead lane), then `not_enough_spare`.
- **PREPARE to the failed device:** `open_lanes` = the spare lanes used; `expected_peer` = spare lane → peer lane.
- **PREPARE to each peer device:** empty `open_lanes`; `expected_peer` = its lane → the spare lane. This follows SPEC 7.2: PREPARE "opens the spare lanes and gives the expected peer lane for each new pair".
- **PREPARE headers in the plan:** seq 0, time 0, `version = view.version`, and the incident id. The scheduler stamps seq and time when it sends.

**D49. Spare pick.**
The pick is the lowest module number among the domain's `spare_modules` that are in `view.spare_pool()` and not in `view.failed_modules()`. The spare is therefore always on the failed device, on the same OCS side.

### Phase 6: SdnController

**D50. `ControllerContext`, `HostedController` and `ControllerFactory` moved from `sim/` to `interfaces.py`.**
The controller must not depend on the simulator, because ns-3 and hardware hosts come later. `sdnctl.sim` re-exports the three names.

**D51. The `scheduler/` package starts in Phase 6 with its event loop (slide task 1: "route events to the other modules").**
Wiring the modules to the adapters means routing their events, and safety rule 5 says only the scheduler may send, KEEPALIVE included.
- The facade never calls an adapter. Only the scheduler is given them.
- A test checks that the Topology Manager, Routing Engine, OCS and topology packages never name an adapter or call `send` or `read_matrix`.
- Phase 7 adds the incident runner, the command sender with waves, and timeouts. Until then, incidents stay OPEN.

**D52. KEEPALIVE: the scheduler sends one to every device at startup.**
It uses controller id `sdnctl` and epoch 1. It is not counted and gets no ACK. There is no periodic KEEPALIVE, because the config has no interval for it (open item 10).

**D53. The controller's copy of the tables (`Scheduler.installed`).**
- It starts as the startup `compute()` output.
- Each LANE_DOWN prunes the reporting device's lanes from that device's groups in the copy, exactly as the device does (SPEC 5.1).
- The startup tables handed to the host are copies, so the host's devices and the controller never share a mutable table.

**D54. The OCS controller follows the feature flag.**
`OcsMatrixController` is used when `features.ocs` is on (the validator already rejects that on a topology without an OCS), and `NullOcsController` otherwise.

**D55. The table version counter, `Scheduler.table_version`, starts at the startup version (1).**
Phase 7 raises it for each table write.

### Phase 7: SDN Scheduler

**D56. Parked incidents free the active slot.**
- A parked incident is one in DEGRADED_WAITING_REPAIR or DEGRADED_NO_SPARE.
- "One open incident at a time" (SPEC 7.3) counts only incidents being planned, rerouted, repaired or activated. Otherwise one module waiting for a technician would block every later incident.
- A parked incident rejoins the queue when all its lanes are usable again (after a repair), then activates and closes, as in G1, G2 and G6. Neither parked state is final.

**D57. When activation starts.**
- **OCS repair:** every lane named in a PREPARE's `expected_peer` must be usable. These are the spare lanes and their new peer lanes.
- **Parked incident:** every lane its reports named must be usable.
- Activation also waits for any reroute waves to finish, and for the read-back check.

**D58. Activation.**
- **Neighbor back:** if an adjacency in `incident.lost` is a neighbor again, activation takes `compute`, then runs the restore waves.
- **Otherwise:** it waits no simulated time and sends one wave to the devices in the diff.
- `compute()` itself always runs. Simulated time is charged only where SPEC 11.0 says.

**D59. Closing an incident.**
- **Touched pairs:** (device, peer device) for each of the incident's lanes, taken when planning ends.
- **End state:** CLOSED if every touched pair's capacity in the controller's view is 1.0; otherwise DEGRADED, with the capacity in the reason.
- **OCS repairs:** `record_repair()` runs at close.
- **"Version +1":** the Topology Manager's version already goes up with `record_repair` and with every event, so there is no separate bump.

**D60. Table versions.**
Each wave push (one target computation) raises `table_version` by 1, and all its commands carry it. Resends reuse it; PREPARE carries the current value.

**D61. Sending and resending device commands.**
- **ACK timeout, table command:** resend the device's full target tables as one batch, with removals for entries only the controller's copy has.
- **ACK timeout, PREPARE:** resend the same PREPARE with a new header.
- **Busy device:** a command for a device that already has one outstanding waits in a per-device queue (safety rule 8).
- **Rejection:** a rejected command (ACK `ok=false`) ends the incident FAILED_NEEDS_OPERATOR.

**D62. The OCS read-back.**
- An OCS_DONE whose read-back does not match is treated like a timeout: resend once, then fail.
- If OCS_DONE was lost but the read-back at the timeout shows the change applied, the Topology Manager gets the read-back with a synthetic OCS_DONE (seq 0, `matrix_version` 0). The verify timer starts from then.

**D63. Scenarios.**
The scenario files are in `scenarios/` (G1–G8 and G6b, in the SPEC 11 format). `sdnctl/scenario.py` loads and runs them. A run stops 30 s after the last event, which is longer than every timeout and the worst-case restore.

**D64. `Simulator.controller`** exposes the hosted controller, so tests and reports can inspect it.

**D65. New scheduler trace records.**
`plan_done`, `compute_done`, `waves` (sizes, entry counts), `wave_sent`, `wave_acked`, `incident_open`, `incident_ready`, `incident_state` (state, reason), `ack_timeout`, `resend`, `queued`, `ack_ignored`, `ocs_timeout`, `ocs_check`.

### Phase 8: CLI and report

**D66. The `sdnctl-sim` flags.**
- `--scenario PATH...` (required): scenario files or folders. A folder means every `*.json` in it, sorted by name. A file named twice runs once.
- `--topology NAME` (optional): runs only the scenarios on that topology. It never changes a scenario's topology, because a scenario's module and device names belong to its own topology.
- `--out DIR` (required): each run writes to `DIR/<scenario name>/`.
- **Reason:** a scenario file (SPEC 11) already names its topology, so `--topology` can only choose among scenarios. See open item 11.

**D67. What `--config` changes.**
- The file is merged over the SPEC 9.5 defaults. Its timing values, timeouts and limits apply to every run.
- A `timing_profile` or feature flag that the file sets replaces each scenario's own. Flags the file leaves out come from the scenario.
- **Reason:** this lets you rerun the golden scenarios under the worst profile, or with the OCS off, without a flag the spec does not list. `start_scenario`/`run_scenario` now take the config. See open item 12.

**D68. The report files** (written by `sdnctl/report.py`, so other hosts and tests can write them without the CLI).
- **`trace.jsonl`:** every trace record, in order, as compact JSON, one per line.
- **`summary.json`:** a `RunSummary` (it reads back with `jsonio.load_json`). It holds:
  - the settings as run: topology, spares, features, profile, the active timing values, timeouts, limits, events and run length;
  - each incident's outcome;
  - the counted messages by type, and their total;
  - the lowest and final value of every capacity key that changed;
  - the number of trace records.
- **`capacity.csv`:** `t_us,key,value` for every capacity sample.
- **Determinism:** times are microseconds, like the trace's `t`. No file holds a wall-clock value, and line endings are always `\n`, so the same run writes the same bytes. Wall time appears only on the console.

**D69. Exit status and errors.**
- **Bad inputs:** every scenario file, the config and `--out` are checked before any run. A bad input exits with 1 and nothing runs.
- **A scenario that cannot run** (a `ValueError`, such as an unknown module or the OCS on topology 1): it is reported, the other scenarios still run, and the exit status is 1.
- **Bad command line:** exits with 2 (argparse).
- **Stale reports:** a run first deletes its three report files, so a failed run never leaves an older report behind.
- **Scenario names** must be plain folder names, unique when case is ignored (Windows).

**D70. `--out` is never in `reference/`.**
The CLI refuses an output folder that is, or is inside, a folder holding `ref_model.py`.

**D71. `dump_json` line endings.**
`dump_json` now writes `\n` line endings on every platform; on Windows it used to write `\r\n`.

**D72. Fix: capacity samples missed changes at the far end of a lane.**
- **The bug:** a lane is in service only if the lane it faces is too. When the far end installed its groups, the near device's and domain's capacity changed without a sample. In G4a the samples ended at `npu-0` = 0.933 and `npu-0.d0` = 0.5 instead of 1.0.
- **What was affected:** only `report().capacity`. The live values, which the Phase 7 tests checked, were right.
- **The fix:** `CapacityMonitor.touch` now also re-reads the device and domain of the faced lanes.
- **New test:** in every scenario, replaying the samples gives the measured values at the end, for every device, every domain and every sampled pair.

**D73. `--config` overrides print a warning (open items 11 and 12, answered: keep D66 and D67).**
- For each scenario setting that the config changes, the CLI prints a warning to stderr before that scenario runs. For example: `sdnctl-sim: warning: G4a: --config overrides the scenario's timing_profile (typical -> worst)`.
- A setting the config gives the same value as the scenario does is not an override, so it gets no warning.
- Timing values, timeouts and limits never warn, because scenarios do not set them.

## Answers received (2026-10-02)

v1 (git tag `v1`) keeps the SPEC model, which `golden.json` checks. The answers marked **v2** change that model, and none of them is built yet.

- **Lanes (v2):** no lanes and nothing at 200G. Ports are 400G and links are 800G.
  - **A module failure** leaves 800G of a 4-port domain. Under 2+2, all 4 ports carry half. Under 2:1, the healthy module's 2 ports carry 800G and the failed module's 2 ports are gone.
- **Item 4 (v2):** an NPU has 4 optical ports on 2 modules, like the switches. All 4 go to the same peer NPU as 2 links of 800G (2 ports each), 1.6T in total. v1 has 2 ports.
- **Item 6 (v2):** both SURE links of an NPU go to the same peer NPU, on another board. SURE has no racks yet.
  - Reading still to confirm: NPU i of a board connects to NPU i of its peer board.
  - v1 uses the script's transpose instead: `npu-1` ↔ `npu-72`.
- **The OCS (v2):** under 2+2, the spare replaces the failed module's resources completely.
  - Reading still to confirm: the spare takes over every connection of the failed module, and every port is back to full rate.
- **Item 9:** still open. 10 ms is the candidate HELLO interval.
  - If the first HELLO after light can take one interval, `hello_check` becomes 10 ms. G4a's typical restore then goes from 98 ms to 106 ms; G8 closes at 156 ms instead of 148 ms; G6 closes at 10,078 ms instead of 10,070 ms.
  - A wrong peer would be found 30 ms after light (3 missed HELLOs).
  - The worst profile's `hello_check` of 200 ms would then be 20 intervals, longer than the 3-miss timeout.
- **Item 10:** closed. There is no standby controller, so v1 keeps its one KEEPALIVE at startup (D52).
- **Items 11 and 12:** closed. They stay as built, with a warning added (D73).
- **Items 1, 2, 3, 7 and 8:** no answer yet.

## Open items (SPEC Section 3.6; to be confirmed)

1. An NPU is a compute die plus its IO dies: one node, which also forwards transit traffic.
2. EXT unions are L1 switches.
3. L1–L2 links carry optical modules in both topologies. Topology 1's script labels them active copper, but my glossary says L1–L2 always uses modules.
4. NPU optical domains have 2 ports and 2 modules of 2 lanes.
5. The OCS switches single lanes, with one OCS port per lane. The script's `SURE_MIRROR_PER_OCS = 128` counts links.
6. Both SURE links of an NPU go to the same peer, as the code does. The comment in step 11 says the second link should go to another board.
7. Routing is hop-count shortest path with ECMP, like the scripts. This has side effects:
   - In topology 1, half of the in-rack traffic between boards goes through L2: an L1 union's next hops are 4 EXT plus 4 L2.
   - In topology 2, some in-rack traffic crosses the SURE links. For example, `npu-0` → `npu-8` is 3 hops through rack 1, against 4 hops through L1.
8. The timing values are placeholders sized to the 100 ms–5 s OCS restore range.

## Open items (from implementation)

9. The HELLO interval: is one interval equal to `hello_check`? It decides when 3 missed HELLOs time out (D26). Candidate: 10 ms (see "Answers received").
10. ~~Periodic KEEPALIVE~~: closed. There is no standby controller, so v1 keeps one KEEPALIVE at startup (D52).
11. ~~What `--topology` means~~: closed. It stays a filter (D66).
12. ~~Whether `--config` or the scenario wins~~: closed. The config wins, with a warning (D67, D73).
