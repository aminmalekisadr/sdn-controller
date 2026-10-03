"""Phase 8 exit check: sdnctl-sim runs every scenario in one command and writes its reports."""

import contextlib
import csv
import io
import json
import subprocess
import sys
import sysconfig
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from sdnctl.cli import EXIT_FAILED, EXIT_OK, main
from sdnctl.jsonio import load_json
from sdnctl.report import (
    CAPACITY_FILE,
    CAPACITY_HEADER,
    REPORT_FILES,
    SUMMARY_FILE,
    TRACE_FILE,
    RunSummary,
)
from sdnctl.sim.adapters import MessageLog
from sdnctl.types import IncidentState
from tests.scenario_helpers import SCENARIOS

ALL = ["G1", "G2", "G4a", "G4b", "G5", "G6", "G6b", "G7", "G8"]

Run = tuple[Path, int, str, str]  # out folder, exit status, stdout, stderr


def run_cli(*args: str) -> tuple[int, str, str]:
    """Run sdnctl-sim in-process: (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(scope="module")
def all_runs(tmp_path_factory: pytest.TempPathFactory) -> Run:
    """Every scenario, run once with one command."""
    out = tmp_path_factory.mktemp("runs")
    code, stdout, stderr = run_cli("--scenario", str(SCENARIOS), "--out", str(out))
    return out, code, stdout, stderr


def summary_of(folder: Path) -> RunSummary:
    """A run's summary.json."""
    return load_json(RunSummary, folder / SUMMARY_FILE)


def golden_ends(golden: dict[str, Any]) -> dict[str, tuple[int, str]]:
    """Golden message total and end state of every scenario."""
    g6b = golden["G6"]["variant_b_ocs_on_one_spare"]
    return {
        "G1": (golden["G1"]["messages_total"], "CLOSED"),
        "G2": (golden["G2"]["messages_total"], "CLOSED"),
        "G4a": (golden["G4a"]["messages_total"], golden["G4a"]["end_state"]),
        "G4b": (golden["G4b"]["messages_total"], golden["G4b"]["end_state"]),
        "G5": (golden["G5"]["messages_total"], golden["G5"]["result"]),
        "G6": (golden["G6"]["messages_total"], "CLOSED"),
        "G6b": (g6b["messages_total"], g6b["result"]),
        "G7": (golden["G7"]["messages_total"], golden["G7"]["result"]),
        "G8": (golden["G8"]["messages_total"], "CLOSED"),
    }


# ------------------------------------------------------------------ the exit check


def test_one_command_runs_every_scenario(all_runs: Run, golden: dict[str, Any]) -> None:
    out, code, stdout, stderr = all_runs
    assert (code, stderr) == (EXIT_OK, "")
    assert sorted(p.name for p in out.iterdir()) == sorted(ALL)
    assert stdout.splitlines()[-1] == "9 run, 0 failed, 0 skipped (other topology)"
    for name, (total, state) in golden_ends(golden).items():
        assert sorted(p.name for p in (out / name).iterdir()) == sorted(REPORT_FILES)
        s = summary_of(out / name)
        assert s.scenario == name
        [incident] = s.incidents
        assert (s.messages_total, incident.state.value.upper()) == (total, state), name
        assert f"{name} (" in stdout


def test_summary_states_the_settings_run(all_runs: Run) -> None:
    out = all_runs[0]
    s = summary_of(out / "G4a")
    assert (s.topology, s.spare_modules_per_domain, s.timing_profile) == ("topology2", 1, "typical")
    assert (s.features.two_plus_two, s.features.ocs) == (True, True)
    assert s.events == ({"t_ms": 0, "event": "module_fail", "modules": ["npu-0.m0"]},)
    assert s.run_until_ms == 30_000
    assert (s.timing_us.device_write, s.timeouts_us.ack, s.limits.ack_resends) == (3000, 50000, 1)


def test_trace_file_matches_the_summary(all_runs: Run) -> None:
    out = all_runs[0]
    for name in ALL:
        s = summary_of(out / name)
        lines = (out / name / TRACE_FILE).read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]
        assert len(records) == s.trace_records
        assert all(isinstance(r["t"], int) and isinstance(r["kind"], str) for r in records)
        assert [r["t"] for r in records] == sorted(r["t"] for r in records), name
        assert sum(r["kind"] == "load" for r in records) == 1
        sends = Counter(r["type"] for r in records
                        if r["kind"] == "send" and r["type"] not in MessageLog.UNCOUNTED)
        assert sends == s.messages, name


def test_capacity_file_matches_the_summary(all_runs: Run, golden: dict[str, Any]) -> None:
    out = all_runs[0]
    for name in ALL:
        s = summary_of(out / name)
        with open(out / name / CAPACITY_FILE, encoding="utf-8", newline="") as f:
            header, *rows = list(csv.reader(f))
        assert tuple(header) == CAPACITY_HEADER
        times = [int(t) for t, _, _ in rows]
        assert times == sorted(times)
        final = {key: float(v) for _, key, v in rows}
        lowest: dict[str, float] = {}
        for _, key, v in rows:
            lowest[key] = min(lowest.get(key, 1.0), float(v))
        assert {k: (r.min, r.final) for k, r in s.capacity.items()} == {
            k: (lowest[k], final[k]) for k in final
        }, name
    for key in ("G4a", "G4b"):
        s = summary_of(out / key)
        for k, value in golden[key]["capacity_final"].items():
            assert s.capacity[k].final == value, (key, k)
            assert round(s.capacity[k].min, 6) == golden[key]["capacity_during"][k], (key, k)


# ------------------------------------------------------------------ determinism and config


def test_output_is_deterministic(tmp_path: Path) -> None:
    for run in ("a", "b"):
        code, _, _ = run_cli("--scenario", str(SCENARIOS / "g8.json"), "--out", str(tmp_path / run))
        assert code == EXIT_OK
    for f in REPORT_FILES:
        a = (tmp_path / "a" / "G8" / f).read_bytes()
        assert a == (tmp_path / "b" / "G8" / f).read_bytes(), f
        assert b"\r\n" not in a, f


def write_config(tmp_path: Path, data: dict[str, Any]) -> str:
    """A --config file holding data."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_config_timing_profile_replaces_the_scenarios(tmp_path: Path,
                                                      golden: dict[str, Any]) -> None:
    config = write_config(tmp_path, {"timing_profile": "worst"})
    code, stdout, stderr = run_cli("--scenario", str(SCENARIOS / "g4a.json"), "--config", config,
                                   "--out", str(tmp_path / "out"))
    assert code == EXIT_OK
    assert stderr == ("sdnctl-sim: warning: G4a: --config overrides the scenario's "
                      "timing_profile (typical -> worst)\n")
    s = summary_of(tmp_path / "out" / "G4a")
    assert (s.timing_profile, s.timing_us.lane_bringup) == ("worst", 2_500_000)
    [incident] = s.incidents
    done = golden["G4a"]["timeline_worst"]["groups_acked"]
    assert (incident.state, incident.ended_at) == (IncidentState.CLOSED, done * 1000)
    assert f"closed at {done} ms" in stdout


def test_config_feature_flag_replaces_the_scenarios(tmp_path: Path) -> None:
    config = write_config(tmp_path, {"features": {"ocs": False}})
    code, _, stderr = run_cli("--scenario", str(SCENARIOS / "g4a.json"), "--config", config,
                              "--out", str(tmp_path / "out"))
    assert code == EXIT_OK
    assert stderr == ("sdnctl-sim: warning: G4a: --config overrides the scenario's "
                      "features.ocs (true -> false)\n")  # two_plus_two is not set: no warning
    s = summary_of(tmp_path / "out" / "G4a")
    assert (s.features.two_plus_two, s.features.ocs) == (True, False)  # two_plus_two kept
    [incident] = s.incidents
    assert (incident.state, incident.reason) == (IncidentState.DEGRADED_WAITING_REPAIR, "no_ocs")


def test_config_values_reach_the_controller(tmp_path: Path) -> None:
    """G8 with device_write 4 ms and a 60 ms ACK timeout: GROUP_SET still goes out at 95 ms
    (only the PREPARE ACK, at 6 ms, is later), the lost ACK times out at 155 ms, and the resend
    is ACKed at 159 ms."""
    config = write_config(tmp_path, {"timing_us": {"typical": {"device_write": 4000}},
                                     "timeouts_us": {"ack": 60000}})
    code, _, stderr = run_cli("--scenario", str(SCENARIOS / "g8.json"), "--config", config,
                              "--out", str(tmp_path / "out"))
    assert (code, stderr) == (EXIT_OK, "")  # scenarios set no timing values: nothing overridden
    s = summary_of(tmp_path / "out" / "G8")
    assert (s.timing_us.device_write, s.timeouts_us.ack) == (4000, 60000)
    [incident] = s.incidents
    assert (incident.state, incident.ended_at) == (IncidentState.CLOSED, 159_000)
    records = (tmp_path / "out" / "G8" / TRACE_FILE).read_text(encoding="utf-8").splitlines()
    timeouts = [json.loads(r)["t"] for r in records if '"kind":"ack_timeout"' in r]
    assert timeouts == [155_000]


def test_config_equal_to_the_scenario_overrides_nothing(tmp_path: Path) -> None:
    config = write_config(tmp_path, {"timing_profile": "typical",
                                     "features": {"two_plus_two": True, "ocs": True}})
    code, _, stderr = run_cli("--scenario", str(SCENARIOS / "g5.json"), "--config", config,
                              "--out", str(tmp_path / "out"))
    assert (code, stderr) == (EXIT_OK, "")


# ------------------------------------------------------------------ choosing scenarios


def test_topology_keeps_only_its_scenarios(tmp_path: Path) -> None:
    code, stdout, _ = run_cli("--scenario", str(SCENARIOS / "g1.json"), str(SCENARIOS / "g5.json"),
                              "--topology", "topology2", "--out", str(tmp_path))
    assert code == EXIT_OK
    assert [p.name for p in tmp_path.iterdir()] == ["G5"]
    assert stdout.splitlines()[-1] == "1 run, 0 failed, 1 skipped (other topology)"


def test_a_file_named_twice_runs_once(tmp_path: Path) -> None:
    g5 = str(SCENARIOS / "g5.json")
    code, stdout, _ = run_cli("--scenario", g5, g5, "--out", str(tmp_path))
    assert code == EXIT_OK
    assert stdout.splitlines()[-1] == "1 run, 0 failed, 0 skipped (other topology)"


# ------------------------------------------------------------------ errors


def test_refuses_out_in_the_reference_folder(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "ref_model.py").write_text("", encoding="utf-8")
    for out in (reference, reference / "runs"):
        code, stdout, stderr = run_cli("--scenario", str(SCENARIOS / "g5.json"), "--out", str(out))
        assert (code, stdout) == (EXIT_FAILED, "")
        assert "reference folder" in stderr
    assert [p.name for p in reference.iterdir()] == ["ref_model.py"]


def bad_scenario(tmp_path: Path, stem: str, **changes: Any) -> str:
    """A copy of G5 with changes, written to tmp_path/<stem>.json."""
    data = json.loads((SCENARIOS / "g5.json").read_text(encoding="utf-8"))
    data.update(changes)
    path = tmp_path / f"{stem}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing_file", "no such scenario file or folder"),
        ("empty_folder", "no *.json scenario"),
        ("not_json", "Expecting value"),
        ("unknown_key", "unknown scenario keys"),
        ("bad_features", "scenario features must be"),
        ("bad_name", "is not a plain folder name"),
        ("same_name", "is also used by"),
        ("unknown_config_key", "unknown config key config.timing"),
        ("unknown_profile", "unknown timing profile"),
        ("config_not_object", "must be a JSON object"),
    ],
)
def test_bad_input_stops_before_any_run(case: str, message: str, tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    scenarios = [str(SCENARIOS / "g5.json")]
    config: dict[str, Any] | list[int] | None = None
    if case == "missing_file":
        scenarios.append(str(inputs / "nope.json"))
    elif case == "empty_folder":
        scenarios.append(str(inputs))
    elif case == "not_json":
        (inputs / "x.json").write_text("not json", encoding="utf-8")
        scenarios.append(str(inputs / "x.json"))
    elif case == "unknown_key":
        scenarios.append(bad_scenario(inputs, "x", name="X", extra=1))
    elif case == "bad_features":
        scenarios.append(bad_scenario(inputs, "x", name="X", features={"ocs": True}))
    elif case == "bad_name":
        scenarios.append(bad_scenario(inputs, "x", name="../X"))
    elif case == "same_name":
        scenarios.append(bad_scenario(inputs, "x", name="g5"))
    elif case == "unknown_config_key":
        config = {"timing": {}}
    elif case == "unknown_profile":
        config = {"timing_profile": "fast"}
    elif case == "config_not_object":
        config = [1]
    args = ["--scenario", *scenarios, "--out", str(tmp_path / "out")]
    if config is not None:
        (inputs / "config.json").write_text(json.dumps(config), encoding="utf-8")
        args += ["--config", str(inputs / "config.json")]
    code, stdout, stderr = run_cli(*args)
    assert (code, stdout) == (EXIT_FAILED, "")
    assert message in stderr
    assert not (tmp_path / "out").exists()


def test_a_scenario_that_cannot_run_does_not_stop_the_others(tmp_path: Path) -> None:
    bad = bad_scenario(tmp_path, "bad", name="Bad",
                       events=[{"t_ms": 0, "event": "module_fail", "modules": ["npu-999.m0"]}])
    stale = tmp_path / "out" / "Bad" / SUMMARY_FILE
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")  # an older run's report
    code, stdout, stderr = run_cli("--scenario", bad, str(SCENARIOS / "g5.json"),
                                   "--out", str(tmp_path / "out"))
    assert code == EXIT_FAILED
    assert "Bad: unknown modules: ['npu-999.m0']" in stderr
    assert stdout.splitlines()[-1] == "1 run, 1 failed, 0 skipped (other topology)"
    assert list((tmp_path / "out" / "Bad").iterdir()) == []
    assert summary_of(tmp_path / "out" / "G5").scenario == "G5"


def test_no_scenario_on_the_topology(tmp_path: Path) -> None:
    code, _, stderr = run_cli("--scenario", str(SCENARIOS / "g5.json"),
                              "--topology", "topology1", "--out", str(tmp_path / "out"))
    assert code == EXIT_FAILED
    assert "none of the scenarios runs on topology1" in stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--out", "x"],
        ["--scenario", "x"],
        ["--scenario", "x", "--out", "y", "--topology", "t3"],
    ],
)
def test_bad_command_line(args: list[str]) -> None:
    with pytest.raises(SystemExit) as e, contextlib.redirect_stderr(io.StringIO()):
        main(args)
    assert e.value.code == 2


# ------------------------------------------------------------------ entry points


def test_entry_points_start() -> None:
    script = Path(sysconfig.get_path("scripts")) / ("sdnctl-sim.exe" if sys.platform == "win32"
                                                    else "sdnctl-sim")
    for cmd in ([sys.executable, "-m", "sdnctl.cli", "--help"], [str(script), "--help"]):
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert done.returncode == 0, done.stderr
        assert "--scenario" in done.stdout and "--topology" in done.stdout
