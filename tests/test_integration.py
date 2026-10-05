"""End to end tests: HMI service and API against the simulated modules."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hmi import model
from hmi import profiles as prof
from hmi.app import create_app
from hmi.service import HmiService, ModuleConfig
from sim.fake_module import SimulatedServer

SESSION = "test-session-1"
OTHER_SESSION = "test-session-2"
TIMEOUT = 30.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Simulator:
    """Runs a simulated controller on its own event loop in a background thread."""

    def __init__(self, keys: list[str], port: int | None = None) -> None:
        self.keys = keys
        self.port = port or free_port()
        self.endpoint = f"opc.tcp://127.0.0.1:{self.port}"
        self.profiles = [prof.get_profile(key) for key in keys]
        self.loop = asyncio.new_event_loop()
        self.server: SimulatedServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        ready = threading.Event()

        async def main() -> None:
            self.server = SimulatedServer(self.endpoint, self.profiles)
            await self.server.start()
            ready.set()
            while True:
                await asyncio.sleep(3600)

        def worker() -> None:
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(main())
            except (asyncio.CancelledError, RuntimeError):
                pass

        self.thread = threading.Thread(target=worker, name="sim", daemon=True)
        self.thread.start()
        assert ready.wait(timeout=20)

    def module(self, key: str):
        return self.server.modules[key]

    def stop(self) -> None:
        if self.server is not None and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop)
            try:
                future.result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
        self.loop.close()


@pytest.fixture(scope="module")
def simulator():
    instance = Simulator(["filling", "stoppering"])
    instance.start()
    yield instance
    instance.stop()


@pytest.fixture(scope="module")
def service(simulator):
    configs = [
        ModuleConfig(profile=profile, endpoint=simulator.endpoint)
        for profile in (prof.FILLING, prof.STOPPERING)
    ]
    return HmiService(configs, sampling_ms=100)


@pytest.fixture(scope="module")
def client(service):
    """The app on the test client's event loop; its lifespan starts and stops the service."""
    with TestClient(create_app(service)) as test_client:
        assert wait_for(lambda: all(link.connected for link in service.links.values()))
        yield test_client


def wait_for(predicate, timeout: float = TIMEOUT, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def wait_for_any(predicate, timeout: float = TIMEOUT, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def snapshot(client, session: str = SESSION) -> dict:
    response = client.get("/api/snapshot", headers={"X-Session-Id": session})
    assert response.status_code == 200, response.text
    return response.json()


def view(client, key: str, session: str = SESSION) -> dict:
    return snapshot(client, session)["modules"][key]


def value_of(module_view: dict, path: str):
    parts = path.split("/")
    if parts[0] == "Module":
        return module_view["moduleState"]["value"]
    if parts[0] == "Equipment":
        return module_view["sensors"][parts[2]]["value"]
    if parts[0] == "Skills":
        return module_view["skills"][parts[1]][parts[2].lower()]
    raise AssertionError(f"unsupported path {path}")


def post(client, path: str, payload: dict, session: str = SESSION) -> dict:
    response = client.post(path, json=payload, headers={"X-Session-Id": session})
    assert response.status_code == 200, response.text
    return response.json()


def wait_state(client, key: str, expected: str, timeout: float = TIMEOUT) -> dict:
    assert wait_for(
        lambda: view(client, key)["moduleState"]["name"] == expected, timeout=timeout
    ), f"{key} did not reach {expected}: {view(client, key)['moduleState']}"
    return view(client, key)


def wait_skill(client, key: str, skill: str, expected: str, timeout: float = TIMEOUT) -> dict:
    assert wait_for(
        lambda: view(client, key)["skills"][skill]["stateName"] == expected, timeout=timeout
    ), f"{skill} did not reach {expected}: {view(client, key)['skills'][skill]}"
    return view(client, key)["skills"][skill]


def accepted(result: dict) -> bool:
    assert result["accepted"] is True, f"refused: {result}"
    return True


def refused(result: dict, error_id: int) -> bool:
    assert result["accepted"] is False, f"unexpectedly accepted: {result}"
    assert result["errorId"] == error_id, result
    return True


def occupy(client, key: str) -> None:
    accepted(post(client, "/api/occupation", {"module": key, "action": "occupy"}))


def module_command(client, key: str, command: str) -> bool:
    return accepted(post(client, "/api/module-command", {"module": key, "command": command}))


def skill_command(client, key: str, skill: str, command: str, params: dict | None = None) -> dict:
    payload: dict = {"module": key, "skill": skill, "command": command}
    if params:
        payload["params"] = params
    return post(client, "/api/skill-command", payload)


def to_execute(client, key: str) -> None:
    """Bring a module into Execute, whatever state it is in."""
    for _attempt in range(3):
        occupy(client, key)
        name = view(client, key)["moduleState"]["name"]
        if name == "Aborted":
            module_command(client, key, "Clear")
            wait_state(client, key, "Stopped")
            name = "Stopped"
        if name == "Stopping":
            wait_state(client, key, "Stopped")
            name = "Stopped"
        if name in ("Starting", "Execute"):
            break
        if name == "Stopped":
            module_command(client, key, "Reset")
            wait_state(client, key, "Idle", timeout=60)
            name = "Idle"
        if name == "Resetting":
            wait_state(client, key, "Idle", timeout=60)
            name = "Idle"
        if name == "Idle":
            module_command(client, key, "Start")
            wait_state(client, key, "Execute")
            break
        raise AssertionError(f"cannot reach Execute from {name}")
    assert view(client, key)["moduleState"]["name"] == "Execute", view(client, key)["moduleState"]


def idle_skill(client, key: str, skill: str) -> None:
    """Bring a skill into a state in which it can be started again.

    Waits for a state that is stable for several polls, so that a value from
    just before an abort cannot be mistaken for the current one.
    """
    deadline = time.monotonic() + 40
    stopped = reset = False
    stable = 0
    previous = None
    while time.monotonic() < deadline:
        current = view(client, key)["skills"][skill]
        name = current["stateName"]
        stable = stable + 1 if name == previous else 0
        previous = name
        if name in ("Idle", "Succeeded", "Failed") and stable >= 5:
            return
        if name in ("Running", "Stopping") and not stopped:
            skill_command(client, key, skill, "Stop")
            stopped = True
        elif name == "Aborted" and not reset:
            skill_command(client, key, skill, "Reset")
            reset = True
        time.sleep(0.15)
    raise AssertionError(f"{skill} did not settle: {view(client, key)['skills'][skill]}")


def test_config_lists_both_modules(client):
    payload = client.get("/api/config").json()
    assert [module["key"] for module in payload["modules"]] == ["filling", "stoppering"]
    assert payload["moduleStates"]["6"]["name"] == "Execute"
    assert payload["skillStates"]["1"] == "Running"
    assert payload["errors"]["7"]["name"] == "Interrupted"


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "AP2030 Modular Production HMI" in body
    assert "diagram.js" in body
    assert "hmi.js" in body


def test_snapshot_without_session_is_rejected(client):
    response = client.get("/api/snapshot")
    assert response.status_code == 400
    assert "session" in response.json()["error"]


def test_values_arrive_through_the_subscription(client):
    module = view(client, "filling")
    assert module["connected"] is True
    assert module["moduleState"]["name"] == "Stopped"
    assert value_of(module, "Equipment/NeedleAxis/AtTop") is True
    assert value_of(module, "Skills/MoveNeedleUp/State") == model.SK_IDLE
    assert module["skills"]["Dispensing"]["steps"][0]["name"] == "MoveNeedleDown"
    assert module["skills"]["Dispensing"]["stopSteps"][0]["name"] == "MoveNeedleUp"
    assert view(client, "stoppering")["procedures"]["Resetting"]["steps"][0]["name"] == "ArmMiddle"


def test_snapshots_are_pushed_over_the_websocket(client):
    with client.websocket_connect(f"/ws?session={SESSION}") as socket:
        first = socket.receive_json()
        assert set(first["modules"]) == {"filling", "stoppering"}
        assert first["session"] == SESSION
        occupy(client, "stoppering")
        for _ in range(50):
            pushed = socket.receive_json()
            if pushed["modules"]["stoppering"]["occupier"]:
                break
        else:
            raise AssertionError("no snapshot showed the occupation")
        assert pushed["log"][-1]["text"] in ("module occupied", "Occupation/Occupy: accepted")


def test_a_websocket_without_session_is_closed(client):
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
    assert closed.value.code == 1008


def test_commands_are_refused_without_the_occupation(client):
    stranger = "session-that-never-occupied"
    refused(
        post(client, "/api/module-command", {"module": "filling", "command": "Reset"}, stranger),
        model.ERR_NOT_PERMITTED,
    )
    assert view(client, "filling", stranger)["occupier"] is False
    assert view(client, "filling", stranger)["commands"]["Reset"] is False
    texts = [entry["text"] for entry in client.get("/api/log?limit=20").json()["entries"]]
    assert any("Module/Reset: refused" in text and "NotPermitted" in text for text in texts), texts


def test_occupy_release_cycle(client):
    occupy(client, "filling")
    assert wait_for(lambda: view(client, "filling")["occupier"] is True)
    assert wait_for(lambda: view(client, "filling")["occupied"] is True)
    module = view(client, "filling")
    assert module["occupation"]["release"] is True
    assert module["occupation"]["occupy"] is False

    refused(
        post(client, "/api/occupation", {"module": "filling", "action": "occupy"}, OTHER_SESSION),
        model.ERR_NOT_PERMITTED,
    )
    assert view(client, "filling", OTHER_SESSION)["occupier"] is False

    accepted(post(client, "/api/occupation", {"module": "filling", "action": "release"}))
    assert wait_for(lambda: view(client, "filling")["occupied"] is False)
    assert wait_for(lambda: view(client, "filling")["occupier"] is False)


def test_reset_start_execute_cycle(client):
    occupy(client, "filling")
    module_command(client, "filling", "Reset")
    module = wait_state(client, "filling", "Idle", timeout=60)
    assert module["commands"]["Reset"] is False
    assert module["commands"]["Start"] is True
    module_command(client, "filling", "Start")
    module = wait_state(client, "filling", "Execute")
    assert module["commands"]["Start"] is False
    assert module["commands"]["Stop"] is True
    assert module["commands"]["Clear"] is False


def test_start_is_refused_out_of_execute(client):
    module_command(client, "filling", "Stop")
    wait_state(client, "filling", "Stopped")
    refused(skill_command(client, "filling", "MoveNeedleUp", "Start"), model.ERR_NOT_READY)


def test_parameters_out_of_range_are_not_sent(client):
    to_execute(client, "stoppering")
    result = skill_command(client, "stoppering", "RaisePiston", "Start", {"Duration": 99})
    refused(result, model.ERR_OUT_OF_RANGE)
    assert result["sent"] is False
    assert "Duration" in result["problem"]
    texts = [entry["text"] for entry in client.get("/api/log?limit=20").json()["entries"]]
    assert any("not sent" in text and "OutOfRange" in text for text in texts), texts


def test_primitive_skill_moves_equipment_and_succeeds(client):
    to_execute(client, "stoppering")
    idle_skill(client, "stoppering", "LowerPiston")
    accepted(skill_command(client, "stoppering", "LowerPiston", "Start"))
    skill = wait_skill(client, "stoppering", "LowerPiston", "Succeeded")
    assert skill["errorId"] == 0
    assert wait_for(
        lambda: value_of(view(client, "stoppering"), "Equipment/Piston/AtLimit") is True
    )
    idle_skill(client, "stoppering", "RaisePiston")
    accepted(skill_command(client, "stoppering", "RaisePiston", "Start", {"Duration": 1.0}))
    wait_skill(client, "stoppering", "RaisePiston", "Succeeded")


def place(simulator, module: str, equipment: str, position: float) -> None:
    """Move an axis by hand (0 is its backward end, 1 its forward end)."""
    future = asyncio.run_coroutine_threadsafe(
        simulator.module(module).place(equipment, position), simulator.loop
    )
    future.result(timeout=5)


def test_second_skill_on_the_same_equipment_is_busy(client, simulator):
    to_execute(client, "stoppering")
    for skill in ("ExtendPlunger", "RetractPlunger", "MoveArm", "LowerPiston", "RaisePiston"):
        idle_skill(client, "stoppering", skill)

    accepted(skill_command(client, "stoppering", "ExtendPlunger", "Start", {"Duration": 6}))
    refused(
        skill_command(client, "stoppering", "RetractPlunger", "Start", {"Duration": 6}),
        model.ERR_BUSY,
    )

    place(simulator, "stoppering", "Piston", 0.5)
    accepted(skill_command(client, "stoppering", "LowerPiston", "Start"))
    refused(
        skill_command(client, "stoppering", "RaisePiston", "Start", {"Duration": 2}),
        model.ERR_BUSY,
    )
    accepted(skill_command(client, "stoppering", "MoveArm", "Start", {"Angle": 90, "Settle": 1}))

    accepted(skill_command(client, "stoppering", "MoveArm", "Abort"))
    accepted(skill_command(client, "stoppering", "LowerPiston", "Abort"))
    accepted(skill_command(client, "stoppering", "ExtendPlunger", "Abort"))


def test_composite_skill_drives_its_steps_not_the_standalone_skills(client, simulator):
    """A composite writes its own step variables, not the primitive's.

    That is why the page links a primitive skill card to the step that is
    currently running.
    """
    to_execute(client, "filling")
    idle_skill(client, "filling", "Dispensing")
    idle_skill(client, "filling", "MoveNeedleDown")
    place(simulator, "filling", "NeedleAxis", 0.0)
    accepted(skill_command(client, "filling", "Dispensing", "Start"))

    running = wait_for_any(
        lambda: next(
            (step for step in view(client, "filling")["skills"]["Dispensing"]["steps"]
             if step["stateName"] == "Running"),
            None,
        ),
        timeout=10,
    )
    assert running is not None, view(client, "filling")["skills"]["Dispensing"]["steps"]
    primitive = view(client, "filling")["skills"].get(running["name"])
    if primitive is not None:
        assert primitive["stateName"] != "Running", (
            "the standalone skill variable stays untouched while a step runs"
        )
    wait_skill(client, "filling", "Dispensing", "Succeeded", timeout=30)


def test_the_needle_end_switches_are_never_both_true(client):
    to_execute(client, "filling")
    def sensors():
        return view(client, "filling")["sensors"]

    assert sensors()["AtTop"]["value"] is True
    assert sensors()["AtBottom"]["value"] is False

    for skill in ("MoveNeedleDown", "MoveNeedleUp"):
        idle_skill(client, "filling", skill)
        accepted(skill_command(client, "filling", skill, "Start"))
        wait_skill(client, "filling", skill, "Succeeded")
        current = sensors()
        assert current["AtTop"]["value"] is False or current["AtBottom"]["value"] is False, (
            f"{skill} left the axis at both end switches"
        )
    assert sensors()["AtTop"]["value"] is True
    assert sensors()["AtBottom"]["value"] is False


def test_a_moving_skill_does_not_succeed_at_once(client):
    to_execute(client, "filling")
    idle_skill(client, "filling", "MoveNeedleDown")
    accepted(skill_command(client, "filling", "MoveNeedleDown", "Start"))
    time.sleep(0.4)
    assert view(client, "filling")["skills"]["MoveNeedleDown"]["stateName"] == "Running", (
        "the skill succeeded although the axis was not at the end switch"
    )
    wait_skill(client, "filling", "MoveNeedleDown", "Succeeded")
    # already at the target: the next skill that way succeeds without moving
    accepted(skill_command(client, "filling", "MoveNeedleDown", "Start"))
    wait_skill(client, "filling", "MoveNeedleDown", "Succeeded")
    accepted(skill_command(client, "filling", "MoveNeedleUp", "Start"))
    wait_skill(client, "filling", "MoveNeedleUp", "Succeeded")


def test_tare_zeroes_the_scale_and_dispensing_fills_about_three_grams(client):
    to_execute(client, "filling")
    for skill in ("Tare", "Dispensing", "Weigh"):
        idle_skill(client, "filling", skill)
    accepted(skill_command(client, "filling", "Tare", "Start"))
    wait_skill(client, "filling", "Tare", "Succeeded", timeout=15)
    assert wait_for(lambda: view(client, "filling")["sensors"]["Weight"]["value"] == 0.0, timeout=5)
    accepted(skill_command(client, "filling", "Dispensing", "Start"))
    wait_skill(client, "filling", "Dispensing", "Running")       # not the last run's Succeeded
    skill = wait_skill(client, "filling", "Dispensing", "Succeeded", timeout=30)
    assert 2.9 <= skill["results"]["Weight"]["value"] <= 3.2, skill["results"]
    accepted(skill_command(client, "filling", "Weigh", "Start"))

    def weighed():
        value = view(client, "filling")["skills"]["Weigh"]["results"]["Weight"]["value"]
        return value is not None and 2.9 <= value <= 3.2

    assert wait_for(weighed, timeout=15), view(client, "filling")["skills"]["Weigh"]


def test_a_succeeded_skill_returns_to_idle_by_itself(client):
    """SKILL_Control: Succeeded -> Idle after 1.5 s; Start runs it again from either."""
    to_execute(client, "stoppering")
    idle_skill(client, "stoppering", "MoveArm")
    accepted(skill_command(client, "stoppering", "MoveArm", "Start", {"Angle": 120, "Settle": 0.5}))
    wait_skill(client, "stoppering", "MoveArm", "Succeeded")
    skill = wait_skill(client, "stoppering", "MoveArm", "Idle", timeout=5)
    assert skill["commands"]["Start"] is True
    assert skill["commands"]["Reset"] is False
    assert skill["errorId"] == 0
    accepted(skill_command(client, "stoppering", "MoveArm", "Start", {"Angle": 60, "Settle": 0.5}))
    wait_skill(client, "stoppering", "MoveArm", "Running", timeout=5)
    wait_skill(client, "stoppering", "MoveArm", "Succeeded")


def test_stop_passes_stopping_and_fails_with_interrupted(client):
    to_execute(client, "stoppering")
    idle_skill(client, "stoppering", "ExtendPlunger")
    accepted(skill_command(client, "stoppering", "ExtendPlunger", "Start", {"Duration": 8}))
    wait_skill(client, "stoppering", "ExtendPlunger", "Running")
    accepted(skill_command(client, "stoppering", "ExtendPlunger", "Stop"))
    skill = wait_skill(client, "stoppering", "ExtendPlunger", "Failed", timeout=5)
    assert skill["errorId"] == model.ERR_INTERRUPTED
    refused(skill_command(client, "stoppering", "ExtendPlunger", "Stop"), model.ERR_NOT_READY)


def test_an_old_error_is_not_shown_once_the_skill_is_idle_again(client, simulator):
    """The controller keeps ErrorID after Abort and Reset; the page shows it only where it belongs."""
    to_execute(client, "stoppering")
    idle_skill(client, "stoppering", "RetractPlunger")
    accepted(skill_command(client, "stoppering", "RetractPlunger", "Start", {"Duration": 8}))
    accepted(skill_command(client, "stoppering", "RetractPlunger", "Abort"))
    skill = wait_skill(client, "stoppering", "RetractPlunger", "Aborted")
    assert skill["errorId"] == model.ERR_INTERRUPTED
    accepted(skill_command(client, "stoppering", "RetractPlunger", "Reset"))
    skill = wait_skill(client, "stoppering", "RetractPlunger", "Idle")
    assert simulator.module("stoppering").skills["RetractPlunger"].error_id == model.ERR_INTERRUPTED
    assert skill["errorId"] == 0
    assert skill["errorText"] == ""


def test_a_module_level_skill_locks_the_equipment_of_its_steps(client):
    to_execute(client, "stoppering")
    for skill in ("Stoppering", "LowerPiston", "MoveArm", "ExtendPlunger"):
        idle_skill(client, "stoppering", skill)
    accepted(skill_command(client, "stoppering", "Stoppering", "Start"))
    wait_skill(client, "stoppering", "Stoppering", "Running")
    module = view(client, "stoppering")
    # the piston is held from the first step to the last
    assert module["skills"]["LowerPiston"]["heldBy"] == ["Stoppering"]
    assert module["skills"]["LowerPiston"]["commands"]["Start"] is False
    refused(skill_command(client, "stoppering", "LowerPiston", "Start"), model.ERR_BUSY)
    accepted(skill_command(client, "stoppering", "Stoppering", "Abort"))
    wait_skill(client, "stoppering", "Stoppering", "Aborted")
    assert view(client, "stoppering")["skills"]["LowerPiston"]["heldBy"] == []
    accepted(skill_command(client, "stoppering", "Stoppering", "Reset"))
    wait_skill(client, "stoppering", "Stoppering", "Idle")


def test_start_is_refused_while_the_skill_is_still_running(client):
    to_execute(client, "stoppering")
    idle_skill(client, "stoppering", "ExtendPlunger")
    accepted(skill_command(client, "stoppering", "ExtendPlunger", "Start", {"Duration": 6}))
    wait_skill(client, "stoppering", "ExtendPlunger", "Running")
    refused(
        skill_command(client, "stoppering", "ExtendPlunger", "Start", {"Duration": 6}),
        model.ERR_BUSY,
    )
    accepted(skill_command(client, "stoppering", "ExtendPlunger", "Abort"))
    wait_skill(client, "stoppering", "ExtendPlunger", "Aborted")
    accepted(skill_command(client, "stoppering", "ExtendPlunger", "Reset"))
    wait_skill(client, "stoppering", "ExtendPlunger", "Idle")


def test_skill_abort_then_reset(client):
    to_execute(client, "stoppering")
    idle_skill(client, "stoppering", "ExtendPlunger")
    accepted(skill_command(client, "stoppering", "ExtendPlunger", "Start", {"Duration": 8}))
    accepted(skill_command(client, "stoppering", "ExtendPlunger", "Abort"))
    skill = wait_skill(client, "stoppering", "ExtendPlunger", "Aborted")
    assert skill["commands"]["Reset"] is True
    assert skill["commands"]["Start"] is False
    accepted(skill_command(client, "stoppering", "ExtendPlunger", "Reset"))
    assert wait_skill(client, "stoppering", "ExtendPlunger", "Idle")["commands"]["Start"] is True


def test_stoppering_cycle_runs_to_success(client):
    to_execute(client, "stoppering")
    idle_skill(client, "stoppering", "Stoppering")
    accepted(skill_command(client, "stoppering", "Stoppering", "Start"))
    wait_skill(client, "stoppering", "Stoppering", "Running")
    skill = wait_skill(client, "stoppering", "Stoppering", "Succeeded", timeout=90)
    # each step succeeded; the early ones are back in Idle by now
    assert all(step["stateName"] in ("Succeeded", "Idle") and step["errorId"] == 0
               for step in skill["steps"]), skill["steps"]
    # the last step raises the piston off its limit switch
    assert value_of(view(client, "stoppering"), "Equipment/Piston/AtLimit") is False


def test_needle_moves_and_weigh_produces_a_result(client):
    to_execute(client, "filling")
    for skill in ("MoveNeedleDown", "MoveNeedleUp", "Weigh"):
        idle_skill(client, "filling", skill)
    accepted(skill_command(client, "filling", "MoveNeedleDown", "Start"))
    assert wait_for(
        lambda: value_of(view(client, "filling"), "Equipment/NeedleAxis/AtBottom") is True
    )
    accepted(skill_command(client, "filling", "MoveNeedleUp", "Start"))
    assert wait_for(
        lambda: value_of(view(client, "filling"), "Equipment/NeedleAxis/AtTop") is True
    )
    accepted(skill_command(client, "filling", "Weigh", "Start"))
    skill = wait_skill(client, "filling", "Weigh", "Succeeded")
    assert skill["results"]["Weight"]["value"] > 0


def test_dispensing_runs_and_can_be_stopped(client):
    to_execute(client, "filling")
    idle_skill(client, "filling", "Dispensing")
    accepted(skill_command(client, "filling", "Dispensing", "Start"))
    wait_skill(client, "filling", "Dispensing", "Running")
    accepted(skill_command(client, "filling", "Dispensing", "Stop"))
    skill = wait_skill(client, "filling", "Dispensing", "Failed", timeout=20)
    assert skill["errorId"] == model.ERR_INTERRUPTED
    assert skill["stopSteps"][0]["stateName"] == "Succeeded"
    assert view(client, "filling")["moduleState"]["name"] == "Execute"


def test_module_stop_returns_to_stopped(client):
    module_command(client, "filling", "Stop")
    wait_state(client, "filling", "Stopped")
    assert view(client, "filling")["commands"]["Reset"] is True


def test_abort_and_clear(client):
    to_execute(client, "stoppering")
    module_command(client, "stoppering", "Abort")
    module = wait_state(client, "stoppering", "Aborted")
    assert module["commands"]["Clear"] is True
    assert module["commands"]["Abort"] is False
    module_command(client, "stoppering", "Clear")
    wait_state(client, "stoppering", "Stopped")
    assert view(client, "stoppering")["skills"]["Stoppering"]["stateName"] == "Idle"


def test_sensor_fault_makes_the_skill_fail_with_timeout(client, simulator):
    simulator.module("filling").faults.add("sensor")
    try:
        to_execute(client, "filling")
        idle_skill(client, "filling", "MoveNeedleDown")
        place(simulator, "filling", "NeedleAxis", 0.0)
        accepted(skill_command(client, "filling", "MoveNeedleDown", "Start"))
        assert wait_for(
            lambda: view(client, "filling")["skills"]["MoveNeedleDown"]["errorId"]
            == model.ERR_TIMEOUT,
            timeout=20,
        ), view(client, "filling")["skills"]["MoveNeedleDown"]
        assert view(client, "filling")["skills"]["MoveNeedleDown"]["stateName"] == "Failed"
        assert view(client, "filling")["moduleState"]["name"] == "Execute"
    finally:
        simulator.module("filling").faults.clear()


def test_log_contains_refusals_and_failures(client):
    def entries():
        return client.get("/api/log?limit=200", headers={"X-Session-Id": SESSION}).json()["entries"]

    # the transition watcher logs within its next round (0.1 s)
    assert wait_for(lambda: any("Timeout" in entry["text"] for entry in entries()), timeout=3), entries()
    texts = [entry["text"] for entry in entries()]
    assert any("refused" in text for text in texts)
    assert any("Timeout" in text for text in texts), texts
    assert any(entry["level"] in ("warn", "error") for entry in entries())


def test_unknown_module_command_and_parameters_are_rejected(client):
    assert client.post(
        "/api/module-command", json={"module": "nope", "command": "Reset"},
        headers={"X-Session-Id": SESSION},
    ).status_code == 404
    assert client.post(
        "/api/module-command", json={"module": "filling", "command": "Explode"},
        headers={"X-Session-Id": SESSION},
    ).status_code == 400
    assert client.post(
        "/api/skill-command", json={"module": "filling", "skill": "Nope", "command": "Start"},
        headers={"X-Session-Id": SESSION},
    ).status_code == 400
    assert client.post(
        "/api/skill-command",
        json={"module": "filling", "skill": "Weigh", "command": "Start", "params": [1, 2]},
        headers={"X-Session-Id": SESSION},
    ).status_code == 400