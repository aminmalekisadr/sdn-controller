## 0. How I want you to work

You are helping me (one engineer, one month) build an SDN controller for our NPU networks. This is the **simple version (v1)**: one simulator, four controller modules, and only what a module failure and its repair need. Everything else is listed in Section 13 and comes later. Read this whole document before writing code.

- **Work in phases (Section 10).** Do **Phase 0 and Phase 1 now**. Phases 2–8 are described so that the API you design fits them. Do not implement them yet.
- **At the end of each phase, stop and report:** what you built, how to run it, the test results and your open questions. Then wait for me to say "continue".
- **Keep it simple:**
  - Build only what this document asks for. If something from Section 13 looks necessary, ask me first.
  - Prefer simple, readable code over clever code.
  - No global state. Behavior is deterministic (Section 2).
- **Decisions:**
  - If something here is unclear or looks wrong, ask me before working around it.
  - Record every design decision in `docs/DECISIONS.md`, one short entry each, with the reason.
  - Copy my assumptions from Section 3.6 into DECISIONS.md as open items.
- **Progress:** keep `docs/PROGRESS.md` up to date (phase, status, what's next).
- **Tests:**
  - Write tests together with the code.
  - Every golden number in Sections 3 and 11 comes from `reference/ref_model.py` and is stored in `reference/golden.json`. Tests read the expected values from that file. The port, lane and OCS spot checks in Sections 3.2–3.4 are written out in this spec.
  - Your code must reproduce the golden numbers exactly.
  - Never edit a golden number, or `golden.json`, to make a test pass. If you think one is wrong, tell me.
  - `reference/` is mine and only for cross-checks. Code under `src/` must not import from it, and ruff and mypy skip it.
- **Vocabulary:** there are exactly three device types: **NPU**, **L1 switch** and **L2 switch**.
  - Do not add other device types (chip, die, host, leaf, spine, HRS and so on).
  - The names in my scripts (union, EXT, HRS) survive only as a `role` attribute.

## 1. What we are building and why

**Problem.** Our network uses BGP today, and after a failure BGP converges slowly:
- the news travels hop by hop;
- every router re-decides on its own;
- routers hunt through stale paths;
- timers add delay.

**Solution.** A centralized SDN controller:
- Devices report lane state to the controller.
- One controller computes the routes from the full map and pushes only the changes, in a safe order.
- Where an optical circuit switch (OCS) exists, it replaces failed lanes with spare lanes and so restores capacity.

**Devices.** There are three kinds:
- the **NPU**, a compute device that also forwards traffic;
- the **L1 switch**, inside a rack;
- the **L2 switch**, between racks.

Section 3 maps my scripts onto these three.

**Two optional features. Both can be switched on and off:**
1. **2+2 local protection** (`two_plus_two`). Optical ports come in domains of 4 ports × 400G = 1.6T, served by 2 optical modules with 4 lanes of 200G each.
   - **2+2:** every port has one lane on each module. When a module fails:
     - every port of the domain drops from 400G to 200G, so the domain runs at 50%;
     - no neighbor is lost, so routes do not change;
     - devices prune the dead lanes themselves, so nothing has to converge.
   - **2:1** (`two_plus_two = false`): ports 0–1 sit on module A and ports 2–3 on module B. A module failure takes two ports fully down, their neighbors are lost, and the controller must reroute.
2. **OCS** (`ocs`).
   - **With the OCS,** the controller restores the lost capacity in the background, in about 100 ms typically and up to 5 s. It moves the failed lanes to a spare module and puts the new lanes into service.
   - **Without the OCS,** routing alone handles the failure (Section 5.4), and capacity comes back when a technician repairs the module.

**Design principle:** the OCS does not replace fast local protection. It restores the capacity that local protection temporarily loses.

**This month's target:** controller logic that is fully testable in a built-in simulator, on our two networks:
- **topology 1:** the Huawei 910D design, which has no OCS;
- **topology 2:** the OCS-based SURE design.

## 2. Decisions already made

| Topic | Decision |
|---|---|
| Language | Python 3.11 or newer |
| Packaging | `pyproject.toml`, src layout, package `sdnctl`. Dev tools: pytest, ruff, mypy, networkx (tests only) |
| Dependencies | Standard library first. numpy and scipy are allowed in the Routing Engine for speed. networkx is never used in `src/` |
| Data types | `@dataclass(frozen=True)` for values, `typing.Protocol` for interfaces, `enum.Enum` for enums. Type hints everywhere; mypy must pass |
| Concurrency | Single-threaded and event-driven. One event queue (heapq) drives everything. No threads, no asyncio |
| Time | Integer microseconds inside (`SimTime = int`). The simulator API takes milliseconds |
| Determinism | Sort before iterating over any set, using the sort keys in Section 3.1. Never use Python's `hash()`; use `zlib.crc32` for flow hashing. No randomness |
| Logging | Every message and state change goes to a JSON-lines trace through `ITraceSink` |
| IDs | Strings. Devices `npu-<n>`, `l1-<n>`, `l2-<n>`, where n is the node id in my script. Ports `<device>.p<k>`. Lanes `<port>.<j>`. Modules `<device>.m<k>`. Optical domains `<device>.d<k>`. OCS ports `N<k>` and `S<k>` |
| Units | A lane is 200 Gb/s. A port, and a link, is 400 Gb/s = 2 lanes |
| Speed | `compute()` on topology 1 (1,312 devices × 512 destinations) must finish within 5 s; report the measured time. Simulated compute time comes from config, not from the wall clock |

## 3. The two networks

My two topology scripts are in Appendix A and Appendix B, and in `reference/topologies/`.
- The builders in `src/` re-implement the scripts' wiring.
- A test runs my scripts with the stub `net_sim_builder` and `PHY_model` modules in `reference/stubs/`, and checks that the edges match.

### 3.1 How my scripts map to NPU, L1 and L2

| In my scripts | In the controller |
|---|---|
| A david compute die plus its two IO dies (die A is `GRID_START + d`, die B is `GRID_START + TOTAL_COMPUTES + d`) | One **NPU**, `npu-<d>`. The compute-to-IO-die edges disappear. NPUs forward transit traffic, as the IO dies do in the scripts |
| L1 unions (16 per plane per rack) | An **L1 switch** with role `union` |
| Extension unions, EXT (8 per plane per rack) | An **L1 switch** with role `ext`; they are inside the rack |
| HRS (5808) switches | An **L2 switch** with role `hrs` |
| The SURE OCS (topology 2) | The **OCS**: an optical device, not a routing node |

- **Ports:** numbered per device in the order the script creates the edges: `<device>.p0`, `.p1`, and so on.
- **Links and lanes:** each script edge is one **link** of 400G with 2 lanes. Lane `j` at one end faces lane `j` at the other end.
- **Link kinds:**
  - `d2d`: NPU to NPU on one board (copper);
  - `copper`: NPU to L1, and L1 union to EXT;
  - `optical`: L1 to L2 in both topologies, and NPU to NPU across racks in topology 2.
- **Routing domain:**
  - Topology 1 routes over all devices, like your `all_shortest_paths`.
  - Topology 2 routes over NPUs and L1 switches only, like your `scaleup_paths` (it uses `inside`). Its L2 switches and L1–L2 links stay in the topology, but no route uses them.
- **Destinations** are NPUs only.
- **Sort keys:**
  - Devices sort by tier (NPU < L1 < L2), then by number.
  - Lanes sort by device, then port number, then lane number.
  - Neighbor lists and lane lists are always sorted this way.

### 3.2 Topology 1: the Huawei 910D design (no OCS)

The script parameters: 8 racks × 8 boards × 8 NPUs, with 4 planes. Each rack has 16 unions and 8 EXT per plane. There are 32 L2 switches in one switching domain. The script's `SW_BASE` is 1536 and `HRS_START` is 2304.

| Item | Count |
|---|---|
| Devices | 1,312 = 512 NPU + 768 L1 (512 union, 256 EXT) + 32 L2 |
| Links | 9,984 = 1,792 NPU–NPU (d2d) + 4,096 NPU–L1 + 2,048 union–EXT + 2,048 L1–L2 (optical) |
| Lanes | 39,936 |
| Optical domains | 1,024 (512 on unions, 512 on L2). 2,048 modules, no spares |
| Route entries | 671,232 |
| Group entries | 19,968 |
| NPU-pair hop counts | 1 hop: 3,584 pairs (same board); 4 hops: 258,048 pairs |
| Next-hop set sizes | 1 neighbor: 40,448 entries; 4: 229,376; 8: 401,408 |

**Wiring** (r = rack, p = plane, b = board within the rack, base = `1536 + 96r + 24p`):
- NPU `d` sits on board `d // 8` and in rack `d // 64`.
- **d2d:** every pair of NPUs on a board is linked.
- **NPU to L1:** every NPU of board b connects, in every plane p, to the left union `base + 2b` and the right union `base + 2b + 1`.
- **Union to EXT:** every left union (`base + 2b`) connects to the 4 left EXT `base + 16` … `base + 19`. Every right union (`base + 2b + 1`) connects to the 4 right EXT `base + 20` … `base + 23`.
- **Union to L2:** the left union of plane p connects to L2 switches `2304 + 8p + k`, k = 0…3. The right union connects to `2304 + 8p + 4 + k`.

**Reference devices for the scenarios:**
- `l1-1536` is rack 0, plane 0, the left union of board 0.
  - p0–p7 → `npu-0` … `npu-7`;
  - p8–p11 → `l1-1552` … `l1-1555` (EXT);
  - p12–p15 → `l2-2304` … `l2-2307`. These four ports form optical domain `l1-1536.d0`.
- `l2-2304` has 64 ports in 16 domains. Domain `l2-2304.d0` is p0–p3, which go to `l1-1536`, `l1-1538`, `l1-1540` and `l1-1542`.
- `npu-0`:
  - p0–p6 → `npu-1` … `npu-7`;
  - p7–p14 → `l1-1536`, `l1-1537`, `l1-1560`, `l1-1561`, `l1-1584`, `l1-1585`, `l1-1608`, `l1-1609`.

### 3.3 Topology 2: the OCS-based SURE design

There are 2 racks with the same rack layout as topology 1, except that each NPU's die-A plane is given to optics:
- NPU `d` has no link to plane `d % 4`.
- Its 2 freed ports go to the OCS.

The script's `SW_BASE` is 384 and `HRS_START` is 576. Counts use one spare module per NPU domain, the scenario setting; values in brackets are for the script default of no spares.

| Item | Count |
|---|---|
| Devices | 352 = 128 NPU + 192 L1 (128 union, 64 EXT) + 32 L2 (not routed) |
| Links | 2,368 = 448 d2d + 768 NPU–L1 + 512 union–EXT + 512 L1–L2 (optical, not routed) + 128 NPU–NPU optical through the OCS |
| Lanes | 9,728 [9,472] |
| Optical domains | 384: 128 NPU domains of 2 ports, 128 on unions, 128 on L2 |
| Modules | 768 working; 128 spare [0] |
| OCS | 256 working cross-connects; 384 ports per side [256] |
| Routed devices | 320 |
| Route entries | 40,832 |
| Group entries | 4,608 over all devices (L2 switches keep group tables); 3,584 have both ends in the routing domain |
| NPU-pair hop counts | 1: 1,024; 2: 1,792; 3: 7,168; 4: 6,272 |

**Wiring differences from topology 1** (base = `384 + 96r + 24p`; L2 = `576 + 8p + k`):
- NPU `d` connects to the left and right union of its board in the 3 planes other than `d % 4`. For `npu-0`:
  - p0–p6 → `npu-1` … `npu-7`;
  - p7–p12 → `l1-408`, `l1-409`, `l1-432`, `l1-433`, `l1-456`, `l1-457`.
- **SURE links:** rack-0 NPU `d` (board b = `d // 8`, index i = `d % 8`) has two optical links to rack-1 NPU `64 + 8i + b`, its transpose. Both links go to the same peer:
  - `npu-0` p13 and p14 ↔ `npu-64` p13 and p14;
  - `npu-1` ↔ `npu-72`.
- **OCS:**
  - North holds the optical lanes of rack-0 NPUs. South holds those of rack-1 NPUs.
  - Working lanes are numbered first, in (NPU, port, lane) order. Spare lanes are numbered after all working lanes.
  - North: `npu-0.p13.0`, `.p13.1`, `.p14.0` and `.p14.1` are N1–N4. `npu-1` is N5–N8, and so on. The spare lanes `npu-0.p15.0` and `.p15.1` are N257 and N258.
  - South works the same way: `npu-64` is S1–S4, and its spare lanes are S257 and S258.
  - Cross-connects: N1–S1, N2–S2, N3–S3 and N4–S4 join `npu-0` and `npu-64`. N5–S33 and N6–S34 start `npu-1` ↔ `npu-72`.

### 3.4 Optical domains and modules: 2+2 and 2:1

- A device's optical ports, taken in port order, are cut into domains:
  - 4 ports per domain on L1 and L2;
  - 2 ports per domain on NPUs (an assumption, Section 3.6).
- **Naming:**
  - Domain k is `<device>.d<k>`.
  - Its working modules are `<device>.m<2k>` (A) and `<device>.m<2k+1>` (B).
  - Spare modules are numbered after all working modules of the device. Spare ports are numbered after all working ports.
- Copper and d2d links have no modules.

| Domain | Mapping | Module A | Module B | When module A fails |
|---|---|---|---|---|
| 4 ports, P0–P3 | 2+2 | P0.0 P1.0 P2.0 P3.0 | P0.1 P1.1 P2.1 P3.1 | All 4 ports at 200G; no neighbor lost |
| 4 ports | 2:1 | P0.0 P0.1 P1.0 P1.1 | P2.0 P2.1 P3.0 P3.1 | P0 and P1 down (their neighbors are lost); P2 and P3 at 400G |
| 2 ports, P0–P1 (NPU) | 2+2 | P0.0 P1.0 | P0.1 P1.1 | Both ports at 200G |
| 2 ports | 2:1 | P0.0 P0.1 | P1.0 P1.1 | P0 down; P1 at 400G |

**Examples:**
- `l1-1536.m0`:
  - under 2+2: `l1-1536.p12.0`, `.p13.0`, `.p14.0`, `.p15.0`;
  - under 2:1: `.p12.0`, `.p12.1`, `.p13.0`, `.p13.1`.
- `npu-0.m0` (topology 2):
  - under 2+2: `npu-0.p13.0`, `.p14.0`;
  - under 2:1: `.p13.0`, `.p13.1`.
- The spare `npu-0.m2` is `npu-0.p15.0` and `.p15.1`, on spare port p15.

**Spare modules:**
- A spare module has as many lanes as a working module: 2 on an NPU domain.
- Spare modules exist only on OCS-attached domains, which are the NPU domains of topology 2.
- The builder parameter `spare_modules_per_domain` sets how many each domain gets. The script default is 0.

### 3.5 Device agents

Each device has a device agent, the UBM:
- In hardware there is one UBM per board (serving its 8 NPUs) and one per switch.
- The simulator models one agent per device.
- Message counts are per device (NPU, L1 or L2), not per UBM.

### 3.6 Assumptions (copy into DECISIONS.md; I will confirm them)

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

## 4. Glossary

| Term | Meaning |
|---|---|
| **NPU** | A compute device that also forwards traffic. Tier `npu` |
| **L1 switch** | A switch inside a rack (role `union` or `ext`). Tier `l1` |
| **L2 switch** | A switch between racks (role `hrs`). Tier `l2` |
| **UBM** | The device agent. It reports lane state and installs tables. It never computes routes |
| **Port** | One 400G interface of a device, `<device>.p<k>` |
| **Link** | Two ports joined directly (copper) or through optics and, in topology 2, the OCS |
| **Lane** | One 200G channel of a port, `<port>.<j>` with j = 0 or 1. It faces lane j at the far end, or whichever lane the OCS cross-connect points to |
| **Module** | An optical module on an NPU or a switch. It owns lanes and is the failure unit. Always used between L1 and L2; NPU-to-NPU links can use them too |
| **Optical domain** | The ports served by one pair of working modules: 4 ports on switches, 2 on NPUs |
| **2+2 / 2:1** | How the lanes of a domain's ports are spread over its two modules (Section 3.4) |
| **OCS port** | `N<k>` (North, rack 0) or `S<k>` (South, rack 1); one per lane |
| **Cross-connect** | One North port ↔ one South port. At most one per port |
| **Spare module** | Powered, cabled to the OCS and idle; its lanes are IDLE. The OCS can turn its lanes into replacements |
| **Neighbor** | A device with at least one usable lane to this device (Section 5.1) |
| **Neighbor group** | The ACTIVE lanes from a device toward one neighbor, over all links to it. The device runs it as a LAG or an ECMP group |
| **Route** | Destination NPU → list of neighbors (ECMP across neighbors) |
| **Routing domain** | The devices that get route tables: all of topology 1; the NPUs and L1 switches of topology 2 |
| **Incident** | One failure as the controller sees it. Several reports with the same root cause become one incident |
| **Version** | A monotonically increasing number on topology state and on every table write. Devices ignore anything older than what they hold |

## 5. System model

### 5.1 Two-level device tables (central to the design)

Devices hold two tables:
1. **Route table** (only devices in the routing domain): `dest NPU → RouteEntry(neighbors, hops)`.
   - `neighbors` is every neighbor in the routing domain that is one hop closer to the destination: a BFS over devices by hop count, with ECMP across all of them.
   - **Lanes never enter the BFS.**
2. **Group table** (every device): `neighbor → [ACTIVE lanes]`, with equal weights. A group with no ACTIVE lanes is removed, so there are no empty groups.

**Forwarding:**
1. Look up the route.
2. Hash the flow to pick a neighbor among the route neighbors that still have a group.
3. Hash again to pick a lane in that neighbor's group.
4. If no route neighbor has a group, drop the packet.

**Installed versus target tables:**
- A device's installed groups hold its ACTIVE lanes.
- `compute()` builds the target tables from **usable** lanes. A lane is usable when it is ACTIVE, or VERIFYING with LANE_UP received from both ends.
- Neighbors for the BFS are devices with at least one usable lane.
- So a verified lane appears in the target tables first. The diff turns it into a GROUP_SET (and, if a neighbor comes back, ROUTE_SETs). The lane becomes ACTIVE when the device applies that GROUP_SET.

**Consequences:**
- A lane or module failure that leaves at least one lane toward a neighbor changes **only groups**. The device prunes locally, and the controller later writes the restored group. **Zero route changes.**
- If a group becomes **empty**, that neighbor is lost. The Routing Engine reruns the BFS, and the scheduler pushes the route changes in ordered waves (Section 7.4).
- The controller keeps a copy of every device's tables. When LANE_DOWN arrives, it applies the same local prune to its copy. It never sends GROUP_SET just to prune.
- Two route entries are equal only if their neighbors and hops are equal. The diff counts every entry that differs.

### 5.2 Lane states

Spare lanes start `IDLE`. A working lane moves `DOWN → CONNECTING → VERIFYING → ACTIVE`:
- **DOWN → CONNECTING:** OCS_SET is sent, or a repair starts.
- **CONNECTING → VERIFYING:** light is back.
- **VERIFYING → ACTIVE:** the HELLO identity matches and the error check is clean, and then the device applies the GROUP_SET that adds the lane. A lane is ACTIVE exactly when it is in its device's group.
- **Any state → DOWN:** loss of light, module fault, 3 missed HELLOs, or errors over the threshold.

Only ACTIVE lanes carry traffic. The DOWN lanes of a failed module become `FAILED` once a spare replaces them.

### 5.3 Capacity

A lane pair (a lane and the lane it faces) is **in service** when both lanes are ACTIVE. Spare lanes count once they are in service.
- **Domain capacity** = in-service lanes of the domain (spare lanes included) ÷ the domain's lanes in service at startup.
- **Pair capacity** (devices a and b) = in-service lane pairs between a and b ÷ the startup count.
- **Device capacity** = in-service lanes of the device ÷ its lanes in service at startup (IDLE spares are not counted at startup).
- An incident ends `CLOSED` when the pair capacity of every device pair it touched is back to 1.0, and `DEGRADED` otherwise.

Scenario capacities are measured on the simulated devices, not on the controller's copy, which can lag behind a lost ACK.

### 5.4 Failure handling without the OCS (v1)

v1 uses two layers:

| Layer | Where | How fast | What it fixes |
|---|---|---|---|
| 1. Local prune (ECMP) | Device | `local_prune` (about 1 ms) | Dead lanes leave their group. When a group empties, the device stops using that neighbor in every route that still has another neighbor |
| 2. Controller recompute | Controller | plan + compute + waves: 6–9 ms typical, 25–35 ms worst | Installs the next-best shortest paths on the new topology |

- **Why layer 2 is needed:** remote devices cannot see the failure. They keep sending toward a neighbor that has lost its path.
- **Example (G2):** after a 2:1 module failure on `l1-1536`, layer 1 fixes `l1-1536`'s own 504 affected entries at once. Layer 2 fixes the 63 remote unions; until wave 1 lands at 6 ms, 4,032 NPU pairs lose some traffic.
- A precomputed loop-free backup next hop is a later addition (Section 13).

## 6. Feature flags and modes

```json
{ "features": { "two_plus_two": true, "ocs": true } }
```

- **`two_plus_two`:** true selects the 2+2 mapping, false selects 2:1.
  - It changes only the topology builder and the validator rules on module wiring.
  - **The controller logic stays generic.** It reacts to "group shrank" versus "group became empty". Do not scatter `if two_plus_two` through the controller.
- **`ocs`:**
  - `true`: use the `OcsMatrixController`.
  - `false`: use the `NullOcsController`, whose `plan_repair()` returns `NoRepair("no_ocs")`.
  - Topology 1 has no OCS (the OXC block in the script is commented out), so it always runs with `ocs = false`. The validator rejects `ocs = true` on a topology without an OCS.
  - In topology 2 with `ocs = false`, the cross-connects stay as built.

| Topology | 2+2 | OCS | One module fails | Until restore | Restore |
|---|---|---|---|---|---|
| 1 | on | none | 4 links at 50%. 8 group entries on 5 devices pruned locally. 0 route changes, 0 writes | 50% on those 4 links | Repair → LANE_UP → GROUP_SET (G1) |
| 1 | off (2:1) | none | 2 neighbors lost. 1,024 route entries on 66 devices change, in 2 waves (6 and 9 ms) | 2 of 4 uplinks | Repair → routes go back, endpoints first (G2) |
| 2 | on or off | on | The pair runs at 50%. 0 route changes: both optical ports go to the same peer, so even 2:1 keeps the neighbor | 50% | Spare via the OCS → GROUP_SET → 100% at 98 ms typical, 4,975 ms worst (G4) |
| 2 | either | on, no spare | Same | 50% | None: `DEGRADED_NO_SPARE` (G5) |
| 2 | either | off | Same | 50% until repair | Repair → GROUP_SET |
| 2 | either; both modules fail | off | Neighbor lost. 740 route entries on 208 devices change | Detour | Repair → routes go back (G6) |

## 7. Architecture

```
            +------------------------------ SdnController ------------------------------+
            |  SDN Scheduler  (incidents, phases, waves, timeouts;                      |
            |                  the only sender of commands)                             |
            |     |                    |                        |                       |
            |  TopologyManager     RoutingEngine         OcsMatrixController            |
            |  (state, lanes,      (routes, groups,      (matrix, spare picker,         |
            |   incidents)          diff, waves, loops)   repair plan) / NullOcs        |
            +-------|------------------------------------------|------------------------+
                    | commands down / events up                | OCS_SET / OCS_DONE
              IDeviceAdapter                                IOcsAdapter
              - SimDeviceAdapter (now)                      - SimOcsAdapter (now)
              - ns-3, real hardware later                   - ns-3, vendor OCS later
            +-------------------------------- Simulator --------------------------------+
            |  event clock · topology builder · device models (NPU, L1, L2)             |
            |  OCS model · failure injector · trace and report                          |
            +---------------------------------------------------------------------------+
```

### 7.1 Ownership rules (enforce them in code)

- **Topology Manager:** only listens and keeps state. It never sends commands.
- **Routing Engine:** pure computation. `compute()`, `diff()`, `order_waves()` and `check_state()` have no side effects.
- **OCS Matrix Controller:** plans OCS changes and tracks the matrix. It never sends commands itself.
- **SDN Scheduler:** the **only** component that sends anything to devices or the OCS.
- **One writer per thing:** a device's tables are written only by the scheduler, and so is the OCS.
- **Incidents:** the Topology Manager creates them (correlation) and holds lane, module and spare state. From then on the scheduler owns each incident's phase and state, and reports outcomes back through `on_groups_installed()` and `record_repair()`.

### 7.2 Event flow for one failure

1. **Detect.** Devices prune locally and send LANE_DOWN. Reports are correlated into one incident (Section 11.0, timing rule 2).
2. **Plan** (takes `plan`).
   - **Failed device:** the one that reported MODULE_FAULT. If no report says MODULE_FAULT, the controller cannot tell which end failed; `plan_repair()` returns `NoRepair("failed_end_unclear")`.
   - **Reroute:** if a neighbor was lost, the Routing Engine computes new tables (taking `compute`, after `plan`), and the scheduler pushes them in down-waves (Section 7.4).
   - **Spare:** with the OCS on, pick the lowest-numbered spare module of the failed domain on the failed device.
   - **Spare must cover everything:** if the spare has fewer lanes than the dead lanes, `plan_repair()` returns `NoRepair("not_enough_spare")`. A partial restore is a later addition.
   - **OCS pairs:** replace the dead lanes in module order, then lane order. Disconnect each dead lane's cross-connect, and connect a spare lane to the **same peer lane**.
   - **Targets:** the failed device and the peer device of every replaced lane.
   - **No repair** (no OCS, no spare, not enough spare, end unclear): after any reroute waves, the incident waits as `DEGRADED_WAITING_REPAIR` (no OCS or end unclear) or `DEGRADED_NO_SPARE`.
3. **Prepare** (in parallel with step 4, starting when `plan` ends): send PREPARE to every target. It opens the spare lanes and gives the expected peer lane for each new pair.
4. **Rewire:** send OCS_SET, wait for OCS_DONE, then read the matrix back and check it.
5. **Verify:** devices see light and check the HELLO identity and errors. Then both ends send LANE_UP. Without the OCS, a repair event starts this step.
6. **Activate:**
   - If a neighbor comes back, compute and push restore waves: endpoints first (group and route in one batch), then transit.
   - Otherwise send GROUP_SET to the targets in one wave.
   - Wait for every ACK.
7. **Close:** version +1; the scheduler calls `record_repair()`, so the failed module becomes `FAILED` and the used spare leaves the spare pool; the incident becomes `CLOSED` or `DEGRADED` (Section 5.3).

### 7.3 Timeouts (v1: one resend, no rollback)

| Wait | Default | On expiry |
|---|---|---|
| ACK | 50 ms after the command was sent | Resend the device's full tables as one batched command, once. If that is not acknowledged either, the incident ends `FAILED_NEEDS_OPERATOR` |
| OCS_DONE | 1 s after OCS_SET was sent | Read the matrix back. If the change was applied, continue. If not, resend OCS_SET once; if it still is not applied, the incident ends `FAILED_NEEDS_OPERATOR` |
| Verify | 6 s after OCS_DONE | The incident ends `FAILED_NEEDS_OPERATOR` |

- v1 rolls nothing back. A failed incident keeps the current capacity, which never drops below what local protection already provides, and waits for an operator.
- **Concurrency (v1):** one open incident at a time (later incidents queue), one OCS command in flight, and at most one outstanding command per device.

### 7.4 Route waves

- **Neighbor lost:** wave 1 = every device whose routes change, except the endpoints of the lost adjacencies (transit first); wave 2 = those endpoints.
- **Neighbor restored:** wave 1 = the endpoints, with group and route in one batch and the group applied first; wave 2 = transit.
- The next wave starts only after every ACK of the current wave.
- Group-only changes cannot loop. Send them in one wave.
- **Side effect to expect:** when an endpoint is an NPU, its own traffic toward destinations it could reach only over the lost link is dropped until wave 2. Wave 1 fixes remote traffic.
- **Known limit:** the order is loop-free at wave boundaries. Inside a wave, devices that apply at different times can loop (seen in topology 2). The simulator therefore applies all commands of a wave at the same instant. A loop-free update inside a wave is a later addition.

### 7.5 Device behavior in the simulator (`SimDevice`)

- **Tables:** holds route and group tables with a version, ignores older versions, and applies each command atomically (groups before routes in a batch).
- **On a fault at t0:**
  - removes the dead lanes from its groups at t0 + `local_prune`;
  - sends one LANE_DOWN per fault event at t0, listing every lane it lost. The cause is MODULE_FAULT with the module ids if its own module failed, and LOSS_OF_LIGHT otherwise.
- **Forwarding:** follows Section 5.1, including the ECMP prune.
- **HELLO:** model only the verification HELLO and the 3-missed timeout, not every packet.
- **Lanes:** receives on any lane in VERIFYING or ACTIVE; sends only on lanes in its groups.
- **Repair event:** the module's lanes come back on the same ports with the same peers. Light returns after `lane_bringup`.

### 7.6 Simulator API

The simulator hosts the devices, the OCS and the controller, and is driven through five calls:

| Call | What it does |
|---|---|
| `load_topology(name, two_plus_two, spares=0)` | Builds topology 1 or topology 2 and its devices, and installs the steady-state tables (version 1) |
| `configure(ocs, timing="typical")` | OCS controller on or off; typical or worst timing profile |
| `inject(t_ms, event, **args)` | `module_fail` and `repair` (args: `modules`), plus the test faults `drop_ack` (args: `device`, `command`, `count`) and `ocs_never_answers` |
| `run(until_ms)` | Runs the event queue: devices, OCS and controller |
| `report()` | The trace, capacity over time, message counts and each incident's end state |

## 8. Repository layout

```
sdn-ocs-controller/
  pyproject.toml
  scripts/check.sh             ruff + mypy + pytest
  docs/SPEC.md  docs/DECISIONS.md  docs/PROGRESS.md
  src/sdnctl/
    types.py                   ids, enums, SimTime, Version, IncidentId, sort keys
    model.py                   Device, Port, Link, Lane, Module, Domain, CrossConnect, TopologySpec, TopologyView, Incident
    tables.py                  RouteEntry, GroupEntry, DeviceTables, Tables, TableDiff, Wave
    messages.py                the 10 protocol messages, Header, DeviceCommand
    config.py                  Features, Timing, Timeouts, Limits, ControllerConfig
    interfaces.py              the Protocols in Section 9.4
    jsonio.py                  JSON in and out for all of the above
    topology/                  builder.py (topology 1 and 2), validator.py, loader.py
    topology_manager/          (Phase 3)
    routing_engine/            (Phase 4)
    ocs/                       (Phase 5) OcsMatrixController, NullOcsController
    controller/                (Phase 6) SdnController facade and wiring
    scheduler/                 (Phase 7) incidents, phases, waves, timeouts
    sim/                       (Phase 2) simulator API, event clock, SimDevice, SimOcs, adapters, injector, trace
    cli.py                     (Phase 8)
  scenarios/                   g1, g2, g4a, g4b, g5, g6, g6b, g7, g8 (.json; G3 is a unit test)
  tests/
  reference/                   from me: ref_model.py, golden.json, stubs/, topologies/ (cross-check only)
  build/                       generated files (not checked in; topology 1 is large)
```

## 9. Phase 0 and Phase 1 (do these now)

### Phase 0: repository skeleton

- `pyproject.toml` (Python 3.11 or newer), the `sdnctl` package, and pytest, ruff, mypy and networkx as dev dependencies.
- `scripts/check.sh`, empty `docs/DECISIONS.md` and `docs/PROGRESS.md`, and one passing test. ruff and mypy exclude `reference/`.
- Check that `reference/` is present. Run `python reference/ref_model.py --out build/ref_out` (it needs networkx and takes about 80 s) and check that `build/ref_out/golden.json` equals `reference/golden.json`. Never point `--out` at `reference/`.

### Phase 1: types, messages, interfaces, config, topology builders, validator

#### 9.1 Types (`types.py`, `model.py`)

```python
DeviceId = str    # "npu-0", "l1-1536", "l2-2304"
PortId = str      # "l1-1536.p12"
LaneId = str      # "l1-1536.p12.0"
ModuleId = str    # "l1-1536.m0"
DomainId = str    # "l1-1536.d0"
OcsPortId = str   # "N1", "S257"
SimTime = int     # microseconds
Version = int
IncidentId = int

class Tier(Enum): NPU = "npu"; L1 = "l1"; L2 = "l2"
class Role(Enum): NPU = "npu"; UNION = "union"; EXT = "ext"; HRS = "hrs"
class LinkKind(Enum): D2D = "d2d"; COPPER = "copper"; OPTICAL = "optical"
class ModuleMapping(Enum): TWO_PLUS_TWO = "2+2"; TWO_TO_ONE = "2:1"
class LaneState(Enum): IDLE = "idle"; DOWN = "down"; CONNECTING = "connecting"; VERIFYING = "verifying"; ACTIVE = "active"; FAILED = "failed"
class DownCause(Enum): LOSS_OF_LIGHT = "los"; MODULE_FAULT = "module_fault"; HELLO_TIMEOUT = "hello_timeout"; ERRORS = "errors"
class IncidentState(Enum): OPEN = "open"; REROUTING = "rerouting"; REPAIRING = "repairing"; ACTIVATING = "activating"; CLOSED = "closed"; DEGRADED = "degraded"; DEGRADED_WAITING_REPAIR = "degraded_waiting_repair"; DEGRADED_NO_SPARE = "degraded_no_spare"; FAILED_NEEDS_OPERATOR = "failed_needs_operator"
class MsgType(Enum): HELLO = "HELLO"; LANE_DOWN = "LANE_DOWN"; LANE_UP = "LANE_UP"; PREPARE = "PREPARE"; OCS_SET = "OCS_SET"; OCS_DONE = "OCS_DONE"; GROUP_SET = "GROUP_SET"; ROUTE_SET = "ROUTE_SET"; ACK = "ACK"; KEEPALIVE = "KEEPALIVE"

def device_key(d: DeviceId) -> tuple[int, int]: ...                  # (tier rank: npu 0, l1 1, l2 2; number)
def lane_key(x: LaneId) -> tuple[tuple[int, int], int, int]: ...     # (device_key, port number, lane number)

@dataclass(frozen=True)
class Device:
    id: DeviceId
    tier: Tier
    role: Role
    rack: int | None        # None for L2
    plane: int | None       # None for NPUs
    board: int | None       # NPUs only
    script_id: int          # node id in my script (for an NPU, its david id)

@dataclass(frozen=True)
class Port:
    id: PortId
    device: DeviceId
    index: int
    kind: LinkKind
    spare: bool             # spare ports belong to spare modules and have no link

@dataclass(frozen=True)
class Link:
    a: PortId
    b: PortId
    kind: LinkKind

@dataclass(frozen=True)
class Lane:
    id: LaneId
    device: DeviceId
    port: PortId
    index: int              # 0 or 1
    module: ModuleId | None # None on copper and d2d links
    ocs_port: OcsPortId | None
    spare: bool

@dataclass(frozen=True)
class Module:
    id: ModuleId
    device: DeviceId
    domain: DomainId
    lanes: tuple[LaneId, ...]
    spare: bool

@dataclass(frozen=True)
class Domain:
    id: DomainId
    device: DeviceId
    ports: tuple[PortId, ...]
    modules: tuple[ModuleId, ModuleId]          # (A, B)
    spare_modules: tuple[ModuleId, ...]
    mapping: ModuleMapping

@dataclass(frozen=True)
class CrossConnect:
    north: OcsPortId
    south: OcsPortId

@dataclass(frozen=True)
class TopologySpec:                             # physical facts only; feature flags live in the config
    name: str                                   # "topology1" or "topology2"
    mapping: ModuleMapping
    spare_modules_per_domain: int
    devices: tuple[Device, ...]
    ports: tuple[Port, ...]
    links: tuple[Link, ...]
    lanes: tuple[Lane, ...]
    modules: tuple[Module, ...]
    domains: tuple[Domain, ...]
    xconnects: tuple[CrossConnect, ...]         # empty when there is no OCS
    has_ocs: bool
    routed_tiers: frozenset[Tier]               # {NPU, L1, L2} or {NPU, L1}

@dataclass
class Incident:
    id: IncidentId
    opened_at: SimTime
    reports: list[LaneDown]
    failed_device: DeviceId | None
    failed_modules: tuple[ModuleId, ...]
    lanes: frozenset[LaneId]                          # every lane the reports name
    lost: frozenset[tuple[DeviceId, DeviceId]]        # (device, neighbor) whose group became empty
    state: IncidentState
    reason: str = ""                                  # why it ended DEGRADED or FAILED_NEEDS_OPERATOR
```

`TopologyView` is a read-only, versioned snapshot of the spec plus the dynamic state (lane states, current cross-connects and peers, failed modules). Its helpers are:
- `neighbors(device)`, `active_lanes(device, neighbor)`, `peer_of(lane)`, `lane_by_ocs_port(port)`, `routed(device)`;
- `device_capacity(device)`, `pair_capacity(a, b)`, `domain_capacity(domain)`.

#### 9.2 Tables (`tables.py`)

```python
@dataclass(frozen=True)
class RouteEntry:
    dest: DeviceId                      # always an NPU
    neighbors: tuple[DeviceId, ...]     # sorted by device_key
    hops: int

@dataclass(frozen=True)
class GroupEntry:
    neighbor: DeviceId
    lanes: tuple[LaneId, ...]           # ACTIVE lanes only, sorted by lane_key; never empty

@dataclass
class DeviceTables:
    device: DeviceId
    version: Version
    routes: dict[DeviceId, RouteEntry]
    groups: dict[DeviceId, GroupEntry]

Tables = dict[DeviceId, DeviceTables]

@dataclass(frozen=True)
class RouteChange:
    dest: DeviceId
    old: RouteEntry | None
    new: RouteEntry | None

@dataclass(frozen=True)
class GroupChange:
    neighbor: DeviceId
    old: GroupEntry | None
    new: GroupEntry | None

@dataclass
class TableDiff:
    routes: dict[DeviceId, list[RouteChange]]
    groups: dict[DeviceId, list[GroupChange]]
    def route_entries(self) -> int: ...
    def group_entries(self) -> int: ...
    def devices(self) -> set[DeviceId]: ...

@dataclass(frozen=True)
class AdjacencyChange:
    lost: frozenset[tuple[DeviceId, DeviceId]]       # (device, neighbor) whose group became empty
    restored: frozenset[tuple[DeviceId, DeviceId]]

@dataclass(frozen=True)
class Wave:
    index: int
    devices: tuple[DeviceId, ...]

@dataclass(frozen=True)
class ForwardingReport:
    looping_pairs: int
    blackholed_pairs: int
```

#### 9.3 Messages (`messages.py`): exactly these 10, each with a JSON round trip

Common header: `Header(type, seq, version, incident, src, dst, time)`.

| # | Type | Direction | Payload |
|---|---|---|---|
| 1 | HELLO | device ↔ device | device, lane (the receiver checks it against the expected peer lane) |
| 2 | LANE_DOWN | device → controller | device, lanes[], cause, modules[] (for MODULE_FAULT) |
| 3 | LANE_UP | device → controller | device, lanes[], peer_lanes[] (as seen in HELLO) |
| 4 | PREPARE | controller → device | open_lanes[], expected_peer {lane: peer lane} |
| 5 | OCS_SET | controller → OCS | disconnect[] (CrossConnect), connect[] |
| 6 | OCS_DONE | OCS → controller | per-pair result (ok or error), applied matrix version |
| 7 | GROUP_SET | controller → device | entries: neighbor → lanes[] (an empty list removes the entry) |
| 8 | ROUTE_SET | controller → device | entries: dest → neighbors[], hops (an empty neighbor list removes the entry) |
| 9 | ACK | device → controller | acked seq, ok, error text |
| 10 | KEEPALIVE | controller ↔ device | controller id, epoch |

- **Batching:** GROUP_SET and ROUTE_SET for the same device may go out as one `DeviceCommand(group_set=..., route_set=...)`. The device applies the groups first, atomically. A batch counts as one message.
- **The OCS** answers OCS_SET only with OCS_DONE, never with ACK.
- **Class names:** `Hello`, `LaneDown`, `LaneUp`, `Prepare`, `OcsSet`, `OcsDone`, `GroupSet`, `RouteSet`, `Ack`, `Keepalive`; `DeviceCommand(prepare=None, group_set=None, route_set=None, keepalive=None)`; `DeviceEvent = LaneDown | LaneUp | Ack`.

#### 9.4 Interfaces (`interfaces.py`)

Start from these signatures. You may adjust them, but record why in DECISIONS.md.

```python
class IClock(Protocol):
    def now(self) -> SimTime: ...
    def schedule(self, delay: SimTime, fn: Callable[[], None]) -> None: ...

class ITraceSink(Protocol):
    def record(self, event: Mapping[str, Any]) -> None: ...

class IDeviceAdapter(Protocol):     # southbound to device agents (UBMs)
    def send(self, device: DeviceId, cmd: DeviceCommand) -> None: ...           # PREPARE, GROUP_SET, ROUTE_SET (or a batch), KEEPALIVE
    def set_event_sink(self, sink: Callable[[DeviceEvent], None]) -> None: ...  # LANE_DOWN, LANE_UP, ACK

class IOcsAdapter(Protocol):        # southbound to the OCS
    def apply(self, cmd: OcsSet) -> None: ...
    def read_matrix(self) -> list[CrossConnect]: ...                            # an adapter call, not a counted message
    def set_event_sink(self, sink: Callable[[OcsDone], None]) -> None: ...

class ITopologyManager(Protocol):
    def load(self, spec: TopologySpec) -> None: ...
    def on_lane_down(self, msg: LaneDown, now: SimTime) -> None: ...
    def on_lane_up(self, msg: LaneUp, now: SimTime) -> None: ...
    def on_ocs_done(self, msg: OcsDone, matrix: Sequence[CrossConnect]) -> None: ...
    def poll_incidents(self, now: SimTime) -> list[Incident]: ...               # closes correlation windows
    def on_groups_installed(self, device: DeviceId, groups: GroupSet, now: SimTime) -> None: ...  # their lanes become ACTIVE
    def record_repair(self, plan: RepairPlan, now: SimTime) -> None: ...        # failed modules FAILED, spare leaves the pool
    def view(self) -> TopologyView: ...
    def version(self) -> Version: ...

class IRoutingEngine(Protocol):     # pure functions
    def compute(self, view: TopologyView) -> Tables: ...
    def diff(self, installed: Tables, target: Tables) -> TableDiff: ...
    def order_waves(self, diff: TableDiff, change: AdjacencyChange) -> list[Wave]: ...
    def check_state(self, tables: Tables, view: TopologyView) -> ForwardingReport: ...   # tables may mix old and new per device

class IOcsMatrixController(Protocol):
    def enabled(self) -> bool: ...
    def plan_repair(self, incident: Incident, view: TopologyView) -> RepairPlan | NoRepair: ...
    def on_applied(self, cmd: OcsSet, read_back: Sequence[CrossConnect]) -> None: ...

class IScheduler(Protocol):
    def on_device_event(self, ev: DeviceEvent) -> None: ...
    def on_ocs_event(self, ev: OcsDone) -> None: ...
    def on_timer(self, now: SimTime) -> None: ...

class ISimulator(Protocol):         # Section 7.6
    def load_topology(self, name: str, two_plus_two: bool, spares: int = 0) -> TopologySpec: ...
    def configure(self, ocs: bool, timing: str = "typical") -> None: ...
    def inject(self, t_ms: int, event: str, **args: Any) -> None: ...
    def run(self, until_ms: int) -> None: ...
    def report(self) -> SimReport: ...      # trace, capacity over time, message counts, incident end states

@dataclass(frozen=True)
class RepairPlan:
    failed_device: DeviceId
    failed_modules: tuple[ModuleId, ...]
    spare_module: ModuleId
    disconnect: tuple[CrossConnect, ...]
    connect: tuple[CrossConnect, ...]
    targets: frozenset[DeviceId]
    prepares: Mapping[DeviceId, Prepare]

@dataclass(frozen=True)
class NoRepair:
    reason: str     # "no_ocs", "no_spare", "not_enough_spare" or "failed_end_unclear"
```

#### 9.5 Config (`config.py`), loaded from JSON with defaults

```json
{
  "features": { "two_plus_two": true, "ocs": true },
  "timing_profile": "typical",
  "timing_us": {
    "typical": { "local_prune": 1000, "report": 1000, "plan": 1000, "compute": 1000,
                 "ocs_command": 5000, "mirror_move": 25000, "lane_bringup": 50000,
                 "hello_check": 2000, "error_check": 10000, "lane_up_report": 1000,
                 "device_write": 3000, "correlation_window": 5000 },
    "worst":   { "local_prune": 5000, "report": 5000, "plan": 5000, "compute": 5000,
                 "ocs_command": 50000, "mirror_move": 200000, "lane_bringup": 2500000,
                 "hello_check": 200000, "error_check": 2000000, "lane_up_report": 5000,
                 "device_write": 10000, "correlation_window": 5000 }
  },
  "timeouts_us": { "ack": 50000, "ocs_done": 1000000, "verify": 6000000 },
  "limits": { "ack_resends": 1, "ocs_resends": 1 }
}
```

- All times are design estimates sized to the 100 ms–5 s OCS restore range: the typical profile restores in 98 ms, the worst in 4,975 ms. Keep them in config, never as constants in the code.
- The timeouts exceed the worst profile: OCS_DONE arrives at most 250 ms after OCS_SET, and LANE_UP at most 4,705 ms after OCS_DONE.

#### 9.6 Topology builders

```python
def build_topology1(two_plus_two: bool) -> TopologySpec: ...
def build_topology2(two_plus_two: bool, spare_modules_per_domain: int = 0) -> TopologySpec: ...
def validate(spec: TopologySpec, features: Features) -> None: ...   # raises TopologyError (Section 9.7)
```

- Re-implement the wiring of Appendix A and Appendix B. Do not import my scripts from `src/`.
- **Script test:**
  1. Run each script with `reference/stubs` first on `sys.path`. For topology 1, pass a dummy `dir_name` argument.
  2. Collapse its edges as in Section 3.1.
  3. The multiset of (device, device, kind) and the port order of every device must equal the builder's.
- Write the specs to `build/*.json` with `jsonio`, and read them back.

#### 9.7 Validator: reject a topology, with a clear error message, if

1. a port is in two links;
2. a lane appears twice;
3. a cross-connect joins two North ports or two South ports, or an OCS port is in two cross-connects;
4. a spare lane is cross-connected;
5. a domain has the wrong number of ports (4 on L1 and L2, 2 on NPUs) or does not have exactly 2 working modules;
6. the module wiring breaks the domain's mapping:
   - 2+2: each port has exactly one lane on each working module;
   - 2:1: each port's 2 lanes are on one module, and each module serves half the ports;
7. `features.ocs` is true on a topology without an OCS;
8. a port does not have exactly 2 lanes;
9. `features.two_plus_two` does not match the topology's `mapping`.

#### 9.8 Phase 1 tests (all must pass)

1. **JSON round trip** of every message type, the config and both topologies.
2. **Builders match my scripts** (Section 9.6).
3. **Counts** match Sections 3.2–3.3. The values are in `golden.json` under `topology1`, `topology2_spare1` and `topology2_spare0`.
4. **Spot checks:** the port lists in Sections 3.2 and 3.3; the module lanes in Section 3.4 for both mappings; the OCS port numbers; and the cross-connects N1–S1 … N4–S4 and N5–S33.
5. **Validator:** accepts the 6 generated topologies (topology 1 with each mapping; topology 2 with each mapping, with 0 and with 1 spare) and rejects 9 broken cases, one per rule.
6. **TopologyView, topology 1 with 2+2:**
   - `neighbors("l1-1536")` = `npu-0` … `npu-7`, `l1-1552` … `l1-1555`, `l2-2304` … `l2-2307` (16 devices);
   - `active_lanes("l1-1536", "l2-2304")` = (`l1-1536.p12.0`, `l1-1536.p12.1`);
   - every capacity is 1.0.
7. **TopologyView, topology 2 with one spare:**
   - `neighbors("npu-0")` = `npu-1` … `npu-7`, `npu-64`, `l1-408`, `l1-409`, `l1-432`, `l1-433`, `l1-456`, `l1-457` (14 devices);
   - `active_lanes("npu-0", "npu-64")` = the 4 lanes of p13 and p14. Spare lanes are IDLE, so they are not included.

**Definition of done for Phase 1:**
- `scripts/check.sh` passes from a clean checkout (ruff, mypy, pytest).
- Every public type and function has a one-line docstring.
- DECISIONS.md and PROGRESS.md are updated.
- Then stop and report to me.

## 10. Next phases (do not implement yet; design the API so they fit)

| Phase | Module | v1 content | Exit check |
|---|---|---|---|
| 2 | **Simulator** | The simulator API (Section 7.6). Event clock. `SimDevice` (Section 7.5), whose group tables can be built straight from the topology. `SimOcs`: applies OCS_SET after `ocs_command`, settles after `mirror_move`, light after `lane_bringup`. Adapters that deliver every message with exactly the latencies of Section 11.0. Failure injector and the two test faults. Trace and report | G1's local prune: 8 group entries on 5 devices; the devices' route tables are untouched |
| 3 | **Topology Manager** | Topology store, lane state machine (Section 5.2), incident correlation (Section 11.0, rule 2), capacity and versions, spare pool and failed-module records | G2: 3 reports → 1 incident, failed device `l1-1536`. G1's capacities |
| 4 | **Routing Engine** | Route builder (BFS), group builder, `diff`, `order_waves` (Section 7.4), `check_state` (Section 11.0). Measure the wall time of `compute()` on topology 1 | The table numbers in Section 3. The diffs, waves and pair counts of G2, G3 and G6 |
| 5 | **OCS Matrix Controller** | Matrix and OCS port numbering, spare picker, repair plan (Section 7.2), read-back check, `NullOcsController` | The OCS pairs of G4a and G4b. G5's "no spare" and G6b's "not enough spare" |
| 6 | **SdnController** | Facade wiring the four modules and the adapters. Installs the initial tables at startup (version 1). KEEPALIVE | Steady state on both topologies: every device table equals `compute()`; 0 looping and 0 blackholed pairs |
| 7 | **SDN Scheduler** | Incident runner and phase machine (Section 7.2), command sender with waves, timeouts with one resend (Section 7.3) | G1–G8 |
| 8 | **CLI and report** | `sdnctl-sim --topology --scenario --config --out`. Writes `trace.jsonl`, `summary.json` and `capacity.csv` | Runs all scenarios in one command |

## 11. Golden scenarios (v1)

All values are in `reference/golden.json` under the keys below. They come from my reference model.

| Id | Topology | 2+2 | OCS | Event | What it checks |
|---|---|---|---|---|---|
| G1 | 1 | on | none | `l1-1536.m0` fails; repair at 10 s | Group-only prune, 0 route changes, repair |
| G2 | 1 | 2:1 | none | Same | Reroute (1,024 entries on 66 devices), waves, loops, repair |
| G3 | 1 and 2 | — | — | Unit test | The wrong wave order makes loops |
| G4a / G4b | 2 | on / 2:1 | on, 1 spare | `npu-0.m0` fails | OCS restore, 14 messages, 98 ms |
| G5 | 2 | on | on, no spare | `npu-0.m0` fails | `DEGRADED_NO_SPARE` |
| G6 (G6b) | 2 | on | off (on, 1 spare) | `npu-0.m0` and `npu-0.m1` fail; repair at 10 s (no repair) | Reroute and back (not enough spare) |
| G7 | 2 | on | on, 1 spare | G4a, and the OCS never answers | Timeout, one resend, operator |
| G8 | 2 | on | on, 1 spare | G4a, and one ACK is lost | Timeout, one resend |

**Scenario file format** (`scenarios/*.json`):

```json
{ "name": "G4a", "topology": "topology2", "spare_modules_per_domain": 1,
  "features": { "two_plus_two": true, "ocs": true },
  "timing_profile": "typical",
  "events": [ { "t_ms": 0, "event": "module_fail", "modules": ["npu-0.m0"] } ] }
```

Test faults use the same list, for example `{ "t_ms": 0, "event": "ocs_never_answers" }` and `{ "t_ms": 0, "event": "drop_ack", "device": "npu-64", "command": "GROUP_SET", "count": 1 }`.

### 11.0 Rules behind these numbers

**Starting state.** Every scenario starts from steady state: every table installed at version 1 and every working lane ACTIVE. The startup install is not counted.

**Timing** (config values; typical profile unless a scenario says otherwise):
1. **Fault.** A fault happens at t0. Every device that loses a lane prunes it at t0 + `local_prune`, and sends one LANE_DOWN at t0 that reaches the controller at t0 + `report`.
2. **Correlation.** The first report opens an incident.
   - If any report so far is MODULE_FAULT, planning starts at once; reports with the same arrival time join first.
   - Otherwise planning starts `correlation_window` after the first report.
3. **Plan and compute.** Planning takes `plan`. If a neighbor was lost, the Routing Engine then takes `compute`, and the route waves start when it ends.
4. **Device commands.** Any device command (PREPARE, GROUP_SET, ROUTE_SET or a batch) is applied by the device, and its ACK reaches the controller, `device_write` after it is sent. A wave is done when its last ACK arrives; the next wave starts then.
5. **OCS.** Prepare and Rewire both start when `plan` ends. OCS_SET is applied `ocs_command` after it is sent; the mirrors settle `mirror_move` later, and OCS_DONE reaches the controller at that moment.
6. **Lane bring-up.** Light returns `lane_bringup` after the mirrors settle, or after a repair event. The HELLO passes `hello_check` later and the error check `error_check` after that. Each end's LANE_UP reaches the controller `lane_up_report` later.
7. **Activation.** Once every new lane is up at both ends: if a neighbor comes back, the controller takes `compute` and runs the restore waves; otherwise it sends one GROUP_SET wave.
8. **Timeouts:** ACK and OCS_DONE timeouts run from the moment the command was sent; the verify timeout runs from OCS_DONE.
9. **End states:**
   - `DEGRADED_WAITING_REPAIR` and `DEGRADED_NO_SPARE` are set when the last reroute wave is acknowledged, or when planning ends if there are no waves.
   - `CLOSED` and `DEGRADED` are set when the last ACK of the incident arrives.
   - `FAILED_NEEDS_OPERATOR` is set when the last timeout expires.

**Counting messages.**
- Count every controller↔device and controller↔OCS message that is sent, including one the network then loses.
- Do not count HELLO or KEEPALIVE.
- `read_matrix` is an adapter call, not a message.
- A batch counts as one.

**Pair counts** (the loop checker, `check_state`):
- For a destination t, a device's **live neighbors** are its route neighbors that have a non-empty group.
- A (source NPU, destination NPU) pair, source ≠ destination, is:
  - **looping** if some walk from the source along live neighbors revisits a device;
  - **blackholed** if some walk reaches a device other than t that has no live neighbor.
- A pair can be both. Counts are over all ordered NPU pairs.
- **States between waves:** a device uses its new routes once its wave is applied, and its old routes until then.
  - Going down, every device already has its post-failure groups (the local prune is immediate).
  - Going up, the restored lanes join a device's groups only when its wave is applied.
- In steady state both topologies have 0 looping and 0 blackholed pairs.

### G1: topology 1, 2+2, no OCS. `l1-1536.m0` fails at t = 0; repair at t = 10 s

- **Failed lanes:** `l1-1536.p12.0`, `.p13.0`, `.p14.0` and `.p15.0`, one lane of each L2 uplink.
- **Local prune** changes 8 group entries on 5 devices:
  - `l1-1536` → `l2-2304` … `l2-2307`: `[pK.0, pK.1]` → `[pK.1]` (K = 12…15);
  - `l2-2304` … `l2-2307` → `l1-1536`: `[p0.0, p0.1]` → `[p0.1]`.
- **Routes:** no neighbor is lost, so there are 0 route changes and 0 device writes until the repair.
- **Capacity:** domain `l1-1536.d0` 0.5; pairs `l1-1536`–`l2-2304` … `l2-2307` 0.5 each; device `l1-1536` 0.875 (28 of 32 lanes).
- **Incident:** 5 LANE_DOWN reports (1 MODULE_FAULT from `l1-1536`, 4 LOSS_OF_LIGHT) become 1 incident with failed device `l1-1536`. With no OCS it becomes `DEGRADED_WAITING_REPAIR` at 2 ms.
- **Repair at 10,000 ms:** light at 10,050, HELLO at 10,052, errors clean at 10,062, 5 LANE_UP at the controller at 10,063, one GROUP_SET wave to the 5 devices acknowledged at 10,066. Capacity is back to 1.0 and the incident is `CLOSED`.
- **Messages: 20** = 5 LANE_DOWN + 5 LANE_UP + 5 GROUP_SET + 5 ACK.

### G2: topology 1, 2:1, no OCS. `l1-1536.m0` fails at t = 0; repair at t = 10 s

- **Failed lanes:** `l1-1536.p12.0`, `.p12.1`, `.p13.0` and `.p13.1`. The links to `l2-2304` and `l2-2305` go fully down.
- **Local prune:** 4 groups become empty (`l1-1536` → `l2-2304` and `l2-2305`, and back), so 2 neighbors are lost.
- **Reports:** 3 LANE_DOWN (MODULE_FAULT from `l1-1536`; LOSS_OF_LIGHT from `l2-2304` and `l2-2305`) become 1 incident with failed device `l1-1536`.
- **Route diff: 1,024 entries on 66 devices** (64 L1 unions and 2 L2 switches):
  - **`l1-1536`:** 504 entries, one for every NPU outside board 0. Its L2 next hops shrink from 4 to 2. Example, `npu-64`: {`l2-2304` … `l2-2307`} → {`l2-2306`, `l2-2307`}.
  - **The 63 other left unions of plane 0:** 7 in rack 0 (`l1-1538` … `l1-1550`) and all 56 in racks 1–7. Each has 8 entries, for `npu-0` … `npu-7`, and drops `l2-2304` and `l2-2305`.
  - **`l2-2304` and `l2-2305`:** 8 entries each, for `npu-0` … `npu-7`: {`l1-1536`} (2 hops) → the 63 other left unions of plane 0 (4 hops).
- **Waves going down:** wave 1 = the 63 transit unions; wave 2 = {`l1-1536`, `l2-2304`, `l2-2305`}.

| State going down | Looping pairs | Blackholed pairs |
|---|---|---|
| Before the controller acts (local prune only) | 0 | 4,032 |
| After wave 1 | 0 | 0 |
| After wave 2 | 0 | 0 |
| Wrong order (endpoints first) | 4,032 | 0 |

| Timeline going down (ms) | Typical | Worst |
|---|---|---|
| LANE_DOWN at the controller | 1 | 5 |
| Plan done | 2 | 10 |
| Compute done | 3 | 15 |
| Wave 1 acknowledged (the drops end) | 6 | 25 |
| Wave 2 acknowledged → `DEGRADED_WAITING_REPAIR` | 9 | 35 |

- **Repair at 10 s:** 3 LANE_UP at 10,063; compute done at 10,064; wave 1 = the 3 endpoints (batched group + route) acknowledged at 10,067; wave 2 = the 63 transit unions acknowledged at 10,070. The final tables equal the initial tables. Pair counts are 0 in every restore state; restoring transit first gives 4,032 looping pairs.
- **Messages: 270** = 3 LANE_DOWN + 66 ROUTE_SET + 66 ACK + 3 LANE_UP + 66 (3 batched + 63 ROUTE_SET) + 66 ACK.

### G3: wrong-order guard (unit test)

Feed `order_waves()` output in reverse into `check_state()`. The expected looping pairs after the first (wrong) wave are:
- G2 going down: 4,032; G2 restore: 4,032;
- G6 going down: 338; G6 restore: 338.

### G4: topology 2, OCS on, one spare module per NPU domain. `npu-0.m0` fails at t = 0

There are two runs: (a) with 2+2 and (b) with 2:1.

| | (a) 2+2 | (b) 2:1 |
|---|---|---|
| Dead lanes | `npu-0.p13.0`, `npu-0.p14.0` | `npu-0.p13.0`, `npu-0.p13.1` |
| Groups after the local prune | `npu-0` → `npu-64`: `npu-0.p13.1`, `npu-0.p14.1`. `npu-64` → `npu-0`: `npu-64.p13.1`, `npu-64.p14.1` | `npu-0` → `npu-64`: `npu-0.p14.0`, `npu-0.p14.1`. `npu-64` → `npu-0`: `npu-64.p14.0`, `npu-64.p14.1` |
| OCS_SET disconnect | N1–S1, N3–S3 | N1–S1, N2–S2 |
| OCS_SET connect | N257–S1, N258–S3 | N257–S1, N258–S2 |
| GROUP_SET `npu-0` → `npu-64` | `npu-0.p13.1`, `npu-0.p14.1`, `npu-0.p15.0`, `npu-0.p15.1` | `npu-0.p14.0`, `npu-0.p14.1`, `npu-0.p15.0`, `npu-0.p15.1` |
| GROUP_SET `npu-64` → `npu-0` | `npu-64.p13.0`, `npu-64.p13.1`, `npu-64.p14.0`, `npu-64.p14.1` | Same |

**Both runs:**
- 2 group entries on 2 devices are pruned, and there are 0 route changes.
- 2 LANE_DOWN (MODULE_FAULT from `npu-0`, LOSS_OF_LIGHT from `npu-64`) become 1 incident. The failed device is `npu-0`, the spare is `npu-0.m2` (lanes `npu-0.p15.0` and `.p15.1`, on N257 and N258), and the targets are {`npu-0`, `npu-64`}.
- **Capacity:** before activation, pair `npu-0`/`npu-64` and domain `npu-0.d0` are at 0.5 and device `npu-0` at 0.933333 (28 of 30 lanes). After activation all three are 1.0, and the incident ends `CLOSED`.

| Timeline (ms) | Typical | Worst |
|---|---|---|
| LANE_DOWN at the controller | 1 | 5 |
| Plan done | 2 | 10 |
| PREPARE acknowledged | 5 | 20 |
| OCS applied | 7 | 60 |
| OCS_DONE | 32 | 260 |
| Light | 82 | 2,760 |
| HELLO ok | 84 | 2,960 |
| Errors clean | 94 | 4,960 |
| LANE_UP at the controller | 95 | 4,965 |
| Groups acknowledged: the pair is back to 1.0 | **98** | **4,975** |

- **Messages: 14** = 2 LANE_DOWN + 2 PREPARE + 2 ACK + 1 OCS_SET + 1 OCS_DONE + 2 LANE_UP + 2 GROUP_SET + 2 ACK.
- **Final state:** `npu-0.m0` is `FAILED`; the spare pool no longer contains `npu-0.m2`; the matrix read-back equals the expected matrix.

### G5: topology 2, 2+2, OCS on, no spare (the script default). `npu-0.m0` fails

- The local prune is the same as in G4a.
- The plan finds no spare (`NoRepair("no_spare")`), so the incident becomes `DEGRADED_NO_SPARE` at 2 ms and no OCS_SET is sent. The pair stays at 0.5.
- **Messages: 2** (LANE_DOWN).

### G6: topology 2, 2+2, OCS off. `npu-0.m0` and `npu-0.m1` fail at t = 0; repair at t = 10 s

- **Neighbor lost:** all 4 lanes between `npu-0` and `npu-64` are dead, so the neighbor is lost in both directions. 2:1 gives the same result, because both modules are dead either way.
- **Reports:** 2 LANE_DOWN (MODULE_FAULT from `npu-0` naming both modules, LOSS_OF_LIGHT from `npu-64`) become 1 incident.
- **Route diff: 740 entries on 208 devices:** all 128 NPUs carry 548 entries, 16 unions carry 128, and all 64 EXT carry 64. Example: `npu-0` → `npu-8` goes from {`npu-64`} (3 hops) to {`l1-408`, `l1-409`, `l1-432`, `l1-433`, `l1-456`, `l1-457`} (4 hops).
- **Waves going down:** 206 transit devices, then {`npu-0`, `npu-64`}.

| State going down | Looping pairs | Blackholed pairs |
|---|---|---|
| Before the controller acts | 0 | 450 |
| After wave 1 | 0 | 30 |
| After wave 2 | 0 | 0 |
| Wrong order (endpoints first) | 338 | 0 |

- After wave 1, the 30 remaining pairs are the own traffic of `npu-0` and `npu-64` to destinations they could reach only over the lost link (Section 7.4).
- **Timeline going down:** LANE_DOWN 1, plan 2, compute 3, wave 1 acknowledged 6, wave 2 acknowledged 9 → `DEGRADED_WAITING_REPAIR`. Worst profile: 5, 10, 15, 25, 35.
- **Repair:** 2 LANE_UP at 10,063; compute done at 10,064; the endpoints are acknowledged at 10,067 and transit at 10,070. The final tables equal the initial tables exactly. Pair counts are 0 in every restore state; restoring transit first gives 338 looping pairs.
- **Messages: 836** = 2 LANE_DOWN + 208 ROUTE_SET + 208 ACK + 2 LANE_UP + 208 (2 batched + 206 ROUTE_SET) + 208 ACK.
- **G6b, the same failure with the OCS on and one spare:** the spare's 2 lanes cannot cover the 4 dead lanes, so `plan_repair()` returns `NoRepair("not_enough_spare")`. The reroute is identical, the incident becomes `DEGRADED_NO_SPARE` at 9 ms, and no PREPARE or OCS_SET is sent. **Messages: 418** = 2 LANE_DOWN + 208 ROUTE_SET + 208 ACK.

### G7: the OCS never answers (G4a with the `ocs_never_answers` fault)

1. OCS_SET goes out at 2 ms, and no OCS_DONE comes back.
2. At 1,002 the read-back shows nothing applied, so the scheduler resends OCS_SET.
3. At 2,002 nothing is applied still, so the incident ends `FAILED_NEEDS_OPERATOR`, with the reason in the trace. Nothing is rolled back in v1.

- The pair stays at 0.5 throughout.
- **Messages: 8** = 2 LANE_DOWN + 2 PREPARE + 2 ACK + 2 OCS_SET.

### G8: one ACK lost (G4a, with `npu-64`'s first GROUP_SET ACK dropped)

1. GROUP_SET goes to both devices at 95.
2. `npu-0`'s ACK arrives at 98. Both devices have applied the change by 98, so the pair is at 1.0 from then, measured on the devices.
3. At 145 the scheduler resends `npu-64`'s full tables as one batched command.
4. The resend is acknowledged at 148, and the incident is `CLOSED` at 148.

- The trace shows exactly one resend.
- **Messages: 16.** The final state equals G4a.

## 12. Safety rules (enforce them as assertions and test them)

1. Only ACTIVE lanes are in groups, and only verified lanes become ACTIVE.
2. Next-hop changes go in ordered waves (Section 7.4). Group-only changes go in one wave.
3. Versions only go up, and a device ignores older versions.
4. One OCS command at a time. A repair never touches a working cross-connect.
5. The scheduler is the only sender. The Topology Manager, the Routing Engine and the OCS controller never call an adapter.
6. No controller action drops capacity below what local protection already provides.
7. In every golden scenario, `check_state` reports 0 looping pairs in every state that the correct wave order produces.
8. At most one outstanding command per device.

## 13. Later (not in this version)

Design the interfaces so each of these plugs in later without changing the logic of the four modules:

| Area | Later additions |
|---|---|
| SDN Scheduler | Rollback when the OCS or verification fails; several incidents in parallel; loop-free updates inside a wave (a prune step, then a full step) |
| Topology Manager | Flap hold-down for lanes that fail twice in 60 s; splitting state between rack and pod controllers |
| Routing Engine | Precomputed loop-free backup next hops (LFA); incremental or precomputed recompute; weighted ECMP for half-capacity links; routing policies (keep in-rack traffic in the rack, plane-aware paths) |
| OCS Controller | Partial restore when spare lanes run short; suspect spares and retry on another spare; vendor OCS adapter |
| Simulator | A traffic load model (lane loads, dropped traffic); the ns-3 adapter |
| Platform | Rack/pod controller hierarchy, standby controller, real UBM adapters, gRPC, gNMI, P4Runtime, real hardware |

## 14. Start now

1. Do Phase 0, then Phase 1 (Section 9).
2. Run `scripts/check.sh` and show me the output.
3. Report back with: a file tree, the key public types, the test results, the decisions you made and your questions for me.

Then wait.

## Appendix A: topology 1 script (Huawei 910D design), verbatim

Save it as `reference/topologies/topo1_910d.py` if it is not there already.

```python
import argparse
import os
import net_sim_builder as netsim
import networkx as nx
from PHY_model import link_delay_ns, node_forwarding_delay_ns

"""
In this topology design, the switching capability of the NPUs 
is modeled as separate switches connected to the compute chips 
at very high speeds which the most accurate way of modelling

The david indices within the NPU are arranged along the 
x-axis numbers, meaning davids within one NPU will have
adjacent indices.

Also the switches indices belonging to the same rack but in 
different planes are adjacent. So it's like this: 
[plane0_indices:plane1_indices:plane2_indices:plane3_indices]

Speeds / latencies:
  compute_to_leaf (LRS) :  400 Gbps, 20 ns
  mesh_row        :  400 Gbps, 3 ns
  mesh_col        :  400 Gbps, 3 ns
  leaf_to_spine (LRS)   :  400 Gbps, 10 ns
  spine_to_t (HRS)      :  400 Gbps, 100 ns

ID layout (all contiguous, auto-derived):
  Computes (david dies):  0 .. TOTAL_COMPUTES-1
  David switches: GRID_START .. GRID_START+TOTAL_COMPUTES-1
  Leaf/Spine:     SW_BASE .. SW_BASE + NUM_RACKS*PLANES*16 - 1
  HRS switches:     HRS_BASE .. HRS_BASE + 255          (exactly 256 IDs)
"""

# ---------------- Params ----------------
NUM_RACKS       = 8
BOARDS_PER_RACK   = 8
DAVIDS_PER_BOARD  = 8
IO_PER_COMPUTE = 2 # don't go beyond 2
NUM_PLANES           = 4 # These are the back-plane switches as explained in sec. 3.3 of the UB-Mesh paper
L1_UNIONS_PER_PLANE = 16
EXT_UNIONS_PER_PLANE = 8 # edges for each EXT = 8 // EXT_UNIONS_PER_PLANE
HRS_PER_PLANE        = 8  # edges for each HRS = 8 // HRS_PER_PLANE
NUM_SWITCH_DOMAINS   = 1
NUM_RACKS_PER_SWITCH_DOMAIN = 8
# ----------------------------------------

SPEED_LAT = {
    "io_switch":        ('10000Gbps', '0ns'),   # adding this to avoid host-multi switch routing issue
    "compute_to_grid":  ('5600Gbps', '1ns'),
    "compute_to_leaf":  ('400Gbps', f"{int(link_delay_ns('passive_copper', 2))}ns"),
    "d2d":              ('400Gbps', f"{int(link_delay_ns('passive_copper', 0))}ns"),
    "leaf_to_ext":      ('400Gbps', f"{int(link_delay_ns('passive_copper', 2))}ns"),
        # According to the UB-Mesh paper, these interconnects are active 
        # electrical interconnects which incur a latency of 100 - 200ns due to 
        # CDR (Clock and Data Recovery) mechanisms
    "spine_to_hrs":     ('400Gbps', f"{int(link_delay_ns('active_copper', 50))}ns"),   
    "hrs_to_oxc":       ('400Gbps', f"{int(link_delay_ns('optical', 50))}ns"),
}

# --------- Derived sizes & ID bases ---------
TOTAL_COMPUTES      = NUM_RACKS * BOARDS_PER_RACK * DAVIDS_PER_BOARD
COMPUTE_PER_RACK    = BOARDS_PER_RACK * DAVIDS_PER_BOARD
SW_PER_PLANE        = L1_UNIONS_PER_PLANE + EXT_UNIONS_PER_PLANE
SWITCHES_PER_RACK   = NUM_PLANES * SW_PER_PLANE              # 64 (L1) + 16 (EXT) per rack
TOTAL_BOARDS          = NUM_RACKS * BOARDS_PER_RACK

# GRID_SHIFT      = TOTAL_COMPUTES if IO_PER_COMPUTE > 1 else 0 # adding this to avoid host-multi switch routing issue
GRID_SHIFT      = 0 
GRID_START      = TOTAL_COMPUTES + GRID_SHIFT # On-chip IO is modeled as separate from the compute w/ very high-speed connectivity 
SW_BASE         = GRID_START + TOTAL_COMPUTES * IO_PER_COMPUTE
HRS_START       = SW_BASE + NUM_RACKS * SWITCHES_PER_RACK    # starts right after leaf/spine
NUM_HRS         = NUM_SWITCH_DOMAINS * NUM_PLANES * HRS_PER_PLANE
#NUM_HRS         = ((NUM_RACKS-1)//BOARDS_PER_RACK)*HRS_PER_RACK + HRS_PER_RACK # per swicthing domain (8 racks)
OXC_START       = HRS_START + NUM_HRS
NUM_OXC         = NUM_RACKS

# --------------------------------------------
# -------- For plane-aware routing -----------
intra_rack_switches = set(range(SW_BASE, HRS_START))
host_ids = set(range(TOTAL_COMPUTES)) 
hrs_switches = set(range(HRS_START, HRS_START + NUM_HRS))
io_switches = set(range(GRID_START, SW_BASE))
inside = intra_rack_switches | host_ids | io_switches
outside = hrs_switches
# ------------------------------------------------

def all_simple_paths(G, source, target):
    try:
        # 这里你可以在networkx库中寻找适合的寻路函数。
        # 调用networkx库的all_simple_paths函数，可以获得跳数<=cutoff值的所有不成环路径。
        paths = nx.all_simple_paths(G, source, target, cutoff=2)
    except nx.NetworkXNoPath:
        paths = []
    return paths

def all_shortest_paths(G, source, target):
    try:
        # 这里你可以在networkx库中寻找适合的寻路函数。
        # 调用networkx库的all_shortest_path函数，可以获得所有最短路径。
        paths = nx.all_shortest_paths(G, source, target)
    except nx.NetworkXNoPath:
        paths = []
    return paths

def rack_of_david(d): return d // COMPUTE_PER_RACK

def npu_idx_of_david_within_rack(d): return (d % COMPUTE_PER_RACK) // (DAVIDS_PER_BOARD)

def path_finder(graph, source, dest):
    same_rack = rack_of_david(source) == rack_of_david(dest)
    if not same_rack: 
        allowed_nodes = inside | outside
    else: 
        allowed_nodes = inside
    sub = graph.subgraph(allowed_nodes)
    return nx.all_shortest_paths(sub, source, dest)

def case_output_dir(dir_name):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    scratch_dir = os.path.abspath(os.path.join(script_dir, '..', '..'))
    return os.path.join(scratch_dir, 'cases', dir_name)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Generate topology into a case directory.")
    parser.add_argument('dir_name', help='Case directory name; artifacts are written to scratch/cases/<dir-name>')
    parsed = parser.parse_args()

    graph = netsim.NetworkSimulationGraph()
    graph.output_dir = case_output_dir(parsed.dir_name) + os.sep

    leaf_ids = []
    spine_ids = []
    # step1: Add the davids
    for compute_id in range(TOTAL_COMPUTES):
        graph.add_netisim_host(compute_id, forward_delay='1ns')

    # step1.1: add io routers (to avoid routing issue with host-mult switches)
    for io_sw_id in range(TOTAL_COMPUTES, TOTAL_COMPUTES + GRID_SHIFT):
        graph.add_netisim_node(io_sw_id, forward_delay='0ns')

    # step2: Add the grid switches (on-chip IO/controllers)
    for grid_id in range(GRID_START, SW_BASE):
        graph.add_netisim_node(grid_id, forward_delay=f"{int(node_forwarding_delay_ns('io_switch'))}ns") # Data came from Haitao in one of our meetings

    # step3: Add the SFU switches (normal + extension unions)
    for sw_id in range(SW_BASE, SW_BASE + NUM_RACKS * SWITCHES_PER_RACK):
        graph.add_netisim_node(sw_id, forward_delay=f"{int(node_forwarding_delay_ns('mid_latency_UB'))}ns") # Data came from Haitao in one of our meetings

    # step4: Add the 5808 HRS switches
    for sw_id in range(HRS_START, HRS_START + NUM_HRS):
        graph.add_netisim_node(sw_id, forward_delay=f"{int(node_forwarding_delay_ns('broadcom_tomahawk'))}ns")

    # step5: Add the OXC switches
    #for sw_id in range(OXC_START, OXC_START + NUM_OXC):
    #    graph.add_netisim_node(sw_id, forward_delay='10ns')
    
    # Step6: The NPU connections to on-chip IO controller
    for compute_id in range(TOTAL_COMPUTES, TOTAL_COMPUTES + GRID_SHIFT):
        graph.add_netisim_edge(compute_id - TOTAL_COMPUTES, compute_id - TOTAL_COMPUTES + GRID_SHIFT, bandwidth=SPEED_LAT['io_switch'][0], delay=SPEED_LAT['io_switch'][1], edge_count=1)

    for compute_id in range(TOTAL_COMPUTES):
        for iodie_id in range(IO_PER_COMPUTE):
            graph.add_netisim_edge(compute_id + GRID_SHIFT, compute_id + GRID_START + (TOTAL_COMPUTES*iodie_id), bandwidth=SPEED_LAT['compute_to_grid'][0], delay=SPEED_LAT['compute_to_grid'][1], edge_count=1)

    # step7: intra-BOARD d2d connections
    for board_idx in range(TOTAL_BOARDS):
        npu_start_idx = board_idx * DAVIDS_PER_BOARD
        npu_end_idx = npu_start_idx + DAVIDS_PER_BOARD
        io_idx = TOTAL_COMPUTES if IO_PER_COMPUTE > 1 else 0

        for i in range(npu_start_idx, npu_end_idx):
            for j in range(i+1, npu_end_idx):
                # Always connect board A
                graph.add_netisim_edge(i+GRID_START, j+GRID_START, bandwidth=SPEED_LAT['d2d'][0], delay=SPEED_LAT['d2d'][1], edge_count=1)
  
    # step8: NPU to L1 flat union layer
    for n in range(TOTAL_BOARDS):
        davids = list(range(n*DAVIDS_PER_BOARD, (n+1)* DAVIDS_PER_BOARD))
        # ios = [d + GRID_START for d in davids]
        io_groups = []
        for iodie_id in range(IO_PER_COMPUTE):
            io_group = [d + GRID_START + (TOTAL_COMPUTES*iodie_id) for d in davids]
            io_groups.append(io_group)

        ios_A = io_groups[0] # 2 out of frame ports for D1-D4, 6 out of frame ports for D5-D8
        if len(io_groups) == 2:
            ios_B = io_groups[1] # 6 out of frame ports ... then 2
        else:
            ios_B = ios_A

        rack_idx = n // BOARDS_PER_RACK
        rack_sw_base = SW_BASE + rack_idx * SWITCHES_PER_RACK
        for p in range(NUM_PLANES):
            l1_union_base = p * SW_PER_PLANE + rack_sw_base
            npu_idx_within_rack = (n % BOARDS_PER_RACK)
            left_plane_union = 2*npu_idx_within_rack + l1_union_base
            right_plane_union = 2*npu_idx_within_rack +1 + l1_union_base
            # The iodieA is the detour die. The out-of-rack switch connecting to iodieA will be the detour switch for that NPU
            # We want different NPUs in a board to have different detour switches so we don't have one single point of failure. 
            # That's why the iodieA is connected to the L1 switches in a round-robin fashion
            for io_idx in range(len(ios_A)): 
                if p == io_idx % NUM_PLANES:
                    graph.add_netisim_edge(ios_A[io_idx], left_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                    graph.add_netisim_edge(ios_A[io_idx], right_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                else:
                    graph.add_netisim_edge(ios_B[io_idx], left_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                    graph.add_netisim_edge(ios_B[io_idx], right_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)

    # step9: L1 unions <--> extension unions
    for rack in range(NUM_RACKS):
        rack_sw_base = SW_BASE + rack * SWITCHES_PER_RACK
        for plane in range(NUM_PLANES):
            plane_sw_base = rack_sw_base + plane * SW_PER_PLANE
            for u_pair in range(L1_UNIONS_PER_PLANE//2):
                left_plane_union = u_pair*2 + plane_sw_base
                right_plane_union = u_pair*2 + 1 + plane_sw_base
                for ext_pair in range(EXT_UNIONS_PER_PLANE//2):
                    left_plane_ext = ext_pair + plane_sw_base + L1_UNIONS_PER_PLANE
                    right_plane_ext = ext_pair + EXT_UNIONS_PER_PLANE//2 + plane_sw_base + L1_UNIONS_PER_PLANE
                    graph.add_netisim_edge(left_plane_union, left_plane_ext, bandwidth=SPEED_LAT['leaf_to_ext'][0], delay=SPEED_LAT['leaf_to_ext'][1], edge_count=1)
                    graph.add_netisim_edge(right_plane_union, right_plane_ext, bandwidth=SPEED_LAT['leaf_to_ext'][0], delay=SPEED_LAT['leaf_to_ext'][1], edge_count=1)

    # step10: L1 unions <--> 5808 HRS
    for rack in range(NUM_RACKS):
        rack_sw_base = SW_BASE + rack * SWITCHES_PER_RACK
        hrs_sw_base = HRS_START + (rack // NUM_RACKS_PER_SWITCH_DOMAIN) * NUM_PLANES * HRS_PER_PLANE
        for plane in range(NUM_PLANES):
            plane_sw_base = rack_sw_base + plane * SW_PER_PLANE
            plane_hrs_base =  hrs_sw_base + plane * HRS_PER_PLANE
            for u_pair in range(L1_UNIONS_PER_PLANE//2):
                left_plane_union = u_pair*2 + plane_sw_base
                right_plane_union = u_pair*2 + 1 + plane_sw_base
                for hrs_pair in range(HRS_PER_PLANE//2):
                    left_plane_hrs = hrs_pair + plane_hrs_base
                    right_plane_hrs = hrs_pair + HRS_PER_PLANE//2 + plane_hrs_base
                    graph.add_netisim_edge(left_plane_union, left_plane_hrs, bandwidth=SPEED_LAT['spine_to_hrs'][0], delay=SPEED_LAT['spine_to_hrs'][1], edge_count=1)
                    graph.add_netisim_edge(right_plane_union, right_plane_hrs, bandwidth=SPEED_LAT['spine_to_hrs'][0], delay=SPEED_LAT['spine_to_hrs'][1], edge_count=1)

    # step11: 5808 HRS <--> OXC
    '''
    for rack in range(NUM_RACKS):
        rack_hrs_base = HRS_START + (rack//BOARDS_PER_RACK)*HRS_PER_RACK
        for plane in range(NUM_PLANES):
            plane_hrs_base = rack_hrs_base + plane * HRS_PER_PLANE
            for o in range(NUM_OXC):
                oxc = OXC_START + o
                for hrs_pair in range(HRS_PER_PLANE//2):
                    left_plane_hrs = hrs_pair + plane_hrs_base
                    right_plane_hrs = hrs_pair + HRS_PER_PLANE//2 + plane_hrs_base
                    graph.add_netisim_edge(left_plane_hrs, oxc, bandwidth=SPEED_LAT['hrs_to_oxc'][0], delay=SPEED_LAT['hrs_to_oxc'][1], edge_count=1)
                    graph.add_netisim_edge(right_plane_hrs, oxc, bandwidth=SPEED_LAT['hrs_to_oxc'][0], delay=SPEED_LAT['hrs_to_oxc'][1], edge_count=1)
    '''
    # step3: 生成配置文件,build_graph_config会生成一系列中间数据,最终生成dcn2.0_config.xml文件
    graph.build_graph_config()
    # step3.2: gen_route_table 寻路并生成路由表
    # graph.gen_route_table(path_finding_algo=path_finder, multiple_workers=4)
    graph.gen_route_table(path_finding_algo=all_shortest_paths, multiple_workers=3)
    # step3.3: 配置 TP Channel，当前TP Channel的配置策略是基于路由表项，每一条表项对应一个路径，每个路径对应多个优先级
    graph.config_transport_channel(priority_list = [4,7])
    # step3.4: 写入所有配置文件
    graph.write_config()
```

## Appendix B: topology 2 script (OCS-based SURE design), verbatim

Save it as `reference/topologies/topo2_ocs_sure.py`. The only change from your paste is that two stray leading spaces before the first `import os` were removed, because Python cannot parse the file with them.

```python
import os
import net_sim_builder as netsim
import networkx as nx
from PHY_model import link_delay_ns, node_forwarding_delay_ns

"""
In this topology design, the switching capability of the NPUs 
is modeled as separate switches connected to the compute chips 
at very high speeds which the most accurate way of modelling

The david indices within the NPU are arranged along the 
x-axis numbers, meaning davids within one board will have
adjacent indices.

Also the switches indices belonging to the same rack but in 
different planes are adjacent. So it's like this: 
[plane0_indices:plane1_indices:plane2_indices:plane3_indices]

ID layout (all contiguous, auto-derived):
  Computes (david dies):  0 .. TOTAL_COMPUTES-1
  David switches: GRID_START .. GRID_START+TOTAL_COMPUTES-1
  Leaf/Spine:     SW_BASE .. SW_BASE + NUM_RACKS*PLANES*16 - 1
  HRS switches:     HRS_BASE .. HRS_BASE + 255          (exactly 256 IDs)
"""

"""
Two planes are removed to open up ports for the SURE connections
"""

# ---------------- Params ----------------
NUM_RACKS               = 2
BOARDS_PER_RACK         = 8
DAVIDS_PER_BOARD        = 8
IO_PER_COMPUTE          = 2 # don't go beyond 2
NUM_PLANES              = 4 # These are the back-plane switches as explained in sec. 3.3 of the UB-Mesh paper
L1_UNIONS_PER_PLANE     = 16
EXT_UNIONS_PER_PLANE    = 8 # edges for each EXT = 8 // EXT_UNIONS_PER_PLANE
HRS_PER_PLANE           = 8  # edges for each HRS = 8 // HRS_PER_PLANE
NUM_SWITCH_DOMAINS      = 1
NUM_SURE_OCS            = 1
SURE_MIRROR_PER_OCS     = 128
SURE_EPS_PER_RACK       = 0
NUM_SURE_BACKUP_OCS     = 0
SURE_MIRROR_PER_BACKUP_OCS = 0
NUM_RACKS_PER_SWITCH_DOMAIN = 8
# ----------------------------------------

SPEED_LAT = {
    "io_switch":        ('10000Gbps', '0ns'), # adding this to avoid host-multi switch routing issue
    "compute_to_grid":  ('5600Gbps', '1ns'),
    "compute_to_leaf":  ('400Gbps', f"{int(link_delay_ns('passive_copper', 2))}ns"),
    "d2d":              ('400Gbps', f"{int(link_delay_ns('passive_copper', 0))}ns"),
    "leaf_to_ext":      ('400Gbps', f"{int(link_delay_ns('passive_copper', 2))}ns"),
        # According to the UB-Mesh paper, these interconnects are active 
        # electrical interconnects which incur a latency of 100 - 200ns due to 
        # CDR (Clock and Data Recovery) mechanisms. Calc breakdowns in the function
    "spine_to_hrs":     ('400Gbps', f"{int(link_delay_ns('active_copper', 10))}ns"),
        # Even though the propagation delay of fiber is very low,
        # the processing required for the SerDes conversion is a fixed
        # 300ns delay
    "optical":     ('400Gbps', f"{int(link_delay_ns('optical', 10, 'lpo'))}ns")
}

# --------- Derived sizes & ID bases ---------
TOTAL_COMPUTES      = NUM_RACKS * BOARDS_PER_RACK * DAVIDS_PER_BOARD
COMPUTE_PER_RACK    = BOARDS_PER_RACK * DAVIDS_PER_BOARD
SW_PER_PLANE        = L1_UNIONS_PER_PLANE + EXT_UNIONS_PER_PLANE
SWITCHES_PER_RACK   = NUM_PLANES * SW_PER_PLANE              # 64 (L1) + 16 (EXT) per rack
TOTAL_BOARDS          = NUM_RACKS * BOARDS_PER_RACK

# GRID_SHIFT      = TOTAL_COMPUTES if IO_PER_COMPUTE > 1 else 0 # adding this to avoid host-multi switch routing issue
GRID_SHIFT      = 0 
GRID_START      = TOTAL_COMPUTES + GRID_SHIFT # On-chip IO is modeled as separate from the compute w/ very high-speed connectivity 
SW_BASE         = GRID_START + TOTAL_COMPUTES * IO_PER_COMPUTE
HRS_START       = SW_BASE + NUM_RACKS * SWITCHES_PER_RACK    # starts right after leaf/spine
NUM_HRS         = NUM_SWITCH_DOMAINS * NUM_PLANES * HRS_PER_PLANE
#NUM_HRS         = ((NUM_RACKS-1)//BOARDS_PER_RACK)*HRS_PER_RACK + HRS_PER_RACK # per swicthing domain (8 racks)
#OXC_START       = HRS_START + NUM_HRS
#NUM_OXC         = NUM_RACKS
SURE_EPS_START  = HRS_START + NUM_HRS
NUM_SURE_EPS    = SURE_EPS_PER_RACK * NUM_RACKS
SURE_OCS_START       = SURE_EPS_START + NUM_SURE_EPS
NUM_SURE_OCS_MIRRORS = NUM_SURE_OCS * SURE_MIRROR_PER_OCS
SURE_BACKUP_OCS_START = SURE_OCS_START + NUM_SURE_OCS_MIRRORS


# --------------------------------------------
# -------- For plane-aware routing -----------
intra_rack_switches = set(range(SW_BASE, HRS_START))
host_ids = set(range(TOTAL_COMPUTES)) 
hrs_switches = set(range(HRS_START, HRS_START + NUM_HRS))
io_switches = set(range(GRID_START, SW_BASE))
inside = intra_rack_switches | host_ids | io_switches
outside = hrs_switches
# ------------------------------------------------

def rack_of_david(d): return d // COMPUTE_PER_RACK

def npu_idx_of_david_within_rack(d): return (d % COMPUTE_PER_RACK) // (DAVIDS_PER_BOARD)


def all_simple_paths(G, source, target):
    try:
        # 这里你可以在networkx库中寻找适合的寻路函数。
        # 调用networkx库的all_simple_paths函数，可以获得跳数<=cutoff值的所有不成环路径。
        paths = nx.all_simple_paths(G, source, target, cutoff=2)
    except nx.NetworkXNoPath:
        paths = []
    return paths

def all_shortest_paths(G, source, target):
    try:
        # 这里你可以在networkx库中寻找适合的寻路函数。
        # 调用networkx库的all_shortest_path函数，可以获得所有最短路径。
        paths = nx.all_shortest_paths(G, source, target)
    except nx.NetworkXNoPath:
        paths = []
    return paths

def scaleup_paths(graph, source, dest):
    allowed_nodes = inside
    '''
    same_rack = rack_of_david(source) == rack_of_david(dest)
    if not same_rack:
        # We are forcing inter-rack traffic in SURE to go through the 
        # intra-rack scale up connections and then through the opticals
        allowed_nodes = inside
    else:
        allowed_nodes = inside | outside
    '''
    sub = graph.subgraph(allowed_nodes)
    return nx.all_shortest_paths(sub, source, dest)



if __name__ == '__main__':
    graph = netsim.NetworkSimulationGraph()

    leaf_ids = []
    spine_ids = []
    # step1: Add the davids
    for compute_id in range(TOTAL_COMPUTES):
        graph.add_netisim_host(compute_id, forward_delay='1ns')

    # step1.1: add io routers (to avoid routing issue with host-mult switches)
    for io_sw_id in range(TOTAL_COMPUTES, TOTAL_COMPUTES + GRID_SHIFT):
        graph.add_netisim_node(io_sw_id, forward_delay='0ns')

    # step2: Add the grid switches (on-chip IO/controllers)
    for grid_id in range(GRID_START, SW_BASE):
        graph.add_netisim_node(grid_id, forward_delay=f"{int(node_forwarding_delay_ns('low_latency_UB'))}ns") # Data came from Haitao in one of our meetings

    # step3: Add the SFU switches (normal + extension unions)
    for sw_id in range(SW_BASE, SW_BASE + NUM_RACKS * SWITCHES_PER_RACK):
        graph.add_netisim_node(sw_id, forward_delay=f"{int(node_forwarding_delay_ns('low_latency_UB'))}ns") # Data came from Haitao in one of our meetings

    # step4: Add the 5808 HRS switches
    for sw_id in range(HRS_START, HRS_START + NUM_HRS):
        graph.add_netisim_node(sw_id, forward_delay=f"{int(node_forwarding_delay_ns('broadcom_tomahawk'))}ns") # 30ns due to the switch SerDes

    for sw_id in range(SURE_EPS_START, SURE_EPS_START + NUM_SURE_EPS):
        graph.add_netisim_node(sw_id, forward_delay=f"{int(node_forwarding_delay_ns('low_latency_UB'))}ns")

    # Step6: The NPU connections to on-chip IO controller
    for compute_id in range(TOTAL_COMPUTES, TOTAL_COMPUTES + GRID_SHIFT):
        graph.add_netisim_edge(compute_id - TOTAL_COMPUTES, compute_id - TOTAL_COMPUTES + GRID_SHIFT, bandwidth=SPEED_LAT['io_switch'][0], delay=SPEED_LAT['io_switch'][1], edge_count=1)

    for compute_id in range(TOTAL_COMPUTES):
        for iodie_id in range(IO_PER_COMPUTE):
            graph.add_netisim_edge(compute_id + GRID_SHIFT, compute_id + GRID_START + (TOTAL_COMPUTES*iodie_id), bandwidth=SPEED_LAT['compute_to_grid'][0], delay=SPEED_LAT['compute_to_grid'][1], edge_count=1)

    # step7: intra-BOARD d2d connections
    for board_idx in range(TOTAL_BOARDS):
        npu_start_idx = board_idx * DAVIDS_PER_BOARD
        npu_end_idx = npu_start_idx + DAVIDS_PER_BOARD
        io_idx = TOTAL_COMPUTES if IO_PER_COMPUTE > 1 else 0
        for i in range(npu_start_idx, npu_end_idx):
            for j in range(i+1, npu_end_idx):
                # Always connect board A
                graph.add_netisim_edge(i+GRID_START, j+GRID_START, bandwidth=SPEED_LAT['d2d'][0], delay=SPEED_LAT['d2d'][1], edge_count=1)
  
    # step8: NPU to L1 flat union layer
    for n in range(TOTAL_BOARDS):
        davids = list(range(n*DAVIDS_PER_BOARD, (n+1)* DAVIDS_PER_BOARD))
        # ios = [d + GRID_START for d in davids]
        io_groups = []
        for iodie_id in range(IO_PER_COMPUTE):
            io_group = [d + GRID_START + (TOTAL_COMPUTES*iodie_id) for d in davids]
            io_groups.append(io_group)

        ios_A = io_groups[0] # 2 out of frame ports for D1-D4, 6 out of frame ports for D5-D8
        if len(io_groups) == 2:
            ios_B = io_groups[1] # 6 out of frame ports ... then 2
        else:
            ios_B = ios_A

        rack_idx = n // BOARDS_PER_RACK
        rack_sw_base = SW_BASE + rack_idx * SWITCHES_PER_RACK
        for p in range(NUM_PLANES):
            l1_union_base = p * SW_PER_PLANE + rack_sw_base
            npu_idx_within_rack = (n % BOARDS_PER_RACK)
            left_plane_union = 2*npu_idx_within_rack + l1_union_base
            right_plane_union = 2*npu_idx_within_rack +1 + l1_union_base
            # The iodieA is the detour die. The out-of-rack switch connecting to iodieA will be the detour switch for that NPU
            # We want different NPUs in a board to have different detour switches so we don't have one single point of failure. 
            # That's why the iodieA is connected to the L1 switches in a round-robin fashion
            for io_idx in range(len(ios_A)): 
                if p == io_idx % NUM_PLANES:
                    # Contribute both ports to optical
                    # graph.add_netisim_edge(ios_A[io_idx], left_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                    # graph.add_netisim_edge(ios_A[io_idx], right_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                    pass
                else:
                    # io_idx +1 is chosen arbitrarily. We just need one b die to contribute a port for optical
                    if p == (io_idx +1) % NUM_PLANES:
                        graph.add_netisim_edge(ios_B[io_idx], left_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                        graph.add_netisim_edge(ios_B[io_idx], right_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                    else:
                        graph.add_netisim_edge(ios_B[io_idx], left_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)
                        graph.add_netisim_edge(ios_B[io_idx], right_plane_union, bandwidth=SPEED_LAT['compute_to_leaf'][0], delay=SPEED_LAT['compute_to_leaf'][1], edge_count=1)

    # step9: L1 unions <--> extension unions
    for rack in range(NUM_RACKS):
        rack_sw_base = SW_BASE + rack * SWITCHES_PER_RACK
        for plane in range(NUM_PLANES):
            plane_sw_base = rack_sw_base + plane * SW_PER_PLANE
            for u_pair in range(L1_UNIONS_PER_PLANE//2):
                left_plane_union = u_pair*2 + plane_sw_base
                right_plane_union = u_pair*2 + 1 + plane_sw_base
                for ext_pair in range(EXT_UNIONS_PER_PLANE//2):
                    left_plane_ext = ext_pair + plane_sw_base + L1_UNIONS_PER_PLANE
                    right_plane_ext = ext_pair + EXT_UNIONS_PER_PLANE//2 + plane_sw_base + L1_UNIONS_PER_PLANE
                    graph.add_netisim_edge(left_plane_union, left_plane_ext, bandwidth=SPEED_LAT['leaf_to_ext'][0], delay=SPEED_LAT['leaf_to_ext'][1], edge_count=1)
                    graph.add_netisim_edge(right_plane_union, right_plane_ext, bandwidth=SPEED_LAT['leaf_to_ext'][0], delay=SPEED_LAT['leaf_to_ext'][1], edge_count=1)

    # step10: L1 unions <--> 5808 HRS
    for rack in range(NUM_RACKS):
        rack_sw_base = SW_BASE + rack * SWITCHES_PER_RACK
        hrs_sw_base = HRS_START + (rack // NUM_RACKS_PER_SWITCH_DOMAIN) * NUM_PLANES * HRS_PER_PLANE
        for plane in range(NUM_PLANES):
            plane_sw_base = rack_sw_base + plane * SW_PER_PLANE
            plane_hrs_base =  hrs_sw_base + plane * HRS_PER_PLANE
            for u_pair in range(L1_UNIONS_PER_PLANE//2):
                left_plane_union = u_pair*2 + plane_sw_base
                right_plane_union = u_pair*2 + 1 + plane_sw_base
                for hrs_pair in range(HRS_PER_PLANE//2):
                    left_plane_hrs = hrs_pair + plane_hrs_base
                    right_plane_hrs = hrs_pair + HRS_PER_PLANE//2 + plane_hrs_base
                    graph.add_netisim_edge(left_plane_union, left_plane_hrs, bandwidth=SPEED_LAT['spine_to_hrs'][0], delay=SPEED_LAT['spine_to_hrs'][1], edge_count=1)
                    graph.add_netisim_edge(right_plane_union, right_plane_hrs, bandwidth=SPEED_LAT['spine_to_hrs'][0], delay=SPEED_LAT['spine_to_hrs'][1], edge_count=1)

    # step11: SURE OCS Connections
    # For the 2 hop scenario, the first port that each NPU is contributing is just like the normal one and it's 
    # direct pairwise. But the second port they contribute is inter-board
    for david in range(COMPUTE_PER_RACK):
        rack1_david_dieA = david + GRID_START
        rack1_david_board_idx = rack1_david_dieA % DAVIDS_PER_BOARD
        rack1_david_board_id = david // DAVIDS_PER_BOARD
        rack2_david_board_id = rack1_david_board_idx
        rack2_david_board_idx = rack1_david_board_id
        rack2_david_dieA = GRID_START + COMPUTE_PER_RACK + rack2_david_board_id * DAVIDS_PER_BOARD + rack2_david_board_idx
        graph.add_netisim_edge(rack1_david_dieA, rack2_david_dieA, bandwidth=SPEED_LAT['optical'][0], delay=SPEED_LAT['optical'][1], edge_count=1)
        graph.add_netisim_edge(rack1_david_dieA, rack2_david_dieA, bandwidth=SPEED_LAT['optical'][0], delay=SPEED_LAT['optical'][1], edge_count=1)


    # step3: 生成配置文件,build_graph_config会生成一系列中间数据,最终生成dcn2.0_config.xml文件
    graph.build_graph_config()
    # step3.2: gen_route_table 寻路并生成路由表
    graph.gen_route_table(path_finding_algo=scaleup_paths, multiple_workers=4)
    #graph.gen_route_table(path_finding_algo=all_shortest_paths, multiple_workers=4)
    # step3.3: 配置 TP Channel，当前TP Channel的配置策略是基于路由表项，每一条表项对应一个路径，每个路径对应多个优先级
    graph.config_transport_channel(priority_list = [7])
    # step3.4: 写入所有配置文件
    graph.write_config()  
```
