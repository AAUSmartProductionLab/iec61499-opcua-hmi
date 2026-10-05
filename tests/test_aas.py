"""The module descriptions built from the modules' AAS (hmi/aas.py).

The AAS in tests/data are what iec61499-mgmt-py's modreg registers for the two modules
(tools/aas_fixtures.py writes them). Built from them, the profiles must describe the same address
space as the hand-written ones in hmi/profiles.py, and the same skills, sequences and procedures.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hmi import aas
from hmi import profiles as prof

DATA = Path(__file__).resolve().parent / "data"
FILES = {"filling": DATA / "FillingModuleAAS.json.gz", "stoppering": DATA / "StopperingModuleAAS.json.gz"}


@pytest.fixture(scope="module")
def built() -> dict[str, prof.ModuleProfile]:
    return {key: aas.load(str(path))[0] for key, path in FILES.items()}


def shape(params) -> list[tuple]:
    return [(p.name, p.unit, p.minimum, p.maximum, p.default) for p in params]


@pytest.mark.parametrize("key", list(FILES))
def test_the_address_space_is_the_one_the_hand_written_profile_names(built, key):
    module, hand = built[key], prof.get_profile(key)
    assert (module.key, module.root) == (hand.key, hand.root)
    assert prof.monitored_paths(module) == prof.monitored_paths(hand)
    assert prof.method_paths(module) == prof.method_paths(hand)
    assert prof.interface(module).root == hand.root


@pytest.mark.parametrize("key", list(FILES))
def test_skills_sequences_and_procedures_match(built, key):
    module, hand = built[key], prof.get_profile(key)
    assert [s.name for s in module.skills] == [s.name for s in hand.skills]
    for got, want in zip(module.skills, hand.skills):
        assert got.module_level == want.module_level, got.name
        assert got.uses == want.uses, got.name
        assert shape(got.params) == shape(want.params), got.name
        assert shape(got.results) == shape(want.results), got.name
        for steps, wanted in ((got.steps, want.steps), (got.stop_steps, want.stop_steps)):
            assert [(s.name, s.runs, s.uses) for s in steps] == [(s.name, s.runs, s.uses) for s in wanted]
            # A step's parameters with the constants bound to it.
            assert [[(p.name, p.unit, p.default) for p in s.params] for s in steps] == \
                   [[(p.name, p.unit, p.default) for p in s.params] for s in wanted]
    assert [p.name for p in module.procedures] == [p.name for p in hand.procedures]
    for got, want in zip(module.procedures, hand.procedures):
        assert [(s.name, s.runs, [(p.name, p.default) for p in s.params]) for s in got.steps] == \
               [(s.name, s.runs, [(p.name, p.default) for p in s.params]) for s in want.steps]
    assert [(s.name, s.equipment, s.kind, s.unit) for s in module.sensors] == \
           [(s.name, s.equipment, s.kind, s.unit) for s in hand.sensors]


def test_what_the_page_shows_comes_from_the_aas_as_well(built):
    filling = built["filling"]
    assert filling.default_endpoint == "opc.tcp://192.168.0.191:4840"
    assert filling.aas_id == "https://smartproductionlab.aau.dk/aas/FillingModuleAAS"
    assert filling.title == "Filling" and "IEC 61499" in filling.summary
    up = next(s for s in filling.skills if s.name == "MoveNeedleUp")
    assert up.label == "Move needle up"
    assert up.description.startswith("Needle up to the top end switch.")
    assert "Timeout (3) after 8s" in up.description and "InvariantViolated" in up.description
    assert any(note.startswith("NeedleAxis: Needle lift") for note in filling.equipment_notes)
    arm_in = next(s for s in built["stoppering"].skills if s.name == "Stoppering").steps[1]
    assert arm_in.label.startswith("Arm in: ")
    assert json.dumps(filling.to_dict())                       # what /api/config sends


def test_a_shell_that_is_not_a_module_is_left_out():
    env = aas.read_file(FILES["filling"])
    station = {"idShort": "Station", "id": "urn:station", "modelType": "AssetAdministrationShell", "submodels": []}
    env["assetAdministrationShells"].append(station)
    modules = [aas.module_profile(e) for e in aas.environments(env) if e.is_module]
    assert [m.key for m in modules] == ["filling"]
    with pytest.raises(aas.AasError):
        aas.module_profile(aas.environments(env)[-1])


class _Repository(BaseHTTPRequestHandler):
    """The read side of an AAS repository (part 2 HTTP API), paged like BaSyx."""

    env: dict = {}

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        shells = self.env["assetAdministrationShells"]
        if path == "/shells":
            first = "cursor=1" not in query
            body = {"result": shells[:1] if first else shells[1:],
                    "paging_metadata": {"cursor": "1"} if first and len(shells) > 1 else {}}
        elif path.startswith("/submodels/"):
            found = [s for s in self.env["submodels"] if aas.b64(s["id"]) == path.rsplit("/", 1)[1]]
            if not found:
                self.send_error(404)
                return
            body = found[0]
        else:
            self.send_error(404)
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def test_modules_are_read_from_an_aas_repository(built):
    envs = [aas.read_file(path) for path in FILES.values()]
    _Repository.env = {"assetAdministrationShells": [s for e in envs for s in e["assetAdministrationShells"]],
                       "submodels": [s for e in envs for s in e["submodels"]]}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Repository)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        modules = aas.load(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
    assert [m.key for m in modules] == ["filling", "stoppering"]          # both pages of shells
    assert [m.to_dict() for m in modules] == [built["filling"].to_dict(), built["stoppering"].to_dict()]
