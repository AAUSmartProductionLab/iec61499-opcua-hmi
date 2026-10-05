"""modlink.aas: a module's AAS read into a Resource by following its references.

The AAS in tests/data are what iec61499-mgmt-py's modreg registers (tools/aas_fixtures.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hmi import profiles as prof
from modlink import aas

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
