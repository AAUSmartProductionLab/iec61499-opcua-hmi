"""The HMI against the controller it operates (iec61499-mgmt-py).

Reads the module specifications (cell/modules/*.yaml), the state and error
tables of the generator (modgen/library.py) and the generated state machines
(MOD_StateLogic, SKILL_Control) and checks that the profiles and the command
rules of the HMI are the same. Skipped unless a checkout of iec61499-mgmt-py is
next to this one or named by IEC61499_MGMT_PY.
"""

from __future__ import annotations

import ast
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from hmi import model
from hmi import profiles as prof

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
MGMT = Path(os.environ.get("IEC61499_MGMT_PY", ROOT.parent / "iec61499-mgmt-py"))
SPECS = MGMT / "cell" / "modules"
CONTROL = MGMT / "cell" / "control"
LIBRARY = MGMT / "iec61499-skill-lib" / "modgen" / "library.py"

pytestmark = pytest.mark.skipif(
    not SPECS.is_dir(), reason="no iec61499-mgmt-py checkout (set IEC61499_MGMT_PY)"
)

MODULES = {"filling": "filling.yaml", "stoppering": "stoppering.yaml"}


def spec(key: str) -> dict:
    return yaml.safe_load((SPECS / MODULES[key]).read_text(encoding="utf-8"))


def library_table(name: str) -> dict:
    tree = ast.parse(LIBRARY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not in {LIBRARY}")


def fb_type(key: str, folder: str, name: str) -> ET.Element:
    project = spec(key)["project"]
    return ET.parse(CONTROL / project / "Type Library" / folder / f"{name}.fbt").getroot()


# --- tables ------------------------------------------------------------------


def test_state_and_error_numbers_are_the_generators():
    assert {name: code for code, name in model.MODULE_STATES.items()} == library_table("STATES")
    assert {name: code for code, name in model.SKILL_STATES.items()} == library_table("SKILL_STATES")
    errors = {name: code for code, name in model.ERROR_CODES.items() if code}
    assert errors == library_table("ERRORS")


# --- state machines ----------------------------------------------------------


def _algorithms(root: ET.Element) -> dict[str, str]:
    return {alg.get("Name"): alg.findtext("ST") or "" for alg in root.iter("Algorithm")}


def _here_to_state(root: ET.Element) -> dict[int, int]:
    """Map the ECC position (Here) to the published State, following "1" transitions."""
    algorithms = _algorithms(root)
    ecc = root.find(".//ECC")
    sets: dict[str, dict[str, int]] = {}
    for ec_state in ecc.findall("ECState"):
        values: dict[str, int] = {}
        for action in ec_state.findall("ECAction"):
            text = algorithms.get(action.get("Algorithm") or "", "")
            for var, number in re.findall(r"\b(State|Here) := (\d+);", text):
                values[var] = int(number)
        sets[ec_state.get("Name")] = values
    previous = {t.get("Destination"): t.get("Source") for t in ecc.findall("ECTransition")
                if not t.get("Source").startswith("Chk")}
    mapping: dict[int, int] = {}
    for name, values in sets.items():
        if "Here" not in values:
            continue
        current = name
        while "State" not in sets[current]:
            current = previous[current]
        mapping[values["Here"]] = sets[current]["State"]
    return mapping


def _accepted_positions(text: str, every: set[int]) -> set[int]:
    """Positions (Here) a Chk algorithm accepts its command in."""
    accepted = re.search(r"Accepted := Owned AND \((.*)\);", text)
    if accepted:
        condition = accepted.group(1)
        if "<>" in condition:
            return every - {int(n) for n in re.findall(r"Here <> (\d+)", condition)}
        return {int(n) for n in re.findall(r"Here = (\d+)", condition)}
    # SKILL_Control Start: refused in the positions named before the ELSE branch
    refused = {int(n) for n in re.findall(r"Here = (\d+)", text)}
    return every - refused


@pytest.mark.parametrize("key", sorted(MODULES))
def test_module_commands_are_accepted_where_the_controller_accepts_them(key):
    root = fb_type(key, "Module/Base", "MOD_StateLogic")
    here = _here_to_state(root)
    algorithms = _algorithms(root)
    for command in model.MODULE_COMMANDS:
        positions = _accepted_positions(algorithms[f"Alg_Chk{command}"], set(here))
        assert {here[p] for p in positions} == set(model.MODULE_COMMAND_FROM_STATES[command]), command


@pytest.mark.parametrize("key", sorted(MODULES))
def test_skill_commands_are_accepted_where_the_controller_accepts_them(key):
    root = fb_type(key, "Skills/Base", "SKILL_Control")
    here = _here_to_state(root)
    algorithms = _algorithms(root)
    every = set(model.SKILL_STATES)

    def accepted(command: str) -> set[int]:
        return {here[p] for p in _accepted_positions(algorithms[f"Alg_Chk{command}"], set(here))}

    assert "ModState <> 6" in algorithms["Alg_ChkStart"], "Start only in module Execute"
    assert accepted("Start") == set(model.SKILL_STATES_ALLOWED_TO_START)
    for command in ("Stop", "Abort", "Reset"):
        expected = {
            state for state in every
            if model.skill_command_enabled(command, state, model.EXECUTE, True)
        }
        assert accepted(command) == expected, command


@pytest.mark.parametrize("key", sorted(MODULES))
def test_the_skill_diagram_has_the_controllers_transitions(key):
    """The HMI draws exactly the transitions of SKILL_Control between published states."""
    root = fb_type(key, "Skills/Base", "SKILL_Control")
    sets = {}
    algorithms = _algorithms(root)
    ecc = root.find(".//ECC")
    for ec_state in ecc.findall("ECState"):
        text = " ".join(algorithms.get(a.get("Algorithm") or "", "") for a in ec_state.findall("ECAction"))
        match = re.search(r"\bState := (\d+);", text)
        sets[ec_state.get("Name")] = int(match.group(1)) if match else None
    edges: dict[str, list[str]] = {}
    for transition in ecc.findall("ECTransition"):
        source = transition.get("Source")
        # A refused command returns to where it came from (Here = n): only the
        # accepted branch of a check leads anywhere.
        if source.startswith("Chk") and transition.get("Condition") != "Accepted":
            continue
        edges.setdefault(source, []).append(transition.get("Destination"))

    def reached(name: str, seen: frozenset[str]) -> set[int]:
        if sets.get(name) is not None:
            return {sets[name]}
        found: set[int] = set()
        for nxt in edges.get(name, []):
            if nxt not in seen:
                found |= reached(nxt, seen | {nxt})
        return found

    here = _here_to_state(root)
    positions = {
        ec_state.get("Name"): int(m.group(1))
        for ec_state in ecc.findall("ECState")
        for a in ec_state.findall("ECAction")
        if (m := re.search(r"\bHere := (\d+);", algorithms.get(a.get("Algorithm") or "", "")))
    }
    drawn = {(source, target) for source, target, _, _ in model.SKILL_DIAGRAM}
    controller: set[tuple[int, int]] = set()
    for name, state in sets.items():
        if state is None:
            continue
        for nxt in edges.get(name, []):
            if nxt.startswith("Chk"):
                allowed = _accepted_positions(algorithms[f"Alg_{nxt}"], set(here))
                if positions.get(name) not in allowed:
                    continue
            for target in reached(nxt, frozenset({nxt})):
                if target != state:
                    controller.add((state, target))
    assert controller == drawn


# --- address space -------------------------------------------------------------


def _step(entry) -> tuple[str, str, dict]:
    """(name, skill, bound parameters) of a step in a spec sequence."""
    if isinstance(entry, str):
        return entry, entry, {}
    entry = dict(entry)
    alias = entry.pop("as", None)
    (skill, bind), = entry.items()
    return alias or skill, skill, dict(bind or {})


def _check_steps(key: str, steps: tuple[prof.Step, ...], entries: list, where: str) -> None:
    skills = spec(key)["skills"]
    assert [s.name for s in steps] == [_step(e)[0] for e in entries], where
    for step, entry in zip(steps, entries, strict=True):
        name, skill, bind = _step(entry)
        assert step.runs == skill, f"{where}/{name}"
        equipment = skills[skill].get("equipment")
        assert step.uses == ((equipment,) if equipment else ()), f"{where}/{name}"
        params = skills[skill].get("parameters", {})
        assert [p.name for p in step.params] == list(params), f"{where}/{name}"
        for param in step.params:
            assert param.default == pytest.approx(bind.get(param.name, params[param.name]["default"])), (
                f"{where}/{name}/{param.name}"
            )


@pytest.mark.parametrize("key", sorted(MODULES))
def test_profile_is_the_module_specification(key):
    data = spec(key)
    profile = prof.get_profile(key)
    assert f"/Objects/{profile.root}" == data["opcua_root"]

    sensors = {(s.equipment, s.name) for s in profile.sensors}
    assert sensors == {(eq, name) for eq, item in data["equipment"].items() for name in item.get("inputs", {})}

    offered = {name: s for name, s in data["skills"].items() if s.get("offered", True)}
    primitives = {s.name: s for s in profile.skills if not s.module_level}
    assert set(primitives) == set(offered)
    for name, skill in offered.items():
        mine = primitives[name]
        assert mine.uses == ((skill["equipment"],) if skill.get("equipment") else ()), name
        params = skill.get("parameters", {})
        assert [p.name for p in mine.params] == list(params), name
        for param in mine.params:
            src = params[param.name]
            assert (param.minimum, param.maximum, param.default) == (
                src["minimum"], src["maximum"], src["default"]
            ), f"{name}/{param.name}"
        assert [r.name for r in mine.results] == list(skill.get("results", {})), name

    composites = {s.name: s for s in profile.skills if s.module_level}
    assert set(composites) == set(data.get("composites", {}))
    for name, comp in data.get("composites", {}).items():
        mine = composites[name]
        _check_steps(key, mine.steps, comp["execute"], f"{name}/Execute")
        _check_steps(key, mine.stop_steps, comp.get("stop", []), f"{name}/Stopping")
        assert [r.name for r in mine.results] == list(comp.get("results", {})), name
        assert set(mine.uses) == {u for step in mine.steps for u in step.uses}, name

    procedures = {p.name: p for p in profile.procedures}
    assert set(procedures) == set(data.get("procedures", {}))
    for name, entries in data.get("procedures", {}).items():
        _check_steps(key, procedures[name].steps, entries, f"Procedures/{name}")
