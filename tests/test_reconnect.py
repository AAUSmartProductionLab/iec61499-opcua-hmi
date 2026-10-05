"""Connection loss and restart of the controller."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hmi import profiles as prof
from hmi.app import create_app
from hmi.service import HmiService, ModuleConfig

from test_integration import SESSION, Simulator, free_port, wait_for, view


@pytest.fixture(scope="module")
def restartable():
    """A service whose simulated controller can be stopped and started again."""
    port = free_port()
    endpoint = f"opc.tcp://127.0.0.1:{port}"
    service = HmiService([ModuleConfig(profile=prof.FILLING, endpoint=endpoint)], sampling_ms=100)
    simulator = Simulator(["filling"], port=port)
    simulator.start()
    with TestClient(create_app(service)) as client:
        assert wait_for(lambda: service.links[endpoint].state.connected)
        context = {"service": service, "endpoint": endpoint, "simulator": simulator, "client": client}
        yield context
    context["simulator"].stop()


def call(context, path: str, payload: dict) -> dict:
    response = context["client"].post(path, json=payload, headers={"X-Session-Id": SESSION})
    assert response.status_code == 200, response.text
    return response.json()


def test_reconnect_marks_values_stale_and_takes_the_occupation_back(restartable):
    context = restartable
    service: HmiService = context["service"]
    endpoint = context["endpoint"]
    client = context["client"]

    assert call(context, "/api/occupation", {"module": "filling", "action": "occupy"})["accepted"]
    assert wait_for(lambda: view(client, "filling")["occupied"] is True)
    assert view(client, "filling")["occupier"] is True

    context["simulator"].stop()
    assert wait_for(lambda: not service.links[endpoint].state.connected, timeout=20)
    stale = view(client, "filling")
    assert stale["connected"] is False
    assert stale["stale"] is True
    assert stale["commands"]["Reset"] is False
    refused = call(context, "/api/module-command", {"module": "filling", "command": "Reset"})
    assert refused["accepted"] is False
    assert refused["transportError"]

    replacement = Simulator(["filling"], port=int(endpoint.rsplit(":", 1)[1]))
    replacement.start()
    context["simulator"] = replacement

    assert wait_for(lambda: service.links[endpoint].state.connected, timeout=30)
    assert wait_for(lambda: view(client, "filling")["occupied"] is True, timeout=30), (
        "the HMI did not take the module again"
    )
    fresh = view(client, "filling")
    assert fresh["stale"] is False
    assert fresh["occupier"] is True
    assert fresh["moduleState"]["name"] == "Stopped"
    assert fresh["skills"]["MoveNeedleUp"]["stateName"] == "Idle"
    assert call(context, "/api/module-command", {"module": "filling", "command": "Reset"})["accepted"]


def test_incomplete_address_space_is_reported(restartable):
    context = restartable
    service: HmiService = context["service"]
    channel = service.link_for("filling").module("Filling")
    previous = list(channel.missing)
    channel.missing.append("Skills/Ghost/State")
    channel.available = False
    try:
        module = view(context["client"], "filling")
        assert module["connected"] is False
        assert any("Skills/Ghost/State" in item for item in module["missing"])
        assert call(context, "/api/module-command", {"module": "filling", "command": "Reset"})[
            "accepted"
        ] is False
    finally:
        channel.missing[:] = previous
        channel.available = True
    assert view(context["client"], "filling")["connected"] is True