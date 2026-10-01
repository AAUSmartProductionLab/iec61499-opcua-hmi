"""Declarative description of the address space of each module.

Transcribed from opcua-filling.md and opcua-stoppering.md. The profiles drive
the OPC UA client (which nodes to resolve and monitor), the Flask API and the
HMI page, so there is a single place to update when a module changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .model import MODULE_COMMANDS, SKILL_COMMANDS

NAMESPACE_INDEX = 1
NODES_FOLDER = "Objects"


@dataclass(frozen=True)
class Param:
    name: str
    unit: str = ""
    minimum: float = 0.0
    maximum: float = 0.0
    default: float = 0.0
    step: float = 0.1
    digits: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "default": self.default,
            "step": self.step,
            "digits": self.digits,
        }


@dataclass(frozen=True)
class Step:
    name: str
    label: str
    uses: tuple[str, ...] = ()
    params: tuple[Param, ...] = ()
    results: tuple[Param, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "uses": list(self.uses),
            "params": [p.to_dict() for p in self.params],
            "results": [p.to_dict() for p in self.results],
        }


@dataclass(frozen=True)
class Skill:
    name: str
    label: str
    description: str
    params: tuple[Param, ...] = ()
    results: tuple[Param, ...] = ()
    steps: tuple[Step, ...] = ()
    stop_steps: tuple[Step, ...] = ()
    module_level: bool = False
    callable: bool = True
    uses: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
            "results": [p.to_dict() for p in self.results],
            "steps": [s.to_dict() for s in self.steps],
            "stopSteps": [s.to_dict() for s in self.stop_steps],
            "moduleLevel": self.module_level,
            "callable": self.callable,
            "uses": list(self.uses),
        }


@dataclass(frozen=True)
class Sensor:
    name: str
    label: str
    kind: str
    equipment: str
    unit: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "equipment": self.equipment,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class Procedure:
    name: str
    label: str
    steps: tuple[Step, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass(frozen=True)
class ModuleProfile:
    key: str
    title: str
    root: str
    summary: str
    sensors: tuple[Sensor, ...] = ()
    skills: tuple[Skill, ...] = ()
    procedures: tuple[Procedure, ...] = ()
    notes: tuple[str, ...] = ()
    default_endpoint: str = ""
    equipment_notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "root": self.root,
            "summary": self.summary,
            "namespaceIndex": NAMESPACE_INDEX,
            "sensors": [s.to_dict() for s in self.sensors],
            "skills": [s.to_dict() for s in self.skills],
            "procedures": [p.to_dict() for p in self.procedures],
            "notes": list(self.notes),
            "equipmentNotes": list(self.equipment_notes),
        }


DURATION = Param("Duration", "s", 0.0, 10.0, 2.0, step=0.5, digits=1)
ANGLE = Param("Angle", "deg", 0.0, 180.0, 120.0, step=1.0, digits=1)
SETTLE = Param("Settle", "s", 0.0, 5.0, 2.0, step=0.5, digits=1)
WEIGHT_RESULT = Param("Weight", "g", 0.0, 10000.0, 0.0, step=0.1, digits=1)


FILLING = ModuleProfile(
    key="filling",
    title="Filling",
    root="Filling",
    summary="Needle lift and scale: move, attach, dispense and weigh.",
    default_endpoint="opc.tcp://192.168.0.191:4840",
    sensors=(
        Sensor("AtTop", "Needle at top", "bool", "NeedleAxis"),
        Sensor("AtBottom", "Needle at bottom", "bool", "NeedleAxis"),
        Sensor("Weight", "Scale weight", "double", "Scale", unit="g"),
    ),
    skills=(
        Skill(
            name="MoveNeedleUp",
            label="Move needle up",
            description="Needle up to the top end switch. Fails with Timeout (3) after 8 s.",
            uses=("NeedleAxis",),
        ),
        Skill(
            name="MoveNeedleDown",
            label="Move needle down",
            description="Needle down to the bottom end switch. Fails with Timeout (3) after 8 s.",
            uses=("NeedleAxis",),
        ),
        Skill(
            name="AttachNeedle",
            label="Attach needle",
            description="Needle down to the attachment position without the start boost.",
            uses=("NeedleAxis",),
        ),
        Skill(
            name="Tare",
            label="Tare",
            description="Tare the scale. Ends after 2 s.",
            uses=("Scale",),
        ),
        Skill(
            name="Weigh",
            label="Weigh",
            description="Read the weight. Ends after 0.2 s.",
            uses=("Scale",),
            results=(WEIGHT_RESULT,),
        ),
        Skill(
            name="Dispensing",
            label="Dispensing",
            description="Needle down, dwell, needle up, weigh.",
            module_level=True,
            uses=("NeedleAxis", "Scale"),
            results=(WEIGHT_RESULT,),
            steps=(
                Step("MoveNeedleDown", "Needle down to the bottom end switch", ("NeedleAxis",)),
                Step(
                    "Dwell",
                    "Wait at the current position",
                    (),
                    (Param("Duration", "s", 0.0, 60.0, 1.0, step=0.5, digits=1),),
                ),
                Step("MoveNeedleUp", "Needle up to the top end switch", ("NeedleAxis",)),
                Step("Weigh", "Read the weight", ("Scale",), results=(WEIGHT_RESULT,)),
            ),
            stop_steps=(Step("MoveNeedleUp", "Needle up to the top end switch", ("NeedleAxis",)),),
        ),
    ),
    procedures=(
        Procedure(
            "Resetting",
            "Resetting procedure",
            (Step("MoveNeedleUp", "Needle up to the top end switch", ("NeedleAxis",)),),
        ),
        Procedure(
            "Stopping",
            "Stopping procedure",
            (Step("MoveNeedleUp", "Needle up to the top end switch", ("NeedleAxis",)),),
        ),
    ),
    notes=(
        "MoveNeedleUp and MoveNeedleDown must hold NOT (AtTop AND AtBottom) during the run, "
        "otherwise they fail with InvariantViolated (2).",
        "Dwell is a step only, it is not callable on its own.",
        "The scale has no hardware yet, the ESP32 publishes a random weight.",
    ),
)


STOPPERING = ModuleProfile(
    key="stoppering",
    title="Stoppering",
    root="Stoppering",
    summary="Piston, plunger and stopper arm: stoppering cycle and single motions.",
    default_endpoint="opc.tcp://localhost:4841",
    sensors=(Sensor("AtLimit", "Piston at limit switch", "bool", "Piston"),),
    skills=(
        Skill(
            name="LowerPiston",
            label="Lower piston",
            description="Piston down to the limit switch (working position). Timeout (3) after 10 s.",
            uses=("Piston",),
        ),
        Skill(
            name="RaisePiston",
            label="Raise piston",
            description="Piston up for Duration (open loop, no top sensor).",
            params=(DURATION,),
            uses=("Piston",),
        ),
        Skill(
            name="ExtendPlunger",
            label="Extend plunger",
            description="Plunger down for Duration (open loop).",
            params=(Param("Duration", "s", 0.0, 20.0, 10.0, step=0.5, digits=1),),
            uses=("Plunger",),
        ),
        Skill(
            name="RetractPlunger",
            label="Retract plunger",
            description="Plunger up for Duration (open loop).",
            params=(Param("Duration", "s", 0.0, 20.0, 6.5, step=0.5, digits=1),),
            uses=("Plunger",),
        ),
        Skill(
            name="MoveArm",
            label="Move stopper arm",
            description="Servo to Angle and wait Settle seconds (no feedback).",
            params=(ANGLE, SETTLE),
            uses=("StopperArm",),
        ),
        Skill(
            name="Stoppering",
            label="Stoppering cycle",
            description="Piston to the working position, place the stopper, plunge, piston up.",
            module_level=True,
            uses=("Piston", "StopperArm", "Plunger"),
            steps=(
                Step("LowerPiston", "Piston down to the limit switch", ("Piston",)),
                Step(
                    "ArmIn",
                    "Servo to the inner position",
                    ("StopperArm",),
                    (Param("Angle", "deg", 0.0, 180.0, 1.0, step=1.0, digits=1), SETTLE),
                ),
                Step(
                    "ArmOut",
                    "Servo to the outer position",
                    ("StopperArm",),
                    (Param("Angle", "deg", 0.0, 180.0, 121.0, step=1.0, digits=1), SETTLE),
                ),
                Step(
                    "ExtendPlunger",
                    "Plunger down",
                    ("Plunger",),
                    (Param("Duration", "s", 0.0, 20.0, 10.0, step=0.5, digits=1),),
                ),
                Step(
                    "RetractPlunger",
                    "Plunger up",
                    ("Plunger",),
                    (Param("Duration", "s", 0.0, 20.0, 6.5, step=0.5, digits=1),),
                ),
                Step(
                    "RaisePiston",
                    "Piston up",
                    ("Piston",),
                    (Param("Duration", "s", 0.0, 10.0, 2.0, step=0.5, digits=1),),
                ),
            ),
        ),
    ),
    procedures=(
        Procedure(
            "Resetting",
            "Resetting procedure",
            (
                Step(
                    "ArmMiddle",
                    "Servo to the middle position",
                    ("StopperArm",),
                    (Param("Angle", "deg", 0.0, 180.0, 90.0, step=1.0, digits=1), SETTLE),
                ),
                Step(
                    "ArmHome",
                    "Servo to the home position",
                    ("StopperArm",),
                    (Param("Angle", "deg", 0.0, 180.0, 120.0, step=1.0, digits=1), SETTLE),
                ),
                Step(
                    "RetractPlunger",
                    "Plunger up",
                    ("Plunger",),
                    (Param("Duration", "s", 0.0, 20.0, 6.5, step=0.5, digits=1),),
                ),
                Step("LowerPiston", "Piston down to the limit switch", ("Piston",)),
                Step(
                    "RaisePiston",
                    "Piston up",
                    ("Piston",),
                    (Param("Duration", "s", 0.0, 10.0, 1.5, step=0.5, digits=1),),
                ),
            ),
        ),
    ),
    notes=("This module has no Stopping procedure, the state passes at once.",),
    equipment_notes=(
        "Plunger: linear actuator through an L298N, no sensors.",
        "StopperArm: servo 0..180 degrees (outer home 120, inner 1), no feedback.",
        "Piston: DC motor through an L298N with a limit switch at the working position.",
    ),
)


PROFILES: dict[str, ModuleProfile] = {FILLING.key: FILLING, STOPPERING.key: STOPPERING}

STEP_GROUP_EXECUTE = "Execute"
STEP_GROUP_STOPPING = "Stopping"


def get_profile(key: str) -> ModuleProfile:
    try:
        return PROFILES[key]
    except KeyError:
        raise KeyError(f"unknown module '{key}', known modules: {', '.join(sorted(PROFILES))}") from None


def step_paths(prefix: str, step: Step, results: Iterable[Param] = ()) -> list[str]:
    paths = [f"{prefix}/State", f"{prefix}/ErrorID"]
    paths += [f"{prefix}/Parameters/{p.name}" for p in step.params]
    paths += [f"{prefix}/Results/{p.name}" for p in results]
    return paths


def monitored_paths(profile: ModuleProfile) -> list[str]:
    """Every variable of a module, as paths below the root object."""
    paths = ["Occupation/Occupied", "Module/State"]
    paths += [f"Equipment/{s.equipment}/{s.name}" for s in profile.sensors]
    for skill in profile.skills:
        base = f"Skills/{skill.name}"
        paths += [f"{base}/State", f"{base}/ErrorID"]
        paths += [f"{base}/Parameters/{p.name}" for p in skill.params]
        paths += [f"{base}/Results/{p.name}" for p in skill.results]
        for group, steps in ((STEP_GROUP_EXECUTE, skill.steps), (STEP_GROUP_STOPPING, skill.stop_steps)):
            for step in steps:
                paths += step_paths(f"{base}/{group}/{step.name}", step, step.results)
    for procedure in profile.procedures:
        for step in procedure.steps:
            paths += step_paths(f"Procedures/{procedure.name}/{step.name}", step)
    seen: dict[str, None] = {}
    for path in paths:
        seen.setdefault(path, None)
    return list(seen)


def method_paths(profile: ModuleProfile) -> list[str]:
    """Every method of a module, as paths below the root object."""
    paths = ["Occupation/Occupy", "Occupation/Release"]
    paths += [f"Module/{command}" for command in MODULE_COMMANDS]
    for skill in profile.skills:
        if not skill.callable:
            continue
        paths += [f"Skills/{skill.name}/{command}" for command in SKILL_COMMANDS]
    return paths


def browse_path(path: str, namespace: int = NAMESPACE_INDEX) -> list[str]:
    """Browse names for a path below the module root, namespaced."""
    return [f"{namespace}:{part}" for part in path.split("/") if part]


def root_browse_path(profile: ModuleProfile) -> list[str]:
    return [f"0:{NODES_FOLDER}", f"{NAMESPACE_INDEX}:{profile.root}"]