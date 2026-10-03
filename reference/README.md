# reference/ (cross-check only; do not edit)

- `ref_model.py`: builds topology 1 (Huawei 910D design) and topology 2 (OCS-based SURE design) from the two scripts in `topologies/`. It collapses each NPU into one node and adds ports, optical domains, modules, lanes and the OCS. Then it computes the v1 golden numbers in `docs/SPEC.md`: routes, local prunes, diffs, waves, looping and blackholed pairs, capacities, timelines and message counts.
- Run it with `python reference/ref_model.py --out build/ref_out`. It needs networkx, because the two scripts import it, and takes about 80 s. It writes `golden.json` into the `--out` folder; compare it with `reference/golden.json`.
- `--full` also writes the numbers for later additions (traffic loads, loop-free backups, rollback, partial restore) to `golden_full.json`. v1 does not use them.
- `topologies/`: the two topology scripts, unchanged except for two stray spaces before `import os` in topology 2.
- `stubs/`: stand-ins for `net_sim_builder` and `PHY_model`. They record the nodes and edges, so the scripts run without the simulator.

Code under `src/` must not import anything from this folder.
