"""The whole flow, with the real parts: FORTE module -> its AAS -> the HMI.

1. 4diac FORTE (tools/forte/build.sh) runs the filling module of iec61499-mgmt-py on its Modbus
   simulator (cell/sim/run_module.py, target pc).
2. ``modsync pull --register`` reads the running module, compares it with its spec and sends its
   profile to ``modreg serve``, which builds the AAS, checks it against the ontology (ARSO) and
   publishes it to an AAS server (Eclipse BaSyx).
3. The HMI reads the module from the AAS server (hmi/aas.py), connects to the endpoint the AAS
   names and runs Dispensing through its API.
4. An agent (modlink) runs the capability the module offers: from the capability to the skill
   realizing it and to the browse paths that skill refers to, all read from the AAS server.

Skipped unless the parts are there:

    FORTE_BIN=../forte-build/build-forte/forte/forte \\
    BASYX_JAR=basyx.aasenvironment.component-2.0.0-milestone-15-exec.jar \\  (or BASYX_URL=http://host:8081)
    python -m pytest tests/test_end_to_end.py

It needs a checkout of iec61499-mgmt-py next to this one (or IEC61499_MGMT_PY), installed with its
registration extra, and the ports FORTE and the simulator use (61499, 4840, 1502) free.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hmi import aas
from hmi import profiles as prof
from hmi.app import create_app
from hmi.service import HmiService, ModuleConfig
from modlink import Module, ModuleState, SkillState
from modlink import aas as modlink_aas

from test_integration import SESSION, free_port, skill_command, to_execute, view, wait_for, wait_skill

ROOT = Path(__file__).resolve().parents[1]
MGMT = Path(os.environ.get("IEC61499_MGMT_PY", ROOT.parent / "iec61499-mgmt-py"))
FORTE = os.environ.get("FORTE_BIN", "")
BASYX_JAR = os.environ.get("BASYX_JAR", "")
BASYX_URL = os.environ.get("BASYX_URL", "")
PORTS = (61499, 4840, 1502)          # FORTE management, its OPC UA server, the Modbus simulator


def in_use(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def missing() -> str:
    if not FORTE or not Path(FORTE).is_file():
        return "FORTE_BIN does not name a FORTE binary (tools/forte/build.sh)"
    if not BASYX_URL and not (BASYX_JAR and Path(BASYX_JAR).is_file()):
        return "neither BASYX_URL nor BASYX_JAR is given"
    if not (MGMT / "cell" / "sim" / "run_module.py").is_file():
        return f"no iec61499-mgmt-py checkout at {MGMT}"
    probe = subprocess.run([sys.executable, "-c", "import modreg.model, modsync"], capture_output=True)
    if probe.returncode:
        return "modreg and modsync are not installed (pip install -e iec61499-mgmt-py[registration])"
    if busy := [p for p in PORTS if in_use(p)]:
        return f"ports in use: {busy}"
    return ""


pytestmark = pytest.mark.skipif(bool(missing()), reason=missing() or "all there")


def answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def until(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out: {what}"
        time.sleep(0.5)


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    work = tmp_path_factory.mktemp("e2e")
    processes: list[subprocess.Popen] = []

    def start(args: list[str], name: str) -> subprocess.Popen:
        log = (work / f"{name}.log").open("wb")
        process = subprocess.Popen(args, cwd=MGMT, stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    try:
        start([sys.executable, "cell/sim/run_module.py", "cell/modules/filling.yaml", "--forte", FORTE], "module")
        until(lambda: in_use(4840), 60, "FORTE's OPC UA server")
        basyx = BASYX_URL.rstrip("/")
        if not basyx:
            port = free_port()
            basyx = f"http://127.0.0.1:{port}"
            start(["java", "-jar", BASYX_JAR, f"--server.port={port}"], "basyx")
        until(lambda: answers(f"{basyx}/shells"), 120, "the AAS server")
        registry = f"http://127.0.0.1:{free_port()}"
        start([sys.executable, "-m", "modreg", "serve", "--listen", registry.rsplit(":", 1)[1],
               "--store", str(work / "registry"), "--ontology", "ontology/ARSO", "--basyx", basyx], "modreg")
        until(lambda: answers(f"{registry}/health"), 60, "the registration service")
        pulled = subprocess.run(
            [sys.executable, "-m", "modsync", "pull", "--spec", "cell/modules/filling.yaml", "--target", "pc",
             "--host", "127.0.0.1", "--out", str(work / "aas"), "--register", registry],
            cwd=MGMT, capture_output=True, text=True, timeout=300)
        yield {"basyx": basyx, "registry": registry, "pull": pulled, "work": work}
    finally:
        for process in reversed(processes):
            # run_module.py stops FORTE on Ctrl+C only.
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def test_the_running_module_is_registered_with_its_aas(flow):
    pulled = flow["pull"]
    assert pulled.returncode == 0, pulled.stdout + pulled.stderr
    assert "in sync with cell/modules/filling.yaml target pc" in pulled.stdout, pulled.stdout
    assert "registered FillingModuleAAS" in pulled.stdout, pulled.stdout
    with urllib.request.urlopen(f"{flow['registry']}/profiles/FillingModuleAAS", timeout=10) as response:
        registration = json.loads(response.read())
    assert "0 restrictions broken" in registration["summary"], registration["summary"]


def test_the_hmi_built_from_the_aas_runs_the_module(flow):
    modules = {m.key: m for m in aas.load(flow["basyx"])}
    filling = modules["filling"]
    assert filling.aas_id.endswith("/FillingModuleAAS")
    assert filling.default_endpoint == "opc.tcp://127.0.0.1:4840"         # where the module was read
    # The AAS names the same address space as the hand-written description.
    assert prof.monitored_paths(filling) == prof.monitored_paths(prof.FILLING)
    service = HmiService([ModuleConfig(profile=filling, endpoint=filling.default_endpoint)], sampling_ms=100)
    with TestClient(create_app(service)) as client:
        assert wait_for(lambda: all(link.connected for link in service.links.values()), timeout=30)
        assert view(client, "filling")["missing"] == []                # every node the AAS names is there
        to_execute(client, "filling")
        assert skill_command(client, "filling", "Dispensing", "Start")["accepted"]
        running = wait_skill(client, "filling", "Dispensing", "Running")
        assert running["steps"][0]["name"] == "MoveNeedleDown"
        done = wait_skill(client, "filling", "Dispensing", "Succeeded", timeout=30)
        assert done["results"]["Weight"]["value"] == pytest.approx(2.0)   # the module's simulated scale
        assert done["results"]["Weight"]["unit"] == "g"
        log = [entry["text"] for entry in client.get("/api/log?limit=200").json()["entries"]]
        assert "Dispensing: Running -> Succeeded" in log, log
        client.post("/api/module-command", json={"module": "filling", "command": "Stop"},
                    headers={"X-Session-Id": SESSION})
        assert wait_for(lambda: view(client, "filling")["moduleState"]["name"] == "Stopped", timeout=30)
        client.post("/api/occupation", json={"module": "filling", "action": "release"},
                    headers={"X-Session-Id": SESSION})


def test_an_agent_runs_an_offered_capability_through_the_references_of_the_aas(flow):
    """Capability -> the skill realizing it -> the actions and properties it refers to -> FORTE."""
    [filling] = [r for r in modlink_aas.load(flow["basyx"]) if r.root == "Filling"]
    cap = filling.capability("https://smartproductionlab.aau.dk/semantics/Filling")
    assert cap.skill == "Dispensing" and cap.properties["FillVolume"].admits(2.0)

    async def scenario():
        async with filling.connect(sampling_ms=100) as link:
            assert await link.wait_connected(30)
            assert link.module("Filling").missing == []
            module = Module(link, filling.root, session="e2e-agent", resource=filling)
            await module.occupy()
            await module.bring_to_execute()
            run = await module.run_capability("Filling", timeout=60)
            assert run.skill == "Dispensing" and run.state == SkillState.SUCCEEDED, run
            assert run.results["Weight"] == pytest.approx(2.0)
            await module.command("Stop")
            await module.wait_state(ModuleState.STOPPED, timeout=30)
            await module.release()

    asyncio.run(scenario())
