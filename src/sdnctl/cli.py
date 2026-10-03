"""The sdnctl-sim command (SPEC 10, Phase 8): run scenarios and write a report for each run.

    sdnctl-sim --scenario PATH [PATH ...] [--topology NAME] [--config FILE] --out DIR

--scenario takes scenario files or folders; a folder means every *.json in it, by name.
--topology keeps only the scenarios on that topology. --config is a SPEC 9.5 config file merged
over the defaults: its timing values, timeouts and limits apply to every run, and a
timing_profile or feature flag it sets replaces the scenario files' own, with a warning on
stderr for each scenario setting it changes. Each run writes trace.jsonl, summary.json and
capacity.csv to DIR/<scenario name>/. DIR may not be in the reference folder (the one holding
ref_model.py).

Exit status: 0 when every chosen scenario ran, 1 when an input is wrong or a scenario could not
run, 2 for a bad command line.
"""

import argparse
import dataclasses
import json
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from sdnctl.config import ControllerConfig, load_config
from sdnctl.interfaces import IncidentOutcome
from sdnctl.report import REPORT_FILES, RunSummary, summarize, write_report
from sdnctl.scenario import Scenario, load_scenario, run_scenario
from sdnctl.topology.loader import TOPOLOGY_NAMES

PROG = "sdnctl-sim"
EXIT_OK = 0
EXIT_FAILED = 1
_FOLDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class RunConfig:
    """The --config file: the controller config, and the scenario settings it replaces."""

    config: ControllerConfig
    timing_profile: str | None  # None: each scenario's own
    features: Mapping[str, bool]  # only the flags the file sets

    def apply(self, scenario: Scenario) -> Scenario:
        """The scenario as it will run: with this file's timing profile and feature flags."""
        features = dataclasses.replace(scenario.features, **self.features)
        profile = self.timing_profile or scenario.timing_profile
        return dataclasses.replace(scenario, features=features, timing_profile=profile)

    def overrides(self, scenario: Scenario) -> list[tuple[str, str, str]]:
        """(setting, the scenario's value, this file's value) for each setting this changes."""
        changes: list[tuple[str, str, str]] = []
        if self.timing_profile is not None and self.timing_profile != scenario.timing_profile:
            changes.append(("timing_profile", scenario.timing_profile, self.timing_profile))
        for f in dataclasses.fields(scenario.features):
            own = getattr(scenario.features, f.name)
            if f.name in self.features and self.features[f.name] != own:
                changes.append(
                    (f"features.{f.name}", json.dumps(own), json.dumps(self.features[f.name]))
                )
        return changes


def read_run_config(path: str | Path | None) -> RunConfig:
    """Read --config (None: the SPEC 9.5 defaults, replacing nothing); a bad file raises."""
    if path is None:
        return RunConfig(load_config(None), None, {})
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("must be a JSON object")
        config = load_config(raw)
    except (OSError, ValueError) as e:
        raise ValueError(f"config {path}: {e}") from None
    features = {k: getattr(config.features, k) for k in raw.get("features", {})}
    profile = config.timing_profile if "timing_profile" in raw else None
    return RunConfig(config, profile, features)


def find_scenarios(paths: Sequence[str | Path]) -> list[Path]:
    """Scenario files from files and folders (a folder: its *.json, by name), each file once."""
    found: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files = sorted(p.glob("*.json"))
            if not files:
                raise ValueError(f"no *.json scenario in {p}")
            found.extend(files)
        elif p.is_file():
            found.append(p)
        else:
            raise ValueError(f"no such scenario file or folder: {p}")
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in found:
        if f.resolve() not in seen:
            seen.add(f.resolve())
            unique.append(f)
    return unique


def load_scenarios(files: Sequence[Path]) -> list[Scenario]:
    """Load every scenario file; a bad file, or two scenarios with one name, raises."""
    scenarios: list[Scenario] = []
    names: dict[str, Path] = {}
    for f in files:
        try:
            s = load_scenario(f)
        except (OSError, ValueError, TypeError) as e:
            raise ValueError(f"{f}: {e}") from None
        if not _FOLDER_NAME.fullmatch(s.name):
            raise ValueError(f"{f}: scenario name {s.name!r} is not a plain folder name")
        other = names.get(s.name.lower())  # folder names: Windows ignores case
        if other is not None:
            raise ValueError(f"{f}: scenario name {s.name!r} is also used by {other}")
        names[s.name.lower()] = f
        scenarios.append(s)
    return scenarios


def check_out_dir(out: Path) -> None:
    """Refuse an output folder that is, or is in, the reference folder (it holds ref_model.py)."""
    full = out.resolve()
    for d in (full, *full.parents):
        if (d / "ref_model.py").is_file():
            raise ValueError(f"--out must not point into the reference folder {d}")


def run_one(scenario: Scenario, run_config: RunConfig, out_dir: Path) -> RunSummary:
    """Run one scenario exactly as given and write its report to out_dir."""
    for name in REPORT_FILES:  # a run that fails must not leave an older report behind
        (out_dir / name).unlink(missing_ok=True)
    sim = run_scenario(scenario, config=run_config.config)
    report = sim.report()
    summary = summarize(scenario, run_config.config, report)
    write_report(out_dir, summary, report)
    return summary


def build_parser() -> argparse.ArgumentParser:
    """The sdnctl-sim command line."""
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Run SDN controller scenarios in the simulator and write their reports.",
    )
    p.add_argument("--scenario", nargs="+", required=True, metavar="PATH",
                   help="scenario files, or folders of them (*.json)")
    p.add_argument("--topology", choices=TOPOLOGY_NAMES,
                   help="run only the scenarios on this topology")
    p.add_argument("--config", metavar="FILE",
                   help="config JSON (SPEC 9.5) merged over the defaults; a timing_profile or "
                        "feature flag it sets replaces the scenarios' own")
    p.add_argument("--out", required=True, metavar="DIR",
                   help="output folder; each run writes DIR/<scenario name>/trace.jsonl, "
                        "summary.json and capacity.csv")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Run sdnctl-sim and return its exit status."""
    args = build_parser().parse_args(argv)
    out = Path(args.out)
    try:
        check_out_dir(out)
        run_config = read_run_config(args.config)
        scenarios = load_scenarios(find_scenarios(args.scenario))
    except ValueError as e:
        return _fail(str(e))
    chosen = [s for s in scenarios if args.topology in (None, s.topology)]
    if not chosen:
        return _fail(f"none of the scenarios runs on {args.topology}")
    failed = 0
    for s in chosen:
        for setting, own, new in run_config.overrides(s):
            print(f"{PROG}: warning: {s.name}: --config overrides the scenario's {setting} "
                  f"({own} -> {new})", file=sys.stderr)
        scenario = run_config.apply(s)
        start = time.perf_counter()
        try:
            summary = run_one(scenario, run_config, out / scenario.name)
        except ValueError as e:
            failed += 1
            print(f"{PROG}: {scenario.name}: {e}", file=sys.stderr)
            continue
        wall = time.perf_counter() - start
        print(f"{_describe(summary)}; {wall:.1f} s -> {out / scenario.name}", flush=True)
    skipped = len(scenarios) - len(chosen)
    print(f"{len(chosen) - failed} run, {failed} failed, {skipped} skipped (other topology)")
    return EXIT_FAILED if failed else EXIT_OK


def _describe(summary: RunSummary) -> str:
    ends = "; ".join(_outcome(o) for o in summary.incidents) or "no incident"
    return (
        f"{summary.scenario} ({summary.topology}, {summary.timing_profile}): {ends}; "
        f"{summary.messages_total} messages"
    )


def _outcome(o: IncidentOutcome) -> str:
    reason = f" ({o.reason})" if o.reason else ""
    if o.ended_at is None:
        return f"incident {o.incident} still {o.state.value}{reason}"
    return f"incident {o.incident} {o.state.value} at {_ms(o.ended_at)} ms{reason}"


def _ms(t_us: int) -> str:
    return str(t_us // 1000) if t_us % 1000 == 0 else f"{t_us / 1000:.3f}"


def _fail(message: str) -> int:
    print(f"{PROG}: error: {message}", file=sys.stderr)
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
