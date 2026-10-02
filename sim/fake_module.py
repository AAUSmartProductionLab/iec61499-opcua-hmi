"""Simulated OPC UA server that serves the address space described by a profile.

Development stand-in for the Eclipse 4diac controller: same nodes, same state
machines (MOD_StateLogic and SKILL_Control of iec61499-mgmt-py), same error
codes, and the equipment of cell/modules/filling.yaml and stoppering.yaml with
the kinematics of cell/sim/module_sim.py: axes with their travel time, dead time
and end switches, and a servo without feedback. The scale goes beyond the module
(which reads a constant): it weighs a vial that Tare zeroes and Dispensing fills.
Variables stay read-only for clients, and a method answers only when it is
called on the object it belongs to, exactly like the controller's OPC UA server
(open62541).
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
TICK = 0.02

FAULT_SENSOR = "sensor"        # end switches never close
FAULT_INVARIANT = "invariant"  # end switches never open again

# A step of a module level skill that finds its equipment held by another skill
# waits this long for it before it fails with Busy (SL_* WaitFree on the controller).
WAIT_FREE = 0.5
# stop_timeout of the module specs: Stopping waits this long for running skills.
STOP_TIMEOUT = 10.0


@dataclass(frozen=True)
class AxisSpec:
    """An axis of cell/modules/*.yaml (sim: axis): position 0..1, forward is +1."""

    travel_s: float
    dead_s: float = 0.0
    start: float = 0.0
    switches: dict[str, float] = field(default_factory=dict)


AXES: dict[str, AxisSpec] = {
    "NeedleAxis": AxisSpec(2.0, 0.15, 0.0, {"AtTop": 0.0, "AtBottom": 1.0}),
    "Piston": AxisSpec(3.0, 0.0, 0.5, {"AtLimit": 1.0}),
    "Plunger": AxisSpec(8.0),
}
# The scale has no hardware; the module reads a constant. The simulator weighs
# an empty vial instead, and while the needle is down in a dwell the pump stand-in
# fills it with 3 mL per second: about 3000 mg in the default 1 s, a little more
# or less for a medicine than for water.
VIAL_G = 9.75
FLOW_ML_PER_S = 3.0
DENSITY_G_PER_ML = (0.98, 1.06)


@dataclass(frozen=True)
class Behaviour:
    """What a skill primitive does (skills: in cell/modules/*.yaml)."""

    equipment: str = ""
    drive: int = 0                              # +1 forward, -1 backward on the axis
    boost: tuple[float, float] = (0.0, 1.0)     # start boost: seconds and speed factor
    ensures: str = ""                           # end switch that ends the run
    timeout: float = 8.0
    after: str | float | None = None            # duration parameter, or seconds
    invariant: bool = False                     # NOT (AtTop AND AtBottom) while it runs
    result: str = ""


# moveToTop/moveToBottom start at 190 for 200 ms, then run at 140 (the nominal speed).
NEEDLE_BOOST = (0.2, 190.0 / 140.0)

BEHAVIOURS: dict[str, Behaviour] = {
    "MoveNeedleUp": Behaviour("NeedleAxis", -1, NEEDLE_BOOST, "AtTop", invariant=True),
    "MoveNeedleDown": Behaviour("NeedleAxis", 1, NEEDLE_BOOST, "AtBottom", invariant=True),
    "AttachNeedle": Behaviour("NeedleAxis", 1, ensures="AtBottom"),
    "Dwell": Behaviour(after="Duration"),
    "Tare": Behaviour("Scale", after=2.0),
    "Weigh": Behaviour("Scale", after=0.2, result="Weight"),
    "LowerPiston": Behaviour("Piston", 1, ensures="AtLimit", timeout=10.0),
    "RaisePiston": Behaviour("Piston", -1, after="Duration"),
    "ExtendPlunger": Behaviour("Plunger", 1, after="Duration"),
    "RetractPlunger": Behaviour("Plunger", -1, after="Duration"),
    "MoveArm": Behaviour("StopperArm", after="Settle"),
}


@dataclass
class Axis:
    spec: AxisSpec
    position: float


@dataclass
class Run:
    """One run of a skill primitive: a standalone skill, a step or a procedure step."""

    skill: str
    params: dict[str, float]
    started: float
    ends_at: float = 0.0
    timeout_at: float = 0.0


@dataclass
class SkillRuntime:
    name: str
    state: int = model.SK_IDLE
    error_id: int = 0
    params: dict[str, float] = field(default_factory=dict)
    results: dict[str, float] = field(default_factory=dict)
    phase: str = ""          # module level skill: Execute, Stopping; Halted after a Stop
    step_index: int = 0
    run: Run | None = None
    done: bool = False       # the run ended at once (already at its end switch)
    fail_at: float = 0.0


@dataclass
class ProcedureRuntime:
    name: str
    active: bool = False
    step_index: int = 0
    run: Run | None = None
    done: bool = False


@dataclass
class StepRuntime:
    name: str
    state: int = model.SK_IDLE
    error_id: int = 0


class SimulatedModule:
    """State machines and simulated equipment of one module."""

    def __init__(self, profile: ModuleProfile) -> None:
        self.profile = profile
        self.module_state = model.STOPPED
        self.holder: str | None = None
        self.faults: set[str] = set()
        self.speed = 1.0
        self.clock = 0.0
        self.skills = {skill.name: SkillRuntime(skill.name) for skill in profile.skills}
        self.procedures = {proc.name: ProcedureRuntime(proc.name) for proc in profile.procedures}
        self.steps: dict[str, StepRuntime] = {}
        self.nodes: dict[str, Any] = {}
        self.types: dict[str, ua.VariantType] = {}
        self.pending_module_state: tuple[float, int] | None = None
        self.stopping_timeout = 0.0
        self.stopping_started = False
        sensors = {f"{sensor.equipment}/{sensor.name}" for sensor in profile.sensors}
        self.axes = {
            name: Axis(spec, spec.start)
            for name, spec in AXES.items()
            if name in {s.equipment for s in profile.sensors} or name in _used_equipment(profile)
        }
        self.inputs: dict[str, Any] = {key: False for key in sensors}
        self.gross_g = VIAL_G
        self.tare_g = 0.0
        self.density = 1.0
        if "Scale/Weight" in self.inputs:
            self._update_scale()
        self.published: dict[str, Any] = {}
        self.angles: dict[str, float] = {}
        self._update_switches(apply_faults=False)
        for skill in profile.skills:
            self.skills[skill.name].params = {param.name: param.default for param in skill.params}
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
                                     self.inputs[f"{name}/{sensor.name}"], variant_type)

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

    # --- equipment --------------------------------------------------------

    async def place(self, equipment: str, position: float) -> None:
        """Put an axis somewhere (a hand on the machine); its switches follow."""
        self.axes[equipment].position = min(1.0, max(0.0, position))
        self._update_switches(apply_faults=False)
        await self.publish_inputs()

    async def publish_inputs(self) -> None:
        for key, value in self.inputs.items():
            if self.published.get(key, KeyError) != value:
                self.published[key] = value
                await self.write(f"Equipment/{key}", value)

    def _runs(self) -> list[Run]:
        runs = [runtime.run for runtime in self.skills.values() if runtime.run]
        runs += [procedure.run for procedure in self.procedures.values() if procedure.active and procedure.run]
        return runs

    def _move_axes(self, dt: float) -> None:
        """Drive every axis by the runs that command it (module_sim.py Axis)."""
        now = self.clock
        for name, axis in self.axes.items():
            rate = 0.0
            for run in self._runs():
                behaviour = BEHAVIOURS[run.skill]
                if behaviour.equipment != name or not behaviour.drive:
                    continue
                elapsed = now - run.started
                if elapsed < axis.spec.dead_s:
                    continue
                boost_s, boost = behaviour.boost
                rate += behaviour.drive * (boost if elapsed < boost_s else 1.0)
            axis.position = min(1.0, max(0.0, axis.position + rate * dt / axis.spec.travel_s))
        self._update_switches(apply_faults=True)
        if "Scale/Weight" in self.inputs:
            dwelling = any(run.skill == "Dwell" for run in self._runs())
            if dwelling and self.inputs.get("NeedleAxis/AtBottom"):
                self.gross_g += FLOW_ML_PER_S * self.density * dt
            self._update_scale()

    def _update_scale(self) -> None:
        """The reading, to 1 mg."""
        self.inputs["Scale/Weight"] = round(self.gross_g - self.tare_g, 3)

    def _update_switches(self, apply_faults: bool) -> None:
        for name, axis in self.axes.items():
            for switch, at in axis.spec.switches.items():
                key = f"{name}/{switch}"
                if key not in self.inputs:
                    continue
                closed = axis.position >= at if at >= 0.5 else axis.position <= at
                old = bool(self.inputs[key])
                if apply_faults and FAULT_SENSOR in self.faults and closed and not old:
                    closed = False
                if apply_faults and FAULT_INVARIANT in self.faults and old and not closed:
                    closed = True
                self.inputs[key] = closed

    def _begin(self, skill: str, params: dict[str, float]) -> Run | None:
        """Start a run of a skill primitive; None when it is already at its end switch."""
        behaviour = BEHAVIOURS[skill]
        if behaviour.ensures and self.inputs.get(f"{behaviour.equipment}/{behaviour.ensures}"):
            return None
        now = self.clock
        run = Run(skill, dict(params), now, timeout_at=now + behaviour.timeout)
        if behaviour.after is not None:
            duration = params.get(behaviour.after, 0.0) if isinstance(behaviour.after, str) else behaviour.after
            run.ends_at = now + float(duration)
        if skill == "Dwell":
            self.density = random.uniform(*DENSITY_G_PER_ML)
        if "Angle" in params and behaviour.equipment:
            self.angles[behaviour.equipment] = params["Angle"]
        return run

    def _outcome(self, run: Run) -> int | None:
        """None while the run goes on, 0 when it is done, else the ErrorID it fails with."""
        behaviour = BEHAVIOURS[run.skill]
        if behaviour.invariant and self.inputs.get("NeedleAxis/AtTop") and self.inputs.get("NeedleAxis/AtBottom"):
            return model.ERR_INVARIANT
        if behaviour.ensures:
            if self.inputs.get(f"{behaviour.equipment}/{behaviour.ensures}"):
                return 0
            return model.ERR_TIMEOUT if self.clock >= run.timeout_at else None
        return 0 if self.clock >= run.ends_at else None

    def _result(self, skill: str) -> dict[str, float]:
        """Results of a run that is done (and what it leaves behind: Tare zeroes the scale)."""
        behaviour = BEHAVIOURS[skill]
        if skill == "Tare":
            self.tare_g = self.gross_g
            self._update_scale()
        if not behaviour.result:
            return {}
        return {behaviour.result: float(self.inputs.get(f"{behaviour.equipment}/{behaviour.result}", 0.0))}

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
        now = self.clock
        if command == "Reset":
            await self.set_module_state(model.RESETTING)
            await self._start_procedure("Resetting")
        elif command == "Start":
            self.pending_module_state = (now + 0.1, model.EXECUTE)
            await self.set_module_state(model.STARTING)
        elif command == "Stop":
            await self.set_module_state(model.STOPPING)
            # A skill started over OPC UA halts itself; the steps of a module level
            # skill or of a running Resetting procedure are left to their parent.
            for runtime in self.skills.values():
                if runtime.state == model.SK_RUNNING:
                    await self._halt(runtime)
            if self._any_active():
                self.stopping_timeout = now + STOP_TIMEOUT
            else:
                await self._begin_stopping_procedure(now)
        elif command == "Abort":
            await self._abort_everything(now)
        elif command == "Clear":
            self.pending_module_state = (now + 0.05, model.STOPPED)
            await self.set_module_state(model.CLEARING)
        return _answer(True, 0)

    def _any_active(self) -> bool:
        """Whether Stopping still waits: a skill runs, or the Resetting procedure does."""
        skills = any(r.state in (model.SK_RUNNING, model.SK_STOPPING) for r in self.skills.values())
        resetting = self.procedures.get("Resetting")
        return skills or bool(resetting and resetting.active)

    async def _begin_stopping_procedure(self, now: float) -> None:
        if self.stopping_started:
            return
        self.stopping_started = True
        self.stopping_timeout = 0.0
        if "Stopping" in self.procedures:
            await self._start_procedure("Stopping")
        else:
            self.pending_module_state = (now + 0.05, model.STOPPED)

    async def _abort_everything(self, now: float) -> None:
        self.pending_module_state = (now + 0.05, model.ABORTED)
        self.stopping_timeout = 0.0
        await self.set_module_state(model.ABORTING)
        for procedure in self.procedures.values():
            procedure.active, procedure.run = False, None
        for runtime in self.skills.values():
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
        runtime.run, runtime.done, runtime.fail_at, runtime.phase = None, False, 0.0, ""
        await self.set_skill_state(
            runtime.name, model.SK_ABORTED, model.ERR_INTERRUPTED if active else None
        )

    def _procedure(self, name: str) -> prof.Procedure | None:
        return next((item for item in self.profile.procedures if item.name == name), None)

    def _skill(self, name: str) -> prof.Skill:
        return next(item for item in self.profile.skills if item.name == name)

    async def _start_procedure(self, name: str) -> None:
        runtime = self.procedures[name]
        runtime.active, runtime.step_index = True, 0
        await self._run_procedure_step(runtime)

    async def _run_procedure_step(self, runtime: ProcedureRuntime) -> None:
        step = self._procedure(runtime.name).steps[runtime.step_index]
        runtime.run = self._begin(step.runs, {param.name: param.default for param in step.params})
        runtime.done = runtime.run is None
        await self.set_step_state(f"Procedures/{runtime.name}/{step.name}", model.SK_RUNNING)

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
            values = {param.name: float(value) for param, value in zip(skill.params, params, strict=False)}
            for param in skill.params:
                if not param.minimum <= values.get(param.name, param.default) <= param.maximum:
                    return _answer(False, model.ERR_OUT_OF_RANGE)
            # A primitive checks its equipment at Start; a module level skill
            # does not (its steps find out, and fail with Busy after a wait).
            if not skill.module_level and self._equipment_busy(skill.uses, name):
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
        return any(wanted & self._held(other) for other_name, other in self.skills.items() if other_name != name)

    async def _start_skill(self, runtime: SkillRuntime, values: dict[str, float]) -> None:
        skill = self._skill(runtime.name)
        runtime.params.update(values)
        runtime.fail_at = 0.0
        runtime.phase = "Execute"
        runtime.step_index = 0
        if values:
            for name, value in values.items():
                await self.write(f"Skills/{runtime.name}/Parameters/{name}", value)
        await self.set_skill_state(runtime.name, model.SK_RUNNING)
        if skill.module_level:
            await self._run_skill_step(runtime)
        else:
            runtime.run = self._begin(runtime.name, runtime.params)
            if runtime.run is None:
                # already at its end switch: done in the same event (AlreadyDone)
                await self._finish(runtime, 0, self._result(runtime.name))

    async def _run_skill_step(self, runtime: SkillRuntime) -> None:
        skill = self._skill(runtime.name)
        steps = skill.stop_steps if runtime.phase == "Stopping" else skill.steps
        step = steps[runtime.step_index]
        params = {param.name: param.default for param in step.params}
        runtime.run = self._begin(step.runs, params)
        runtime.done = runtime.run is None
        if runtime.phase == "Execute" and self._equipment_busy(step.uses, runtime.name):
            runtime.run, runtime.done = None, False
            runtime.fail_at = self.clock + WAIT_FREE
        await self.set_step_state(f"Skills/{runtime.name}/{runtime.phase}/{step.name}", model.SK_RUNNING)

    async def _halt(self, runtime: SkillRuntime) -> None:
        """Stop or module Stop: Running -> Stopping, then Failed with Interrupted (7).

        A module level skill halts the step that runs (it fails with 7) and runs
        its stop sequence, if it has one, while it shows Stopping.
        """
        skill = self._skill(runtime.name)
        runtime.run, runtime.done, runtime.fail_at = None, False, 0.0
        if skill.module_level and runtime.phase == "Execute" and runtime.step_index < len(skill.steps):
            key = f"Skills/{runtime.name}/Execute/{skill.steps[runtime.step_index].name}"
            await self.set_step_state(key, model.SK_FAILED, model.ERR_INTERRUPTED)
        await self.set_skill_state(runtime.name, model.SK_STOPPING)
        if skill.stop_steps:
            runtime.phase, runtime.step_index = "Stopping", 0
            await self._run_skill_step(runtime)
        else:
            runtime.phase = "Halted"

    async def _finish(self, runtime: SkillRuntime, error_id: int, results: dict[str, float]) -> None:
        """End a skill: Succeeded with its results, or Failed with error_id."""
        runtime.run, runtime.done, runtime.fail_at, runtime.phase = None, False, 0.0, ""
        if error_id:
            await self.set_skill_state(runtime.name, model.SK_FAILED, error_id)
            return
        for name, value in results.items():
            await self.write(f"Skills/{runtime.name}/Results/{name}", value)
        await self.set_skill_state(runtime.name, model.SK_SUCCEEDED)

    # --- simulation loop --------------------------------------------------

    def tick(self, dt: float) -> list[Awaitable[None]]:
        self.clock += dt * self.speed
        work: list[Awaitable[None]] = []
        if self.pending_module_state is not None and self.clock >= self.pending_module_state[0]:
            target = self.pending_module_state[1]
            self.pending_module_state = None
            work.append(self.set_module_state(target))
        self._move_axes(dt * self.speed)
        work.append(self.publish_inputs())
        for runtime in self.skills.values():
            if runtime.state in (model.SK_RUNNING, model.SK_STOPPING):
                work.append(self._tick_skill(runtime))
        for procedure in self.procedures.values():
            if procedure.active:
                work.append(self._tick_procedure(procedure))
        work.append(self._tick_module())
        return work

    async def _tick_skill(self, runtime: SkillRuntime) -> None:
        skill = self._skill(runtime.name)
        if runtime.phase == "Halted":
            # the brake command went out: the halted skill reports Interrupted
            await self._finish(runtime, model.ERR_INTERRUPTED, {})
            return
        if runtime.fail_at:
            if self.clock >= runtime.fail_at:
                key = f"Skills/{runtime.name}/Execute/{skill.steps[runtime.step_index].name}"
                await self.set_step_state(key, model.SK_FAILED, model.ERR_BUSY)
                await self._finish(runtime, model.ERR_BUSY, {})
            return
        outcome = 0 if runtime.done else (self._outcome(runtime.run) if runtime.run else None)
        if outcome is None:
            return
        if not skill.module_level:
            await self._finish(runtime, outcome, self._result(runtime.name) if not outcome else {})
            return
        steps = skill.stop_steps if runtime.phase == "Stopping" else skill.steps
        step = steps[runtime.step_index]
        key = f"Skills/{runtime.name}/{runtime.phase}/{step.name}"
        result = self._result(step.runs) if not outcome else {}
        if outcome:
            await self.set_step_state(key, model.SK_FAILED, outcome)
            # a failed stop sequence still ends the skill as stopped
            await self._finish(runtime, model.ERR_INTERRUPTED if runtime.phase == "Stopping" else outcome, {})
            return
        for name, value in result.items():
            await self.write(f"{key}/Results/{name}", value)
            runtime.results[name] = value
        await self.set_step_state(key, model.SK_SUCCEEDED)
        runtime.step_index += 1
        if runtime.step_index < len(steps):
            await self._run_skill_step(runtime)
        elif runtime.phase == "Stopping":
            await self._finish(runtime, model.ERR_INTERRUPTED, {})
        else:
            results = {r.name: runtime.results.get(r.name, 0.0) for r in skill.results}
            await self._finish(runtime, 0, results)

    async def _tick_procedure(self, runtime: ProcedureRuntime) -> None:
        outcome = 0 if runtime.done else (self._outcome(runtime.run) if runtime.run else None)
        if outcome is None:
            return
        procedure = self._procedure(runtime.name)
        key = f"Procedures/{runtime.name}/{procedure.steps[runtime.step_index].name}"
        runtime.run, runtime.done = None, False
        if outcome:
            # RESETTING_FAILED / STOPPING_FAILED: the module aborts
            runtime.active = False
            await self.set_step_state(key, model.SK_FAILED, outcome)
            if self.module_state in (model.RESETTING, model.STOPPING):
                await self._abort_everything(self.clock)
            return
        await self.set_step_state(key, model.SK_SUCCEEDED)
        runtime.step_index += 1
        if runtime.step_index < len(procedure.steps):
            await self._run_procedure_step(runtime)
            return
        runtime.active = False
        if runtime.name == "Resetting" and self.module_state == model.RESETTING:
            await self.set_module_state(model.IDLE)
        elif runtime.name == "Stopping" and self.module_state == model.STOPPING:
            self.pending_module_state = (self.clock + 0.05, model.STOPPED)

    async def _tick_module(self) -> None:
        if self.module_state != model.STOPPING or self.stopping_started:
            return
        if not self._any_active():
            await self._begin_stopping_procedure(self.clock)
        elif self.stopping_timeout and self.clock > self.stopping_timeout:
            await self._abort_everything(self.clock)


def _answer(accepted: bool, error_id: int) -> list[ua.Variant]:
    return [
        ua.Variant(accepted, ua.VariantType.Boolean),
        ua.Variant(error_id, ua.VariantType.UInt16),
    ]

def _used_equipment(profile: ModuleProfile) -> set[str]:
    used = {item for skill in profile.skills for item in skill.uses}
    for procedure in profile.procedures:
        used.update(item for step in procedure.steps for item in step.uses)
    return used


def _value(argument: Any) -> Any:
    return argument.Value if isinstance(argument, ua.Variant) else argument




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
        last = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            dt, last = now - last, now
            for module in self.modules.values():
                for work in module.tick(dt):
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