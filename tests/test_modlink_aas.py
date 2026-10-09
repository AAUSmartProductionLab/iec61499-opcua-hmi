"""modlink.aas: a module's AAS read into a Resource by following its references.

The AAS in tests/data are what iec61499-mgmt-py's modreg registers (tools/aas_fixtures.py).
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from hmi import aas as hmi_aas
from hmi import profiles as prof
from modlink import Link, Module, SkillState, aas
from modlink.module import Paths

from test_integration import Simulator

DATA = Path(__file__).resolve().parent / "data"
FILLING = DATA / "FillingModuleAAS.json.gz"
STOPPERING = DATA / "StopperingModuleAAS.json.gz"


@pytest.fixture(scope="module")
def filling() -> aas.Resource:
    [resource] = aas.load(str(FILLING))
    return resource


@pytest.fixture(scope="module")
def stoppering() -> aas.Resource:
    [resource] = aas.load(str(STOPPERING))
    return resource


def test_the_interface_is_the_one_the_module_serves(filling, stoppering):
    for resource, hand in ((filling, prof.FILLING), (stoppering, prof.STOPPERING)):
        assert resource.root == hand.root and resource.interface.namespace == 1
        assert set(prof.monitored_paths(hand)) <= set(resource.interface.variables)
        assert set(prof.method_paths(hand)) == set(resource.interface.methods)
    assert filling.endpoint == "opc.tcp://192.168.0.191:4840"


def test_skills_lead_to_their_commands_and_properties(filling, stoppering):
    dispensing = filling.skills["Dispensing"]
    assert dispensing.kind == "Composite"
    assert dispensing.commands == {c: f"Skills/Dispensing/{c}" for c in ("Start", "Stop", "Abort", "Reset")}
    assert (dispensing.state, dispensing.error) == ("Skills/Dispensing/State", "Skills/Dispensing/ErrorID")
    assert dispensing.results == {"Weight": "Skills/Dispensing/Results/Weight"}
    arm = stoppering.skills["MoveArm"]
    assert [(p.name, p.unit, p.minimum, p.maximum, p.default) for p in arm.parameters] == \
        [("Angle", "deg", 0.0, 180.0, 120.0), ("Settle", "s", 0.0, 5.0, 2.0)]   # Start's call order
    assert arm.arguments({"Angle": 45}) == [45.0, 2.0]
    with pytest.raises(KeyError):
        arm.arguments({"Speed": 1})
    assert filling.module_commands["Clear"] == "Module/Clear" and filling.module_state == "Module/State"
    assert filling.occupation == {"Occupy": "Occupation/Occupy", "Release": "Occupation/Release",
                                  "Occupied": "Occupation/Occupied"}


def test_capabilities_lead_to_the_skill_that_realizes_them(filling, stoppering):
    cap = filling.capability("https://smartproductionlab.aau.dk/semantics/Filling")
    assert cap is filling.capability("Filling") and cap.skill == "Dispensing"
    assert filling.realizing("Filling") is filling.skills["Dispensing"]
    volume = cap.properties["FillVolume"]
    assert (volume.minimum, volume.maximum, volume.unit) == (0.5, 10.0, "mL")
    assert volume.admits(2.0) and not volume.admits(12.0)
    assert cap.properties["ContainerType"].admits("vial") and not cap.properties["ContainerType"].admits("syringe")
    assert stoppering.realizing("Stoppering").name == "Stoppering"
    with pytest.raises(KeyError):
        filling.capability("Capping")


def test_paths_follow_the_references_not_the_names():
    """Point Dispensing's Start at Tare's action: the client calls what the AAS says."""
    env = aas.read_file(FILLING)
    skills = next(s for s in env["submodels"] if s["idShort"] == "Skills")
    dispensing = aas.at(skills, "Skills", "Dispensing")
    tare_start = copy.deepcopy(aas.value(aas.at(aas.at(skills, "Skills", "Tare"), "Methods", "Start")))
    aas.at(dispensing, "Methods", "Start")["value"] = tare_start
    [resource] = [aas.describe(e) for e in aas.environments(env) if e.is_module]
    assert Paths(resource).skill_command("Dispensing", "Start") == "Skills/Tare/Start"
    assert Paths().skill_command("Dispensing", "Start") == "Skills/Dispensing/Start"     # by convention
    broken = aas.read_file(FILLING)
    skills = next(s for s in broken["submodels"] if s["idShort"] == "Skills")
    aas.at(skills, "Skills", "Dispensing", "StateReference")["value"]["keys"][-1]["value"] = "Nowhere"
    with pytest.raises(aas.AasError, match="Dispensing's state"):
        [aas.describe(e) for e in aas.environments(broken) if e.is_module]


def test_an_agent_runs_a_capability_of_a_module_described_by_its_aas(filling):
    """The simulated module serves the address space its AAS describes."""
    [profile] = hmi_aas.load(str(FILLING))
    simulator = Simulator(["filling"], profiles=[profile])
    simulator.start()

    async def scenario():
        async with Link(simulator.endpoint, [filling.interface], sampling_ms=100) as link:
            assert await link.wait_connected(20)
            module = Module(link, filling.root, session="agent", resource=filling)
            await module.occupy()
            await module.bring_to_execute()
            run = await module.run_capability("Filling", timeout=30)
            assert run.skill == "Dispensing" and run.state == SkillState.SUCCEEDED
            assert run.results["Weight"] > 0
            await module.command("Stop")
            await module.release()

    try:
        asyncio.run(scenario())
    finally:
        simulator.stop()


# --- the modules as they are built since ARSO 0.7: a skill is its commands, primitives are in the
# --- AAS of their component (tests/data/*07.json.gz: the module and its components in one file)

# --- ARSO 0.8 (*08.json.gz): the kind of a skill is a supplemental id, and the module's own commands
# --- are skills of the kind ModuleControl. Both are read, and give the same.

FILLING07 = DATA / "FillingModule07.json.gz"
STOPPERING07 = DATA / "StopperingModule07.json.gz"
FILLING08 = DATA / "FillingModule08.json.gz"
STOPPERING08 = DATA / "StopperingModule08.json.gz"


@pytest.fixture(scope="module", params=[FILLING07, FILLING08], ids=["0.7", "0.8"])
def filling07(request) -> aas.Resource:
    [resource] = aas.load(str(request.param))      # the components are no modules: they have no interface
    return resource


def test_a_module_is_read_with_the_skills_of_its_components(filling07):
    assert filling07.id_short == "FillingModuleAAS" and filling07.root == "Filling"
    assert list(filling07.skills) == ["Dispensing", "Home", "MoveAxis", "Dispense", "Tare", "Weigh"]
    assert {s.name: s.held_by for s in filling07.skills.values()} == {
        "Dispensing": "", "Home": "FillingLinearAxisAAS", "MoveAxis": "FillingLinearAxisAAS",
        "Dispense": "FillingPumpAAS", "Tare": "FillingScaleAAS", "Weigh": "FillingScaleAAS"}
    dispensing = filling07.skills["Dispensing"]
    assert dispensing.kind == "Composite" and filling07.skills["MoveAxis"].kind == "Primitive"
    assert dispensing.meaning == "https://smartproductionlab.aau.dk/skills/Dispensing"
    # A command leads to its action through its InterfaceReference.
    assert dispensing.commands == {c: f"Skills/Dispensing/{c}" for c in ("Start", "Stop", "Abort", "Reset")}
    # State, ErrorID and results are found by what they mean: the data point showing them, and the
    # interface property the mapping feeds it from.
    assert (dispensing.state, dispensing.error) == ("Skills/Dispensing/State", "Skills/Dispensing/ErrorID")
    assert dispensing.results == {"Weight": "Skills/Dispensing/Results/Weight"}
    # The parameters are the inputs of Start's Operation after the session, in call order.
    pump = filling07.skills["Dispense"]
    assert [(p.name, p.unit, p.minimum, p.maximum, p.default) for p in pump.parameters] == \
        [("Volume", "mL", 0.5, 10.0, 1.0), ("FlowRate", "mL/s", 0.1, 5.0, 1.0)]
    assert pump.arguments({"Volume": 3}) == [3.0, 1.0]
    assert filling07.skills["MoveAxis"].commands["Start"] == "Skills/MoveAxis/Start"
    # The module's own commands (a submodel in 0.7, skills of a kind of their own in 0.8) are not
    # among the skills; its state and occupation are data points.
    assert filling07.module_commands == {c: f"Module/{c}" for c in ("Reset", "Start", "Stop", "Abort", "Clear")}
    assert filling07.module_state == "Module/State"
    assert filling07.occupation == {"Occupy": "Occupation/Occupy", "Release": "Occupation/Release",
                                    "Occupied": "Occupation/Occupied"}
    assert filling07.realizing("https://smartproductionlab.aau.dk/semantics/Filling") is dispensing
    assert filling07.capability("Filling").properties["FillVolume"].admits(2.0)


@pytest.mark.parametrize("source", [FILLING07, FILLING08], ids=["0.7", "0.8"])
def test_the_new_structure_is_followed_by_reference_and_meaning_too(source):
    env = aas.read_file(source)
    skills = next(s for s in env["submodels"] if s["id"].endswith("FillingModuleAAS/submodels/Skills"))
    scale = next(s for s in env["submodels"] if s["id"].endswith("FillingScaleAAS/submodels/Skills"))
    tare_start = copy.deepcopy(aas.value(aas.at(scale, "Skills", "Tare", "Start", "InterfaceReference")))
    aas.at(skills, "Skills", "Dispensing", "Start", "InterfaceReference")["value"] = tare_start
    [resource] = [aas.describe(e) for e in aas.environments(env) if e.is_module]
    assert Paths(resource).skill_command("Dispensing", "Start") == "Skills/Tare/Start"
    # A skill whose state no data point shows cannot be watched: the AAS is refused.
    broken = aas.read_file(source)
    data = next(s for s in broken["submodels"] if s["idShort"] == "OperationalData")
    point = aas.at(data, "Dispensing_State")
    point["semanticId"]["keys"][0]["value"] += "/Elsewhere"
    with pytest.raises(aas.AasError, match="Dispensing's state"):
        [aas.describe(e) for e in aas.environments(broken) if e.is_module]


def test_a_command_of_the_module_is_found_through_its_start():
    """ARSO 0.8: Reset is a skill of the kind ModuleControl; what calls it is its Start's action."""
    env = aas.read_file(FILLING08)
    skills = next(s for s in env["submodels"] if s["id"].endswith("FillingModuleAAS/submodels/Skills"))
    assert "Module" not in [s["idShort"] for s in env["submodels"]]
    reset = aas.at(skills, "Skills", "Reset")
    assert aas.kind_of(reset) == "ModuleControl" and aas.semantic(reset) == "https://smartproductionlab.aau.dk/skill"
    clear = copy.deepcopy(aas.value(aas.at(skills, "Skills", "Clear", "Start", "InterfaceReference")))
    aas.at(reset, "Start", "InterfaceReference")["value"] = clear
    [resource] = [aas.describe(e) for e in aas.environments(env) if e.is_module]
    assert resource.module_commands["Reset"] == "Module/Clear" and "Reset" not in resource.skills


@pytest.mark.parametrize("source", [STOPPERING07, STOPPERING08], ids=["0.7", "0.8"])
def test_both_modules_of_the_new_structure_are_read(source):
    [stoppering] = aas.load(str(source))
    assert list(stoppering.skills) == ["Stoppering", "Home", "MoveAxis", "PressStopper", "RetractPiston"]
    assert stoppering.realizing("Stoppering").name == "Stoppering" and stoppering.skills["Stoppering"].parameters == ()
    # Every method and variable of the interface is in the Interface a Link is made for.
    assert "Skills/PressStopper/Start" in stoppering.interface.methods
    assert {"Equipment/LinearAxis/ActualPosition", "Equipment/LinearAxis/Homed"} <= set(stoppering.interface.variables)
