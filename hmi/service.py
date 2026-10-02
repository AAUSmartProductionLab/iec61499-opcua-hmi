"""Application logic between the OPC UA links and the Flask app.

Keeps track of who occupies which module, logs refusals and failures, and
builds one snapshot per browser for the HMI page.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import model
from . import profiles as prof
from .link import CallResult, OpcuaLink
from .profiles import ModuleProfile, Param

LOGGER = logging.getLogger(__name__)

LOG_LIMIT = 500
WATCH_INTERVAL = 0.1
FAILURE_WAIT = 1.0


@dataclass(frozen=True)
class ModuleConfig:
    profile: ModuleProfile
    endpoint: str


@dataclass
class LogEntry:
    ts: float
    level: str
    module: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "time": time.strftime("%H:%M:%S", time.localtime(self.ts)),
            "level": self.level,
            "module": self.module,
            "text": self.text,
        }


@dataclass
class Operator:
    session: str
    occupies: dict[str, bool] = field(default_factory=dict)
    params: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    seen: float = field(default_factory=time.time)
    probed: set[str] = field(default_factory=set)

    def skill_params(self, module_key: str, skill: str, defaults: Iterable[Param]) -> dict[str, float]:
        module_params = self.params.setdefault(module_key, {})
        skill_params = module_params.setdefault(skill, {})
        return {param.name: float(skill_params.get(param.name, param.default)) for param in defaults}


class HmiService:
    def __init__(
        self,
        configs: Iterable[ModuleConfig],
        sampling_ms: int = 200,
        log_limit: int = LOG_LIMIT,
    ) -> None:
        self.configs: list[ModuleConfig] = list(configs)
        self.sampling_ms = sampling_ms
        self._log: deque[LogEntry] = deque(maxlen=log_limit)
        self._lock = threading.RLock()
        self._operators: dict[str, Operator] = {}
        self._last_states: dict[str, dict[str, Any]] = {}
        self._failures: dict[tuple[str, str], tuple[float, str]] = {}
        self._reassert_lock = threading.Lock()
        self._watch_stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self.links: dict[str, OpcuaLink] = {}
        for endpoint, module_configs in self._grouped().items():
            link = OpcuaLink(endpoint, [config.profile for config in module_configs], sampling_ms)
            link.add_state_listener(self._make_listener(endpoint))
            self.links[endpoint] = link
        self.modules: dict[str, ModuleConfig] = {config.profile.key: config for config in self.configs}

    def _grouped(self) -> dict[str, list[ModuleConfig]]:
        grouped: dict[str, list[ModuleConfig]] = {}
        for config in self.configs:
            grouped.setdefault(config.endpoint, []).append(config)
        return grouped

    def _make_listener(self, endpoint: str) -> Any:
        def listener(connected: bool) -> None:
            if connected:
                threading.Thread(
                    target=self._reassert_occupations,
                    args=(endpoint,),
                    name="reoccupy",
                    daemon=True,
                ).start()

        return listener

    def start(self) -> None:
        for link in self.links.values():
            link.start()
        if self._watcher is None and self.configs:
            self._watch_stop.clear()
            self._watcher = threading.Thread(target=self._watch_loop, name="transitions", daemon=True)
            self._watcher.start()

    def stop(self) -> None:
        self._watch_stop.set()
        if self._watcher is not None:
            self._watcher.join(timeout=2)
            self._watcher = None
        for link in self.links.values():
            link.stop()

    def _watch_loop(self) -> None:
        """Log transitions as they come, whether or not a page is open."""
        while not self._watch_stop.wait(WATCH_INTERVAL):
            for config in self.configs:
                channel = self.link_for(config.profile.key).channels[config.profile.key]
                values = {path: entry["value"] for path, entry in channel.snapshot().items()}
                try:
                    self._watch_transitions(config.profile, values)
                except Exception:  # noqa: BLE001
                    LOGGER.exception("transition watch failed")

    # --- operators -------------------------------------------------------

    def operator(self, session: str) -> Operator:
        with self._lock:
            operator = self._operators.get(session)
            if operator is None:
                operator = Operator(session=session)
                self._operators[session] = operator
            operator.seen = time.time()
            return operator

    def is_occupier(self, session: str, module_key: str) -> bool:
        with self._lock:
            operator = self._operators.get(session)
            return bool(operator and operator.occupies.get(module_key))

    def _reassert_occupations(self, endpoint: str) -> None:
        if not self._reassert_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                sessions = [
                    (operator.session, module_key)
                    for operator in self._operators.values()
                    for module_key, holds in operator.occupies.items()
                    if holds
                ]
                module_configs = self._grouped().get(endpoint, [])
            link = self.links.get(endpoint)
            if link is None:
                return
            keys = {config.profile.key for config in module_configs}
            for session, module_key in sessions:
                if module_key not in keys or not link.state.connected:
                    continue
                result = link.call(module_key, "Occupation/Occupy", [session])
                self._note_occupation_result(module_key, session, result, holds=True, reassert=True)
        finally:
            self._reassert_lock.release()

    def _probe_occupation(self, session: str, module_key: str) -> None:
        """Ask once whether this session still owns an occupied module.

        The controller publishes only Occupied, never the owner. After a restart
        of the HMI the browser comes back with its stored session id; Occupy is
        accepted only for the owner, so it tells without taking anything.
        """
        link = self.link_for(module_key)
        result = link.call(module_key, "Occupation/Occupy", [session])
        if not result.ok:
            with self._lock:
                operator = self._operators.get(session)
                if operator is not None:
                    operator.probed.discard(module_key)
            return
        with self._lock:
            operator = self._operators.get(session)
            if operator is not None:
                operator.occupies[module_key] = result.accepted
        if result.accepted:
            self.log(module_key, "info", "occupation of this session taken back")

    def _note_occupation_result(
        self,
        module_key: str,
        session: str,
        result: CallResult,
        holds: bool = True,
        reassert: bool = False,
    ) -> None:
        with self._lock:
            operator = self._operators.get(session)
            if operator is not None:
                if result.accepted:
                    operator.occupies[module_key] = holds
                elif result.error_id == model.ERR_NOT_PERMITTED:
                    operator.occupies[module_key] = False
        if not result.ok:
            self.log(module_key, "error", f"Occupation not sent: {result.transport_error}")
        elif result.accepted:
            if reassert:
                self.log(module_key, "info", "re-occupied after reconnect")
            elif holds:
                self.log(module_key, "info", "module occupied")
            else:
                self.log(module_key, "info", "module released")
        else:
            self.log(
                module_key,
                "warn" if not reassert else "info",
                f"{'Occupy' if holds else 'Release'} refused: {model.error_text(result.error_id)}",
            )

    # --- calls -----------------------------------------------------------

    def occupy(self, session: str, module_key: str, action: str) -> dict[str, Any]:
        if action not in ("occupy", "release"):
            raise ValueError(f"unknown occupation action '{action}'")
        self.operator(session)
        profile = self.modules[module_key].profile
        path = "Occupation/Occupy" if action == "occupy" else "Occupation/Release"
        holds = action == "occupy"
        result = self._call(module_key, path, [session], label=f"Occupation/{action.title()}")
        self._note_occupation_result(module_key, session, result, holds=holds)
        return self._call_payload(result, extra={"module": profile.key, "occupier": holds and result.accepted})

    def module_command(self, session: str, module_key: str, command: str) -> dict[str, Any]:
        if command not in model.MODULE_COMMANDS:
            raise ValueError(f"unknown module command '{command}'")
        self.operator(session)
        result = self._call(module_key, f"Module/{command}", [session], label=f"Module/{command}")
        return self._call_payload(result)

    def skill_command(
        self,
        session: str,
        module_key: str,
        skill_name: str,
        command: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if command not in model.SKILL_COMMANDS:
            raise ValueError(f"unknown skill command '{command}'")
        self.operator(session)
        profile = self.modules[module_key].profile
        skill = next((item for item in profile.skills if item.name == skill_name), None)
        if skill is None:
            raise ValueError(f"module '{module_key}' has no skill '{skill_name}'")
        args: list[Any] = [session]
        if command == "Start":
            values, problems = self._start_params(skill, params or {})
            if problems:
                text = "; ".join(problems)
                self.log(
                    module_key,
                    "warn",
                    f"Skills/{skill_name}/Start not sent: {model.error_text(model.ERR_OUT_OF_RANGE)} ({text})",
                )
                return {
                    "ok": True,
                    "accepted": False,
                    "errorId": model.ERR_OUT_OF_RANGE,
                    "errorText": model.error_text(model.ERR_OUT_OF_RANGE),
                    "transportError": "",
                    "sent": False,
                    "problem": text,
                }
            args += values
        result = self._call(
            module_key, f"Skills/{skill_name}/{command}", args, label=f"Skills/{skill_name}/{command}"
        )
        return self._call_payload(result)

    def _start_params(
        self, skill: prof.Skill, params: dict[str, Any]
    ) -> tuple[list[float], list[str]]:
        values: list[float] = []
        problems: list[str] = []
        for param in skill.params:
            raw = params.get(param.name, param.default)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                problems.append(f"{param.name}={raw!r} is not a number")
                values.append(param.default)
                continue
            if not param.minimum <= value <= param.maximum:
                problems.append(f"{param.name}={value:g} outside {param.minimum:g}..{param.maximum:g}")
            values.append(value)
        return values, problems

    def _call(self, module_key: str, path: str, args: list[Any], label: str) -> CallResult:
        link = self.link_for(module_key)
        result = link.call(module_key, path, args)
        self._log_call_result(module_key, label, result)
        return result

    def _log_call_result(self, module_key: str, label: str, result: CallResult) -> None:
        if not result.ok:
            self.log(module_key, "error", f"{label}: not sent ({result.transport_error})")
        elif result.accepted:
            self.log(module_key, "info", f"{label}: accepted")
        else:
            self.log(module_key, "warn", f"{label}: refused, {model.error_text(result.error_id)}")

    def _call_payload(self, result: CallResult, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "ok": result.ok,
            "accepted": result.accepted,
            "errorId": result.error_id,
            "errorText": model.error_text(result.error_id) if result.ok else "",
            "transportError": result.transport_error,
            "sent": result.ok,
        }
        if extra:
            payload.update(extra)
        return payload

    def link_for(self, module_key: str) -> OpcuaLink:
        config = self.modules[module_key]
        return self.links[config.endpoint]

    # --- log -------------------------------------------------------------

    def log(self, module_key: str, level: str, text: str) -> None:
        with self._lock:
            self._log.append(LogEntry(ts=time.time(), level=level, module=module_key, text=text))

    def log_entries(self, limit: int = 60) -> list[dict[str, Any]]:
        with self._lock:
            entries = list(self._log)[-limit:]
        return [entry.to_dict() for entry in entries]

    # --- snapshot --------------------------------------------------------

    def client_config(self) -> dict[str, Any]:
        return {
            "modules": [config.profile.to_dict() for config in self.configs],
            "endpoints": {config.profile.key: config.endpoint for config in self.configs},
            "moduleStates": {str(code): {"name": name, "kind": model.MODULE_STATE_KIND.get(code, "waits")}
                             for code, name in model.MODULE_STATES.items()},
            "skillStates": {str(code): name for code, name in model.SKILL_STATES.items()},
            "skillStateCss": {str(code): css for code, css in model.SKILL_STATES_CSS.items()},
            "errors": {str(code): {"name": name, "text": model.ERROR_TEXT[code]}
                       for code, name in model.ERROR_CODES.items()},
            "moduleDiagram": [
                {"from": source, "to": target, "label": label}
                for source, target, label in model.MODULE_DIAGRAM
            ],
            "moduleStateKinds": {
                str(code): model.MODULE_STATE_KIND.get(code, "waits")
                for code in model.MODULE_STATES
            },
            "skillDiagram": [
                {"from": source, "to": target, "label": label, "style": style}
                for source, target, label, style in model.SKILL_DIAGRAM
            ],
            "skillStateKinds": {
                str(code): model.SKILL_STATE_KIND.get(code, "waits")
                for code in model.SKILL_STATES
            },
            "moduleCommandStates": {
                command: sorted(model.MODULE_COMMAND_FROM_STATES[command])
                for command in model.MODULE_COMMANDS
            },
        }

    def snapshot(self, session: str) -> dict[str, Any]:
        operator = self.operator(session)
        modules: dict[str, Any] = {}
        for config in self.configs:
            modules[config.profile.key] = self._module_snapshot(config, operator)
        return {
            "ts": time.time(),
            "session": session,
            "modules": modules,
            "log": self.log_entries(),
        }

    def _module_snapshot(self, config: ModuleConfig, operator: Operator) -> dict[str, Any]:
        profile = config.profile
        link = self.link_for(profile.key)
        channel = link.channels[profile.key]
        link_state = link.snapshot()["state"]
        connected = bool(link_state["connected"] and channel.available)
        entries = channel.snapshot()
        values = {path: entry["value"] for path, entry in entries.items()}
        statuses = {path: entry["status"] for path, entry in entries.items()}
        occupied = bool(values.get("Occupation/Occupied"))
        if connected and occupied and profile.key not in operator.occupies:
            with self._lock:
                probe = profile.key not in operator.probed
                operator.probed.add(profile.key)
            if probe:
                threading.Thread(
                    target=self._probe_occupation, args=(operator.session, profile.key),
                    name="probe-occupation", daemon=True,
                ).start()
        occupier = operator.occupies.get(profile.key, False)
        holders = _equipment_holders(profile, values)
        module_state = values.get("Module/State")
        view: dict[str, Any] = {
            "key": profile.key,
            "title": profile.title,
            "endpoint": config.endpoint,
            "connected": connected,
            "stale": not connected,
            "missing": list(channel.missing),
            "detail": link_state["detail"],
            "occupied": occupied,
            "occupier": occupier,
            "moduleState": {
                "value": module_state,
                "name": model.module_state_name(module_state),
                "kind": model.MODULE_STATE_KIND.get(_as_int(module_state), "waits"),
                "status": statuses.get("Module/State", "Bad"),
            },
            "commands": {
                command: connected
                and model.module_command_enabled(command, module_state, occupier)
                for command in model.MODULE_COMMANDS
            },
            "occupation": {
                "occupy": connected and not occupier,
                "release": connected and occupier,
            },
            "skills": {},
            "procedures": {},
            "sensors": {},
        }
        for skill in profile.skills:
            base = f"Skills/{skill.name}"
            skill_state = values.get(f"{base}/State")
            error_id = model.shown_error(skill_state, values.get(f"{base}/ErrorID"))
            stored = operator.skill_params(profile.key, skill.name, skill.params)
            blocked = sorted({
                holder
                for equipment in skill.uses
                for holder in holders.get(equipment, ())
                if holder != skill.name
            })
            view["skills"][skill.name] = {
                "state": skill_state,
                "stateName": model.skill_state_name(skill_state),
                "stateCss": model.SKILL_STATES_CSS.get(_as_int(skill_state), "idle"),
                "errorId": error_id,
                "errorText": model.error_text(error_id) if error_id else "",
                "moduleLevel": skill.module_level,
                "heldBy": blocked,
                "params": {
                    param.name: {"stored": stored[param.name], **param.to_dict()} for param in skill.params
                },
                "results": {
                    result.name: {
                        "value": values.get(f"{base}/Results/{result.name}"),
                        "unit": result.unit,
                    }
                    for result in skill.results
                },
                "commands": {
                    command: connected
                    and model.skill_command_enabled(
                        command,
                        skill_state,
                        module_state,
                        occupier,
                        parameters_in_range=_params_in_range(skill.params, stored),
                        equipment_free=not blocked,
                    )
                    for command in model.SKILL_COMMANDS
                },
                "steps": _step_view(f"{base}/Execute", skill.steps, values, statuses),
                "stopSteps": _step_view(f"{base}/Stopping", skill.stop_steps, values, statuses),
            }
        for procedure in profile.procedures:
            view["procedures"][procedure.name] = {
                "label": procedure.label,
                "steps": _step_view(f"Procedures/{procedure.name}", procedure.steps, values, statuses),
            }
        for sensor in profile.sensors:
            path = f"Equipment/{sensor.equipment}/{sensor.name}"
            view["sensors"][sensor.name] = {
                "value": values.get(path),
                "status": statuses.get(path, "Bad"),
                **sensor.to_dict(),
            }
        return view

    def _watch_transitions(self, profile: ModuleProfile, values: dict[str, Any]) -> None:
        """Log state changes that the operator should know about."""
        with self._lock:
            previous = self._last_states.setdefault(profile.key, {})
            for path, value in values.items():
                old = previous.get(path, KeyError)
                if old is KeyError:
                    previous[path] = value
                    continue
                previous[path] = value
                if old == value:
                    continue
                self._log_transition(profile, path, old, value)
            self._log_failures(profile, values)

    def _log_failures(self, profile: ModuleProfile, values: dict[str, Any]) -> None:
        """Log failed skills with their ErrorID, which may arrive a notification later."""
        for (module_key, skill), (deadline, old_name) in list(self._failures.items()):
            if module_key != profile.key:
                continue
            error_id = _as_int(values.get(f"Skills/{skill}/ErrorID"))
            if error_id <= 0 and time.monotonic() < deadline:
                continue
            del self._failures[(module_key, skill)]
            level = "warn" if error_id == model.ERR_INTERRUPTED else "error"
            self.log(profile.key, level, f"{skill}: {old_name} -> Failed, {model.error_text(error_id)}")

    def _log_transition(self, profile: ModuleProfile, path: str, old: Any, new: Any) -> None:
        parts = path.split("/")
        if path == "Module/State":
            self.log(
                profile.key,
                "info",
                f"Module/State {model.module_state_name(old)} -> {model.module_state_name(new)}",
            )
            return
        if len(parts) >= 3 and parts[0] == "Skills" and parts[2] == "State":
            skill = parts[1]
            old_name = model.skill_state_name(old)
            new_name = model.skill_state_name(new)
            if new == model.SK_FAILED:
                # logged once its ErrorID has arrived (_log_failures)
                self._failures[(profile.key, skill)] = (time.monotonic() + FAILURE_WAIT, old_name)
            elif new == model.SK_ABORTED:
                self.log(profile.key, "warn", f"{skill}: {old_name} -> {new_name}")
            elif new in (model.SK_RUNNING, model.SK_SUCCEEDED):
                self.log(profile.key, "info", f"{skill}: {old_name} -> {new_name}")
            return
        if len(parts) >= 2 and parts[0] == "Procedures" and parts[-1] == "State":
            self.log(profile.key, "info", f"{path}: {model.skill_state_name(old)} -> "
                                          f"{model.skill_state_name(new)}")


def _step_view(
    prefix: str, steps: Iterable[prof.Step], values: dict[str, Any], statuses: dict[str, str]
) -> list[dict[str, Any]]:
    view = []
    for index, step in enumerate(steps, start=1):
        path = f"{prefix}/{step.name}"
        state = values.get(f"{path}/State")
        error_id = model.shown_error(state, values.get(f"{path}/ErrorID"))
        view.append(
            {
                "index": index,
                "name": step.name,
                "skill": step.runs,
                "label": step.label,
                "uses": list(step.uses),
                "state": state,
                "stateName": model.skill_state_name(state),
                "stateCss": model.SKILL_STATES_CSS.get(_as_int(state), "idle"),
                "errorId": error_id,
                "errorText": model.error_text(error_id) if error_id else "",
                "status": statuses.get(f"{path}/State", "Bad"),
                "params": {
                    param.name: values.get(f"{path}/Parameters/{param.name}", param.default)
                    for param in step.params
                },
            }
        )
    return view


def _current_step(steps: Iterable[prof.Step], prefix: str, values: dict[str, Any]) -> int | None:
    """Index of the step that runs now, None between two steps."""
    for index, step in enumerate(steps):
        if _as_int(values.get(f"{prefix}/{step.name}/State")) in (model.SK_RUNNING, model.SK_STOPPING):
            return index
    return None


def _equipment_holders(profile: ModuleProfile, values: dict[str, Any]) -> dict[str, set[str]]:
    """Which active skill holds which equipment, from the skill and step states."""
    holders: dict[str, set[str]] = {}
    for skill in profile.skills:
        base = f"Skills/{skill.name}"
        state = _as_int(values.get(f"{base}/State"))
        if state not in (model.SK_RUNNING, model.SK_STOPPING):
            continue
        held = set(skill.uses)
        if skill.module_level and state == model.SK_RUNNING:
            index = _current_step(skill.steps, f"{base}/{prof.STEP_GROUP_EXECUTE}", values)
            if index is not None:
                held = prof.held_equipment(skill, prof.STEP_GROUP_EXECUTE, index)
        for equipment in held:
            holders.setdefault(equipment, set()).add(skill.name)
    return holders


def _params_in_range(params: Iterable[Param], param_values: dict[str, float]) -> bool:
    for param in params:
        value = param_values.get(param.name)
        if value is None:
            continue
        if not param.minimum <= float(value) <= param.maximum:
            return False
    return True


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1