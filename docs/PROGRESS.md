# Progress

| Phase | Status | Notes |
|---|---|---|
| 0: Repository skeleton | Done | The golden check passed: `build/ref_out/golden.json` equals `reference/golden.json` |
| 1: Types, messages, interfaces, config, builders, validator | Done | |
| 2: Simulator | Done | The exit check passes: G1's local prune |
| 3: Topology Manager | Done | The exit checks pass: G2's incident, G1's capacities |
| 4: Routing Engine | Done | The exit checks pass: Section 3 table numbers, G2, G3, G6; `compute()` on topology 1 takes about 1.7 s |
| 5: OCS Matrix Controller | Done | The exit checks pass: G4a/G4b OCS pairs, G5, G6b |
| 6: SdnController | Done | The exit check passes: steady state on both topologies |
| 7: SDN Scheduler | Done | The exit check passes: G1–G8 end to end |
| 8: CLI and report | Done | The exit check passes: `sdnctl-sim --scenario scenarios --out build/runs` runs all 9 scenarios in about 14 s; 266 tests in total (about 98 s); `scripts/check.sh` is clean |

## How to run

```
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"   # once; installs sdnctl-sim
bash scripts/check.sh                                            # ruff, mypy, pytest
.venv/Scripts/sdnctl-sim --scenario scenarios --out build/runs   # every scenario
.venv/Scripts/sdnctl-sim --scenario scenarios --topology topology2 --config worst.json --out build/worst
```

`worst.json` holds `{"timing_profile": "worst"}`. Each run writes `trace.jsonl`, `summary.json` and `capacity.csv` to `build/runs/<scenario>/`. `python -m sdnctl.cli` does the same as `sdnctl-sim`.

## Phase 8 contents

| File | What it is |
|---|---|
| `cli.py` | `sdnctl-sim`: flags (D66), config precedence (D67) with a warning per override (D73), input checks, exit status (D69), the `--out` guard (D70) |
| `report.py` | `RunSummary`, `CapacityRange`, `summarize`, and the writers for `trace.jsonl`, `summary.json` and `capacity.csv` (D68) |
| `scenario.py` | `start_scenario`/`run_scenario` take a config; `ScenarioEvent.as_dict`; malformed files raise `ValueError` |
| `sim/capacity.py` | Fix: samples now include changes at the far end of a lane (D72) |
| `pyproject.toml` | The `sdnctl-sim` console script |

## Phase 8 results

| Scenario | Console line (typical) | Golden |
|---|---|---|
| G1 | closed at 10,066 ms; 20 messages | 20 |
| G2 | closed at 10,070 ms; 270 messages | 270 |
| G4a, G4b | closed at 98 ms; 14 messages | 14, CLOSED |
| G5 | degraded_no_spare at 2 ms (no_spare); 2 messages | 2, DEGRADED_NO_SPARE |
| G6 | closed at 10,070 ms; 836 messages | 836 |
| G6b | degraded_no_spare at 9 ms (not_enough_spare); 418 messages | 418, DEGRADED_NO_SPARE |
| G7 | failed_needs_operator at 2,002 ms; 8 messages | 8, FAILED_NEEDS_OPERATOR |
| G8 | closed at 148 ms; 16 messages | 16 |

`tests/test_cli.py` checks:
- every summary against golden;
- the trace against the summary's message counts;
- the CSV against the summary's capacity ranges, and G4a/G4b's final capacities against golden;
- byte-identical output from two runs;
- the three kinds of config override: profile, feature flag, timing values;
- the topology filter;
- the `--out` guard;
- 10 bad inputs;
- a failing scenario among good ones;
- bad command lines;
- both entry points.

`tests/test_scenarios.py` now also checks that the capacity samples replay to the measured values in every scenario (D72).

## Phase 7 contents

| File | What it is |
|---|---|
| `scheduler/scheduler.py` | `Scheduler`: event loop, incident runner and phase machine (SPEC 7.2), waves (7.4), OCS steps, timeouts (7.3) |
| `scheduler/sender.py` | `CommandSender`: one outstanding command per device, ACK timeout, one resend |
| `scheduler/commands.py` | Command payloads from table changes, full-table resends, applying ACKed commands to the controller's copy |
| `scheduler/incident.py` | `IncidentRun`: one incident's plan, flags and timers |
| `scenario.py` | Scenario loader and runner (used by the tests now, and by the CLI in Phase 8) |
| `scenarios/*.json` | G1, G2, G4a, G4b, G5, G6, G6b, G7, G8 |

## Phase 7 results (every value from golden.json)

| Scenario | Checked |
|---|---|
| G1 | 8 prunes; G1 capacities at 5 ms; DEGRADED_WAITING_REPAIR at 2; repair 10,050 / 10,052 / 10,062 / 10,063; groups ACKed 10,066; CLOSED; 20 messages |
| G2 | Down and restore timelines, typical and worst; waves 63/3, then 3/63; 1,024 entries; pair counts on the simulated devices at every wave boundary (0/4,032 → 0/0 → 0/0; restore 0/0); messages per phase (270); waiting at 9; CLOSED; final tables equal initial |
| G4a, G4b | Full timeline, typical (98 ms) and worst (4,975 ms); 14 messages; capacities during and after; group lanes; `npu-0.m0` FAILED; spare out of the pool; matrix read-back |
| G5 | DEGRADED_NO_SPARE at 2; 2 messages; pair at 0.5 |
| G6 | Reroute timelines, typical and worst; repair timeline; pair counts at every wave boundary (0/450 → 0/30 → 0/0); 836 messages; final tables equal initial; the same with 2:1 |
| G6b | DEGRADED_NO_SPARE at 9; 418 messages; no PREPARE or OCS_SET |
| G7 | OCS_SET at 2 and 1,002; timeouts at 1,002 and 2,002; FAILED_NEEDS_OPERATOR at 2,002 with a reason; 8 messages; pair stays at 0.5 |
| G8 | GROUP_SET at 95; `npu-0`'s ACK at 98; devices at 1.0 from 98; timeout at 145; exactly one resend, ACKed at 148; CLOSED at 148; 16 messages; final state equals G4a |

## Safety rules (SPEC 12), as tests

| Rule | How it is tested |
|---|---|
| 1 | Devices reject unverified lanes (Phase 2); the Topology Manager raises on an unusable installed lane (Phase 3) |
| 2 | Wave order (Phase 4); every scenario's waves |
| 3 | Per device, versions never go down in any scenario |
| 4 | Repairs only disconnect the failed lanes' cross-connects; OCS_SETs never overlap |
| 5 | Code scan (Phase 6) |
| 6 | Pair capacity never below 0.5 in G4a, G4b, G7, G8 |
| 7 | 0 looping pairs at every wave boundary of G2 and G6, measured on the simulated devices |
| 8 | Per device, a command is sent only after the previous one was applied or timed out, in every scenario |

## Next

All phases of SPEC 10 are done. v1 is committed and tagged `v1` in git.

The answers of 2026-10-02 (`docs/DECISIONS.md`, "Answers received") change the model for v2: no lanes, 4-port NPU domains, and a new SURE pairing. v2 starts once these questions are answered:

1. **Golden numbers:** v2 removes the lane counts and lane lists in `golden.json`. It also changes topology 2's port counts, OCS ports and, with the new pairing, its hop counts and route entries. `golden.json` can't be edited here. Will the user update `ref_model.py` and `golden.json` for v2, or should the v2 values be worked out from the rules for the user to review?
2. **What the OCS restores under 2+2:** the reading of "replace completely" is that the spare module takes over every connection of the failed module, so every port is back to 400G, as in v1. Or should the OCS move the whole link to a different NPU or switch?
3. **Half-ports:** under 2+2, the OCS moves half of each port, so something smaller than a port still has to exist. Proposal: keep it hidden inside the model. Messages, groups, capacity and reports would talk only about ports (400G, or 200G when half-served) and 800G links, with groups weighted by port rate. Is that acceptable?
4. **Which links are 800G:** only the SURE NPU–NPU links, or every link? Today each script edge is one 400G port at each end. Making every link 800G doubles every port count, and an L1 domain's 4 ports would reach 2 L2 switches instead of 4.
5. **Where the NPU's extra optical ports come from:** the script frees 2 ports for optics by dropping the plane `d % 4`. Do NPUs get 2 more ports, or drop a second plane?
6. **SURE pairing:** the reading is board b ↔ board b + 8, with NPU i ↔ NPU i. That makes `npu-1` ↔ `npu-65` (`npu-72` in v1), with boards 0–7 on the OCS North side. Is that right?
7. **Item 9:** add a 10 ms `hello_interval` setting to the config? See "Answers received" for what it changes.

Section 3.6 items 1, 2, 3, 7 and 8 still have no answer.
