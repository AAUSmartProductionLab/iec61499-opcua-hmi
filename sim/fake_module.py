"""Simulated OPC UA server that serves the address space described by a profile.

Development stand-in for the Eclipse 4diac controller: same nodes, same state
machines (MOD_StateLogic and SKILL_Control of iec61499-mgmt-py), same error
codes, simulated equipment and realistic motion times. Variables stay read-only
for clients, and a method answers only when it is called on the object it
belongs to, exactly like the controller's OPC UA server (open62541).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from asyncua import Server, ua

from hmi import model
from hmi import profiles as prof
from hmi.profiles import ModuleProfile

LOGGER = logging.getLogger(__name__)

NS = prof.NAMESPACE_INDEX
TICK = 0.05

FAULT_SENSOR = "sensor"
FAULT_INVARIANT = "invariant"

# A step of a module level skill that finds its equipment held by another skill
# waits this long for it before it fails with Busy (SL_* WaitFree on the controller).
WAIT_FREE = 0.5

# The scale has no hardware: the ESP32 publishes a new weight about once a second.
SCALE_PERIOD = 1.0


@dataclass(frozen=True)
class Behaviour:
    kind: str
    equipment: tuple[str, ...] = ()
    sensor: tuple[str, str] | None = None
    sensor_value: bool = False
    other_sensor: tuple[str, str] | None = None
    motion: float = 1.5
    timeout: float = 8.0
    duration_param: str = ""
    default_duration: float = 1.0
    speed: float = 1.0
    result: str = ""
    tares: bool = False


BEHAVIOURS: dict[str, Behaviour] = {
    "LowerPiston": Behaviour(
        "sensor", ("Piston",), ("Piston", "AtLimit"), True, motion=1.5, timeout=10.0
    ),
    "MoveNeedleUp": Behaviour(
        "sensor", ("NeedleAxis",), ("NeedleAxis", "AtTop"), True,
        other_sensor=("NeedleAxis", "AtBottom"), motion=1.2, timeout=8.0,
    ),
    "MoveNeedleDown": Behaviour(
        "sensor", ("NeedleAxis",), ("NeedleAxis", "AtBottom"), True,
        other_sensor=("NeedleAxis", "AtTop"), motion=1.2, timeout=8.0,
    ),
    "AttachNeedle": Behaviour(
        "sensor", ("NeedleAxis",), ("NeedleAxis", "AtBottom"), True,
        other_sensor=("NeedleAxis", "AtTop"), motion=1.6, timeout=8.0,
    ),
    "Tare": Behaviour(
        "duration", ("Scale",), duration_param="Duration", default_duration=2.0, tares=True
    ),
    "Weigh": Behaviour(
        "duration", ("Scale",), duration_param="Duration", default_duration=0.2,
        result="Weight", speed=1.0,
    ),
    "RaisePiston": Behaviour("duration", ("Piston",), duration_param="Duration", default_duration=2.0),
    "ExtendPlunger": Behaviour(
        "duration", ("Plunger",), duration_param="Duration", default_duration=10.0, speed=1.5
    ),
    "RetractPlunger": Behaviour(
        "duration", ("Plunger",), duration_param="Duration", default_duration=6.5, speed=1.5
    ),
    "MoveArm": Behaviour(
        "duration", ("StopperArm",), duration_param="Settle", default_duration=2.0, speed=1.0
    ),
    "Dwell": Behaviour("duration", (), duration_param="Duration", default_duration=1.0),
}


@dataclass
class SkillRuntime:
    name: str
    state: int = model.SK_IDLE
    error_id: int = 0
    params: dict[str, float] = field(default_factory=dict)
    results: dict[str, float] = field(default_factory=dict)
    equipment: tuple[str, ...] = ()
    phase: str = ""
    step_index: int = 0
    started_at: float = 0.0
    deadline: float = 0.0
    timeout_at: float = 0.0
    fail_at: float = 0.0
    stop_requested: bool = False


@dataclass
class ProcedureRuntime:
    name: str
    active: bool = False
    step_index: int = 0
    deadline: float = 0.0


@dataclass
class StepRuntime:
    name: str
    state: int = model.SK_IDLE
    error_id: int = 0
    deadline: float = 0.0


class SimulatedModule:
    """State machines and simulated equipment of one module."""

    def __init__(self, profile: ModuleProfile) -> None:
        self.profile = profile
        self.module_state = model.STOPPED
        self.holder: str | None = None
        self.faults: set[str] = set()
        self.speed = 1.0
        self.skills = {skill.name: SkillRuntime(skill.name) for skill in profile.skills}
        self.procedures = {proc.name: ProcedureRuntime(proc.name) for proc in profile.procedures}
        self.steps: dict[str, StepRuntime] = {}
        self.nodes: dict[str, Any] = {}
        self.types: dict[str, ua.VariantType] = {}
        self.equipment: dict[str, Any] = {}
        self.pending_module_state: tuple[float, int] | None = None
        self.stopping_timeout = 0.0
        self.stopping_started = False
        self.weight = 0.0
        self.tared = False
        self.next_weight_change = 0.0
        for sensor in profile.sensors:
            self.equipment[f"{sensor.equipment}/{sensor.name}"] = _initial_sensor(sensor)
        for skill in profile.skills:
            runtime = self.skills[skill.name]
            runtime.params = {param.name: param.default for param in skill.params}
            runtime.results = {result.name: 0.0 for result in skill.results}
            runtime.equipment = _equipment_of(skill)
            for group, steps in (("Execute", skill.steps), ("Stopping", skill.stop_steps)):
                for step in steps:
                    self.steps[f"Skills/{skill.name}/{group}/{step.name}"] = StepRuntime(step.name)
        for procedure in profile.procedures:
            for step in procedure.steps:
                self.steps[f"Procedures/{procedure.name}/{step.name}"] = StepRuntime(step.name)

    # --- address space ----------------------------------------------------

    async def build(self, server: Server) -> None:
        profile = self.profile
        root = await server.nodes.objects.add_object(
            ua.NodeId(profile.root, NS), ua.QualifiedName(profile.root, NS)
        )
        occupation = await self._object(root, profile.root, "Occupation")
        await self._variable(occupation, profile.root + "/Occupation", "Occupied",
                             self.holder is not None, ua.VariantType.Boolean)
        await self._method(occupation, f"{profile.root}/Occupation", "Occupy",
                           self._on_occupy, [ua.VariantType.String])
        await self._method(occupation, f"{profile.root}/Occupation", "Release",
                           self._on_release, [ua.VariantType.String])

        module = await self._object(root, profile.root, "Module")
        await self._variable(module, f"{profile.root}/Module", "State",
                             self.module_state, ua.VariantType.Byte)
        for command in model.MODULE_COMMANDS:
            await self._method(module, f"{profile.root}/Module", command,
                               _module_handler(self, command), [ua.VariantType.String])

        equipment = await self._object(root, profile.root, "Equipment")
        for name in sorted({sensor.equipment for sensor in profile.sensors}):
            folder = await self._object(equipment, f"{profile.root}/Equipment", name)
            for sensor in profile.sensors:
                if sensor.equipment != name:
                    continue
                variant_type = ua.VariantType.Boolean if sensor.kind == "bool" else ua.VariantType.Double
                await self._variable(folder, f"{profile.root}/Equipment/{name}", sensor.name,
                                     self.equipment[f"{name}/{sensor.name}"], variant_type)

        skills = await self._object(root, profile.root, "Skills")
        for skill in profile.skills:
            folder = await self._object(skills, f"{profile.root}/Skills", skill.name)
            path = f"{profile.root}/Skills/{skill.name}"
            await self._variable(folder, path, "State", model.SK_IDLE, ua.VariantType.Byte)
            await self._variable(folder, path, "ErrorID", 0, ua.VariantType.UInt16)
            inputs = [ua.VariantType.String] + [ua.VariantType.Double] * len(skill.params)
            await self._method(folder, path, "Start", _skill_handler(self, skill.name, "Start"), inputs)
            for command in ("Stop", "Abort", "Reset"):
                await self._method(folder, path, command,
                                   _skill_handler(self, skill.name, command), [ua.VariantType.String])
            if skill.params:
                params_node = await self._object(folder, path, "Parameters")
                for param in skill.params:
                    await self._variable(params_node, f"{path}/Parameters", param.name,
                                         param.default, ua.VariantType.Double)
            if skill.results:
                results_node = await self._object(folder, path, "Results")
                for result in skill.results:
                    await self._variable(results_node, f"{path}/Results", result.name, 0.0,
                                         ua.VariantType.Double)
            for group, steps in (("Execute", skill.steps), ("Stopping", skill.stop_steps)):
                if not steps:
                    continue
                group_node = await self._object(folder, f"{path}/{group}", group)
                for step in steps:
                    step_path = f"{path}/{group}/{step.name}"
                    step_node = await self._object(group_node, f"{path}/{group}", step.name)
                    await self._variable(step_node, step_path, "State", model.SK_IDLE, ua.VariantType.Byte)
                    await self._variable(step_node, step_path, "ErrorID", 0, ua.VariantType.UInt16)
                    if step.params:
                        node = await self._object(step_node, step_path, "Parameters")
                        for param in step.params:
                            await self._variable(node, f"{step_path}/Parameters", param.name,
                                                 param.default, ua.VariantType.Double)
                    if step.results:
                        node = await self._object(step_node, step_path, "Results")
                        for result in step.results:
                            await self._variable(node, f"{step_path}/Results", result.name, 0.0,
                                                 ua.VariantType.Double)

        procedures = await self._object(root, profile.root, "Procedures")
        for procedure in profile.procedures:
            folder = await self._object(procedures, f"{profile.root}/Procedures", procedure.name)
            for step in procedure.steps:
                step_path = f"{profile.root}/Procedures/{procedure.name}/{step.name}"
                step_node = await self._object(folder, f"{profile.root}/Procedures/{procedure.name}",
                                               step.name)
                await self._variable(step_node, step_path, "State", model.SK_IDLE, ua.VariantType.Byte)
                await self._variable(step_node, step_path, "ErrorID", 0, ua.VariantType.UInt16)
                if step.params:
                    node = await self._object(step_node, step_path, "Parameters")
                    for param in step.params:
                        await self._variable(node, f"{step_path}/Parameters", param.name,
                                             param.default, ua.VariantType.Double)

    async def _object(self, parent: Any, path: str, name: str) -> Any:
        return await parent.add_object(ua.NodeId(f"{path}/{name}", NS), ua.QualifiedName(name, NS))

    async def _variable(
        self, parent: Any, path: str, name: str, value: Any, variant_type: ua.VariantType
    ) -> Any:
        node = await parent.add_variable(
            ua.NodeId(f"{path}/{name}", NS), ua.QualifiedName(name, NS), value, variant_type
        )
        relative = path.split(f"{self.profile.root}/", 1)[-1]
        key = f"{relative}/{name}" if relative else name
        self.nodes[key] = node
        self.types[key] = variant_type
        return node

    async def _method(
        self,
        parent: Any,
        path: str,
        name: str,
        callback: Callable[..., Awaitable[list[ua.Variant]]],
        inputs: list[ua.VariantType],
    ) -> None:
        owner = parent.nodeid

        async def strict(object_id: Any, *args: Any) -> Any:
            # open62541 checks that the object owns the method; asyncua does not.
            if object_id != owner:
                return ua.StatusCode(ua.StatusCodes.BadNodeClassInvalid)
            return await callback(object_id, *args)

        await parent.add_method(
            ua.NodeId(f"{path}/{name}", NS),
            ua.QualifiedName(name, NS),
            strict,
            [ua.Argument(f"RD_{index + 1}", variant) for index, variant in enumerate(inputs)],
            [
                ua.Argument("SD_1", ua.VariantType.Boolean),
                ua.Argument("SD_2", ua.VariantType.UInt16),
            ],
        )

    # --- writes -----------------------------------------------------------

    async def write(self, key: str, value: Any) -> None:
        node = self.nodes.get(key)
        if node is None:
            return
        variant_type = self.types.get(key, ua.VariantType.Double)
        await node.write_value(ua.DataValue(ua.Variant(value, variant_type)))

    async def set_module_state(self, state: int) -> None:
        self.module_state = state
        if state != model.STOPPING:
            self.stopping_started = False
        await self.write("Module/State", state)
        # Every skill instance follows the module state (SKILL_Control MOD_CHG):
        # Clearing or Stopped brings an aborted skill or step back to Idle.
        if state in (model.CLEARING, model.STOPPED):
            for name, runtime in self.skills.items():
                if runtime.state == model.SK_ABORTED:
                    await self.set_skill_state(name, model.SK_IDLE)
            for key, step in self.steps.items():
                if step.state == model.SK_ABORTED:
                    await self.set_step_state(key, model.SK_IDLE, step.error_id)

    async def set_skill_state(self, name: str, state: int, error_id: int | None = None) -> None:
        runtime = self.skills[name]
        runtime.state = state
        # Like SKILL_Control: Start and success clear ErrorID, a failure sets it;
        # Idle and an abort of an inactive skill keep the last value.
        if error_id is not None:
            runtime.error_id = error_id
        elif state in (model.SK_RUNNING, model.SK_SUCCEEDED):
            runtime.error_id = 0
        await self.write(f"Skills/{name}/State", state)
        await self.write(f"Skills/{name}/ErrorID", runtime.error_id)

    async def set_step_state(self, key: str, state: int, error_id: int = 0) -> None:
        step = self.steps[key]
        step.state = state
        step.error_id = error_id
        await self.write(f"{key}/State", state)
        await self.write(f"{key}/ErrorID", error_id)

    async def set_equipment(self, equipment: str, sensor: str, value: Any) -> None:
        self.equipment[f"{equipment}/{sensor}"] = value
        await self.write(f"Equipment/{equipment}/{sensor}", value)

    async def set_skill_result(self, skill: str, name: str, value: float) -> None:
        self.skills[skill].results[name] = value
        await self.write(f"Skills/{skill}/Results/{name}", value)

    # --- occupation -------------------------------------------------------

    def _not_permitted(self, session: str) -> bool:
        return not session or self.holder != session

    async def _on_occupy(self, parent: Any, session: Any) -> list[ua.Variant]:
        session = str(_value(session))
        if not session or (self.holder is not None and self.holder != session):
            return _answer(False, model.ERR_NOT_PERMITTED)
        self.holder = session
        await self.write("Occupation/Occupied", True)
        return _answer(True, 0)

    async def _on_release(self, parent: Any, session: Any) -> list[ua.Variant]:
        if self._not_permitted(str(_value(session))):
            return _answer(False, model.ERR_NOT_PERMITTED)
        self.holder = None
        await self.write("Occupation/Occupied", False)
        return _answer(True, 0)

    # --- module commands --------------------------------------------------

    async def on_module_command(self, command: str, session: str) -> list[ua.Variant]:
        if self._not_permitted(session):
            return _answer(False, model.ERR_NOT_PERMITTED)
        if self.module_state not in model.MODULE_COMMAND_FROM_STATES[command]:
            return _answer(False, model.ERR_NOT_READY)
        now = time.monotonic()
        if command == "Reset":
            await self.set_module_state(model.RESETTING)
            await self._start_procedure("Resetting")
        elif command == "Start":
            self.pending_module_state = (now + 0.1, model.EXECUTE)
            await self.set_module_state(model.STARTING)
        elif command == "Stop":
            await self.set_module_state(model.STOPPING)
            # module Stop halts every running skill instance, the steps of a
            # Resetting procedure that is still going included
            for runtime in self.skills.values():
                if runtime.state == model.SK_RUNNING:
                    await self._halt(runtime)
            resetting = self.procedures.get("Resetting")
            if resetting is not None and resetting.active:
                resetting.active = False
                for key, step in self.steps.items():
                    if key.startswith("Procedures/Resetting/") and step.state == model.SK_RUNNING:
                        await self.set_step_state(key, model.SK_FAILED, model.ERR_INTERRUPTED)
            if not self._any_skill_active():
                await self._begin_stopping_procedure(now)
            else:
                self.stopping_timeout = now + 10.0
        elif command == "Abort":
            await self._abort_everything(now)
        elif command == "Clear":
            self.pending_module_state = (now + 0.05, model.STOPPED)
            await self.set_module_state(model.CLEARING)
        return _answer(True, 0)

    def _any_skill_active(self) -> bool:
        return any(
            runtime.state in (model.SK_RUNNING, model.SK_STOPPING)
            for runtime in self.skills.values()
        )

    async def _begin_stopping_procedure(self, now: float) -> None:
        if self.stopping_started:
            return
        self.stopping_started = True
        if self._procedure("Stopping") is not None:
            await self._start_procedure("Stopping")
        else:
            self.pending_module_state = (now + 0.05, model.STOPPED)

    async def _abort_everything(self, now: float) -> None:
        self.pending_module_state = (now + 0.05, model.ABORTED)
        await self.set_module_state(model.ABORTING)
        for procedure in self.procedures.values():
            procedure.active = False
        for name, runtime in self.skills.items():
            if runtime.state != model.SK_ABORTED:
                await self._abort_skill(runtime)
        # The steps of sequences and procedures are skill instances as well.
        for key, step in self.steps.items():
            if step.state != model.SK_ABORTED:
                active = step.state in (model.SK_RUNNING, model.SK_STOPPING)
                await self.set_step_state(
                    key, model.SK_ABORTED, model.ERR_INTERRUPTED if active else step.error_id
                )

    async def _abort_skill(self, runtime: SkillRuntime) -> None:
        active = runtime.state in (model.SK_RUNNING, model.SK_STOPPING)
        runtime.stop_requested = True
        runtime.fail_at = 0.0
        await self.set_skill_state(
            runtime.name, model.SK_ABORTED, model.ERR_INTERRUPTED if active else None
        )

    def _procedure(self, name: str) -> prof.Procedure | None:
        return next((item for item in self.profile.procedures if item.name == name), None)

    def _skill(self, name: str) -> prof.Skill:
        return next(item for item in self.profile.skills if item.name == name)

    async def _start_procedure(self, name: str) -> None:
        runtime = self.procedures[name]
        runtime.active = True
        runtime.step_index = 0
        await self._run_procedure_step(runtime)

    async def _run_procedure_step(self, runtime: ProcedureRuntime) -> None:
        procedure = self._procedure(runtime.name)
        if procedure is None or runtime.step_index >= len(procedure.steps):
            runtime.active = False
            return
        step = procedure.steps[runtime.step_index]
        key = f"Procedures/{runtime.name}/{step.name}"
        params = {param.name: param.default for param in step.params}
        runtime.deadline = time.monotonic() + _duration(BEHAVIOURS.get(step.runs), params, self.speed)
        self.steps[key].deadline = runtime.deadline
        await self.set_step_state(key, model.SK_RUNNING)

    # --- skill commands ---------------------------------------------------

    async def on_skill_command(
        self, name: str, command: str, session: str, params: list[float]
    ) -> list[ua.Variant]:
        skill = self._skill(name)
        runtime = self.skills[name]
        if self._not_permitted(session):
            return _answer(False, model.ERR_NOT_PERMITTED)
        if command == "Start":
            if self.module_state != model.EXECUTE:
                return _answer(False, model.ERR_NOT_READY)
            if runtime.state in (model.SK_RUNNING, model.SK_STOPPING):
                return _answer(False, model.ERR_BUSY)
            if runtime.state == model.SK_ABORTED:
                return _answer(False, model.ERR_NOT_READY)
            values: dict[str, float] = {}
            for param, value in zip(skill.params, params, strict=False):
                values[param.name] = float(value)
            for param in skill.params:
                value = values.get(param.name, param.default)
                if not param.minimum <= value <= param.maximum:
                    return _answer(False, model.ERR_OUT_OF_RANGE)
            # A primitive checks its equipment at Start; a module level skill
            # does not (its steps find out, and fail with Busy after a wait).
            if not skill.module_level and self._equipment_busy(runtime.equipment, name):
                return _answer(False, model.ERR_BUSY)
            await self._start_skill(runtime, values)
            return _answer(True, 0)
        if command == "Stop":
            if runtime.state != model.SK_RUNNING:
                return _answer(False, model.ERR_NOT_READY)
            await self._halt(runtime)
            return _answer(True, 0)
        if command == "Abort":
            if runtime.state == model.SK_ABORTED:
                return _answer(False, model.ERR_NOT_READY)
            await self._abort_skill(runtime)
            return _answer(True, 0)
        if command == "Reset":
            if runtime.state != model.SK_ABORTED:
                return _answer(False, model.ERR_NOT_READY)
            await self.set_skill_state(name, model.SK_IDLE)
            return _answer(True, 0)
        return _answer(False, model.ERR_NOT_READY)

    def _held(self, runtime: SkillRuntime) -> set[str]:
        """Equipment an active skill holds (see profiles.held_equipment)."""
        if runtime.state not in (model.SK_RUNNING, model.SK_STOPPING):
            return set()
        return prof.held_equipment(self._skill(runtime.name), runtime.phase or "Execute", runtime.step_index)

    def _equipment_busy(self, equipment: Iterable[str], name: str) -> bool:
        wanted = set(equipment)
        return any(
            wanted & self._held(other)
            for other_name, other in self.skills.items()
            if other_name != name
        )

    async def _halt(self, runtime: SkillRuntime) -> None:
        """Stop or module Stop: Running -> Stopping, then Failed with Interrupted (7).

        A module level skill halts the step that runs (it fails with 7) and runs
        its stop sequence, if it has one, while it shows Stopping.
        """
        skill = self._skill(runtime.name)
        runtime.stop_requested = True
        runtime.fail_at = 0.0
        if skill.module_level and runtime.phase == "Execute" and runtime.step_index < len(skill.steps):
            key = f"Skills/{runtime.name}/Execute/{skill.steps[runtime.step_index].name}"
            await self.set_step_state(key, model.SK_FAILED, model.ERR_INTERRUPTED)
        await self.set_skill_state(runtime.name, model.SK_STOPPING)
        if skill.stop_steps:
            runtime.phase = "Stopping"
            runtime.step_index = 0
            await self._run_skill_step(runtime)
        else:
            runtime.phase = "Halted"

    async def _start_skill(self, runtime: SkillRuntime, values: dict[str, float]) -> None:
        skill = self._skill(runtime.name)
        runtime.params.update(values)
        runtime.stop_requested = False
        runtime.fail_at = 0.0
        runtime.phase = "Execute"
        runtime.step_index = 0
        now = time.monotonic()
        if skill.module_level:
            await self.set_skill_state(runtime.name, model.SK_RUNNING)
            await self._run_skill_step(runtime)
            return
        behaviour = BEHAVIOURS.get(runtime.name)
        if behaviour is None:
            runtime.deadline = now + 1.0
            runtime.timeout_at = now + 8.0 / max(self.speed, 0.01)
            await self.set_skill_state(runtime.name, model.SK_RUNNING)
            return
        if behaviour.kind == "sensor" and self._sensor_reached(behaviour):
            await self.set_skill_state(runtime.name, model.SK_SUCCEEDED)
            return
        runtime.started_at = now
        runtime.deadline = now + behaviour.motion / max(behaviour.speed * self.speed, 0.1)
        # slow motion needs a longer timeout, not a shorter one
        runtime.timeout_at = now + behaviour.timeout / max(self.speed, 0.01)
        await self.set_skill_state(runtime.name, model.SK_RUNNING)

    async def _run_skill_step(self, runtime: SkillRuntime) -> None:
        skill = self._skill(runtime.name)
        steps = skill.stop_steps if runtime.phase == "Stopping" else skill.steps
        if runtime.step_index >= len(steps):
            return
        step = steps[runtime.step_index]
        key = f"Skills/{runtime.name}/{runtime.phase}/{step.name}"
        params = {param.name: runtime.params.get(param.name, param.default) for param in step.params}
        behaviour = BEHAVIOURS.get(step.runs)
        now = time.monotonic()
        runtime.deadline = now + _duration(behaviour, params, self.speed)
        runtime.timeout_at = runtime.deadline + (behaviour.timeout if behaviour else 8.0) / max(self.speed, 0.01)
        if runtime.phase == "Execute" and self._equipment_busy(step.uses, runtime.name):
            runtime.fail_at = now + WAIT_FREE
        self.steps[key].deadline = runtime.deadline
        await self.set_step_state(key, model.SK_RUNNING)

    def _sensor_reached(self, behaviour: Behaviour) -> bool:
        if behaviour.sensor is None:
            return True
        equipment, sensor = behaviour.sensor
        return bool(self.equipment.get(f"{equipment}/{sensor}")) == behaviour.sensor_value

    async def _skill_succeeded(self, runtime: SkillRuntime) -> None:
        skill = self._skill(runtime.name)
        for result in skill.results:
            await self.set_skill_result(runtime.name, result.name, self.weight)
            step_name = _result_step(skill, result.name)
            if step_name:
                key = f"Skills/{runtime.name}/Execute/{step_name}/Results/{result.name}"
                await self.write(key, self.weight)
        await self.set_skill_state(runtime.name, model.SK_SUCCEEDED)

    async def _finish_step(self, runtime: SkillRuntime, behaviour: Behaviour | None) -> None:
        if behaviour is None:
            return
        if behaviour.sensor is not None and FAULT_SENSOR not in self.faults:
            equipment, sensor = behaviour.sensor
            await self.set_equipment(equipment, sensor, behaviour.sensor_value)
        if behaviour.other_sensor is not None:
            # one axis has one position: the opposite end switch must go off
            equipment, sensor = behaviour.other_sensor
            await self.set_equipment(equipment, sensor, False)
        if behaviour.tares:
            self.tared = True
            self.weight = 0.0
            await self.set_equipment("Scale", "Weight", self.weight)
        if behaviour.result:
            step_name = _result_step(self._skill(runtime.name), behaviour.result)
            if step_name:
                key = f"Skills/{runtime.name}/{runtime.phase}/{step_name}/Results/{behaviour.result}"
                await self.write(key, self.weight)

    # --- simulation loop --------------------------------------------------

    def tick(self) -> list[Awaitable[None]]:
        now = time.monotonic()
        work: list[Awaitable[None]] = [self._tick_scale(now)]
        if self.pending_module_state is not None:
            due, target = self.pending_module_state
            if now >= due:
                self.pending_module_state = None
                work.append(self.set_module_state(target))
        for name, runtime in self.skills.items():
            work.extend(self._tick_skill(name, runtime, now))
        for procedure in self.procedures.values():
            work.extend(self._tick_procedure(procedure, now))
        return work

    async def _tick_scale(self, now: float) -> None:
        """The ESP32 publishes a fresh weight about once per second."""
        if "Scale/Weight" not in self.equipment:
            return
        if now < self.next_weight_change:
            return
        self.next_weight_change = now + SCALE_PERIOD
        if self.tared:
            self.weight = round(random.uniform(0.0, 1.5), 1)
        else:
            self.weight = round(random.uniform(0.0, 250.0), 1)
        await self.set_equipment("Scale", "Weight", self.weight)

    def _tick_skill(self, name: str, runtime: SkillRuntime, now: float) -> list[Awaitable[None]]:
        if runtime.state not in (model.SK_RUNNING, model.SK_STOPPING):
            return []
        skill = self._skill(name)
        work: list[Awaitable[None]] = []
        if runtime.phase == "Halted":
            # the brake command went out: the halted skill reports Interrupted
            runtime.phase = ""
            work.append(self.set_skill_state(name, model.SK_FAILED, model.ERR_INTERRUPTED))
            return work
        if runtime.state == model.SK_RUNNING and self._invariant_broken(name):
            runtime.stop_requested = True
            work.append(self._fail_skill(runtime, model.ERR_INVARIANT))
            return work
        if runtime.fail_at and now >= runtime.fail_at:
            runtime.fail_at = 0.0
            work.append(self._fail_skill(runtime, model.ERR_BUSY))
            return work
        if skill.module_level:
            work.extend(self._tick_sequence(runtime, now))
            return work
        behaviour = BEHAVIOURS.get(name)
        if now < runtime.deadline:
            return []
        if behaviour is not None and behaviour.kind == "sensor" and (
            FAULT_SENSOR in self.faults or now >= runtime.timeout_at
        ):
            work.append(self._fail_skill(runtime, model.ERR_TIMEOUT))
            return work
        work.append(self._finish_step(runtime, behaviour))
        work.append(self._skill_succeeded(runtime))
        return work

    def _tick_sequence(self, runtime: SkillRuntime, now: float) -> list[Awaitable[None]]:
        skill = self._skill(runtime.name)
        steps = skill.stop_steps if runtime.phase == "Stopping" else skill.steps
        work: list[Awaitable[None]] = []
        if now < runtime.deadline:
            return work
        if runtime.fail_at:
            return work
        step = steps[runtime.step_index]
        key = f"Skills/{runtime.name}/{runtime.phase}/{step.name}"
        behaviour = BEHAVIOURS.get(step.runs)
        if behaviour is not None and behaviour.kind == "sensor" and FAULT_SENSOR in self.faults:
            work.append(self._fail_skill(runtime, model.ERR_TIMEOUT))
            return work
        work.append(self._finish_step(runtime, behaviour))
        work.append(self.set_step_state(key, model.SK_SUCCEEDED))
        runtime.step_index += 1
        if runtime.step_index >= len(steps):
            if runtime.phase == "Stopping":
                work.append(self._fail_skill(runtime, model.ERR_INTERRUPTED))
            else:
                work.append(self._skill_succeeded(runtime))
            return work
        work.append(self._run_skill_step(runtime))
        return work

    async def _fail_skill(self, runtime: SkillRuntime, error_id: int) -> None:
        skill = self._skill(runtime.name)
        if skill.module_level:
            steps = skill.stop_steps if runtime.phase == "Stopping" else skill.steps
            if runtime.step_index < len(steps):
                key = f"Skills/{runtime.name}/{runtime.phase}/{steps[runtime.step_index].name}"
                if key in self.steps:
                    await self.set_step_state(key, model.SK_FAILED, error_id)
        runtime.phase = ""
        await self.set_skill_state(runtime.name, model.SK_FAILED, error_id)

    def _invariant_broken(self, name: str) -> bool:
        if name not in ("MoveNeedleUp", "MoveNeedleDown"):
            return False
        if FAULT_INVARIANT not in self.faults:
            return False
        return bool(self.equipment.get("NeedleAxis/AtTop")) and bool(
            self.equipment.get("NeedleAxis/AtBottom")
        )

    def _tick_procedure(self, runtime: ProcedureRuntime, now: float) -> list[Awaitable[None]]:
        if not runtime.active:
            return []
        procedure = self._procedure(runtime.name)
        if procedure is None or now < runtime.deadline:
            return []
        work: list[Awaitable[None]] = []
        key = f"Procedures/{runtime.name}/{procedure.steps[runtime.step_index].name}"
        work.append(self.set_step_state(key, model.SK_SUCCEEDED))
        runtime.step_index += 1
        if runtime.step_index >= len(procedure.steps):
            runtime.active = False
            if runtime.name == "Resetting" and self.module_state == model.RESETTING:
                work.append(self.set_module_state(model.IDLE))
            elif runtime.name == "Stopping" and self.module_state == model.STOPPING:
                self.pending_module_state = (now + 0.05, model.STOPPED)
        else:
            work.append(self._run_procedure_step(runtime))
        return work

    def tick_module(self, now: float) -> list[Awaitable[None]]:
        work: list[Awaitable[None]] = []
        if self.module_state == model.STOPPING and not self._any_skill_active():
            self.stopping_timeout = 0.0
            work.append(self._begin_stopping_procedure(now))
        if (
            self.module_state == model.STOPPING
            and self.stopping_timeout
            and now > self.stopping_timeout
        ):
            self.stopping_timeout = 0.0
            work.append(self._abort_everything(now))
        return work


def _answer(accepted: bool, error_id: int) -> list[ua.Variant]:
    return [
        ua.Variant(accepted, ua.VariantType.Boolean),
        ua.Variant(error_id, ua.VariantType.UInt16),
    ]


def _value(argument: Any) -> Any:
    return argument.Value if isinstance(argument, ua.Variant) else argument


def _initial_sensor(sensor: prof.Sensor) -> Any:
    if sensor.kind == "bool":
        return sensor.name == "AtTop"
    return 0.0


def _equipment_of(skill: prof.Skill) -> tuple[str, ...]:
    if skill.uses:
        return skill.uses
    equipment: list[str] = []
    for step in skill.steps:
        for item in step.uses:
            if item not in equipment:
                equipment.append(item)
    return tuple(equipment)


def _duration(behaviour: Behaviour | None, params: dict[str, float], speed: float = 1.0) -> float:
    if behaviour is None:
        return 1.0
    factor = max(behaviour.speed, 0.1) * max(speed, 0.01)
    if behaviour.kind == "sensor":
        return behaviour.motion / factor
    value = params.get(behaviour.duration_param, behaviour.default_duration)
    return float(value) / factor


def _result_step(skill: prof.Skill, result_name: str) -> str:
    for step in skill.steps:
        if any(result.name == result_name for result in step.results):
            return step.name
    return ""


def _module_handler(module: SimulatedModule, command: str) -> Any:
    async def handler(parent: Any, session: Any) -> list[ua.Variant]:
        LOGGER.info("Module/%s from %s in state %s", command, _value(session), module.module_state)
        return await module.on_module_command(command, str(_value(session)))

    return handler


def _skill_handler(module: SimulatedModule, name: str, command: str) -> Any:
    async def handler(parent: Any, *args: Any) -> list[ua.Variant]:
        values = [_value(arg) for arg in args]
        session = str(values[0]) if values else ""
        params = [float(value) for value in values[1:]]
        LOGGER.info(
            "Skills/%s/%s from %s in state %s",
            name, command, session, module.skills[name].state,
        )
        return await module.on_skill_command(name, command, session, params)

    return handler


class SimulatedServer:
    """Runs one or more simulated modules on one endpoint."""

    def __init__(self, endpoint: str, profiles: Iterable[ModuleProfile]) -> None:
        self.endpoint = endpoint
        self.modules = {profile.key: SimulatedModule(profile) for profile in profiles}
        self._server: Server | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("iec61499-opcua-hmi simulator")
        for module in self.modules.values():
            await module.build(server)
        await server.start()
        self._server = server
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        LOGGER.info("simulator serving %s", self.endpoint)

    async def _run(self) -> None:
        while not self._stop.is_set():
            for module in self.modules.values():
                for work in module.tick() + module.tick_module(time.monotonic()):
                    try:
                        await work
                    except Exception:  # noqa: BLE001, PERF203
                        LOGGER.exception("simulation step failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._server is not None:
            await self._server.stop()
            self._server = None


async def serve(endpoint: str, profiles: Iterable[ModuleProfile]) -> SimulatedServer:
    server = SimulatedServer(endpoint, list(profiles))
    await server.start()
    return server