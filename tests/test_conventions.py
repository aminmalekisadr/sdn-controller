"""Repository rules: src/ never imports reference/, and every public name has a docstring."""

import importlib
import inspect
import re

from tests.conftest import REPO_ROOT

API_MODULES = [
    "sdnctl.types",
    "sdnctl.model",
    "sdnctl.tables",
    "sdnctl.messages",
    "sdnctl.config",
    "sdnctl.interfaces",
    "sdnctl.jsonio",
    "sdnctl.topology",
    "sdnctl.topology.builder",
    "sdnctl.topology.validator",
    "sdnctl.topology.loader",
    "sdnctl.topology.view",
    "sdnctl.sim",
    "sdnctl.sim.adapters",
    "sdnctl.sim.capacity",
    "sdnctl.sim.clock",
    "sdnctl.sim.device",
    "sdnctl.sim.injector",
    "sdnctl.sim.ocs",
    "sdnctl.sim.physical",
    "sdnctl.sim.simulator",
    "sdnctl.sim.trace",
    "sdnctl.topology_manager",
    "sdnctl.topology_manager.correlator",
    "sdnctl.topology_manager.lanes",
    "sdnctl.topology_manager.manager",
    "sdnctl.routing_engine",
    "sdnctl.routing_engine.check",
    "sdnctl.routing_engine.diff",
    "sdnctl.routing_engine.engine",
    "sdnctl.routing_engine.groups",
    "sdnctl.routing_engine.routes",
    "sdnctl.ocs",
    "sdnctl.ocs.controller",
    "sdnctl.ocs.matrix",
    "sdnctl.ocs.planner",
    "sdnctl.controller",
    "sdnctl.controller.sdn_controller",
    "sdnctl.scheduler",
    "sdnctl.scheduler.scheduler",
    "sdnctl.scheduler.commands",
    "sdnctl.scheduler.incident",
    "sdnctl.scheduler.sender",
    "sdnctl.scenario",
    "sdnctl.report",
    "sdnctl.cli",
]


def test_src_never_imports_reference() -> None:
    pattern = re.compile(r"^\s*(from|import)\s+(reference|ref_model)\b", re.MULTILINE)
    src = REPO_ROOT / "src" / "sdnctl"
    offenders = [
        str(p) for p in src.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def has_own_doc(obj: object) -> bool:
    """True if obj has a written docstring (not the one dataclass generates)."""
    doc = getattr(obj, "__doc__", None) or ""
    if inspect.isclass(obj) and doc.startswith(f"{obj.__name__}("):
        return False
    return bool(doc.strip())


def test_public_api_has_docstrings() -> None:
    missing: list[str] = []
    for mod_name in API_MODULES:
        mod = importlib.import_module(mod_name)
        if not has_own_doc(mod):
            missing.append(mod_name)
        for name, obj in vars(mod).items():
            if name.startswith("_") or getattr(obj, "__module__", None) != mod_name:
                continue
            if not (inspect.isclass(obj) or inspect.isfunction(obj)):
                continue
            if not has_own_doc(obj):
                missing.append(f"{mod_name}.{name}")
            if inspect.isclass(obj):
                for member_name, member in vars(obj).items():
                    fn = member.fget if isinstance(member, property) else member
                    if member_name.startswith("_") or not inspect.isfunction(fn):
                        continue
                    if not has_own_doc(fn):
                        missing.append(f"{mod_name}.{name}.{member_name}")
    assert missing == []
