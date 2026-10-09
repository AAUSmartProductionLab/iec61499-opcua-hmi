"""Write the AAS of the filling and stoppering modules as test data (tests/data/*08.json.gz).

Built by iec61499-mgmt-py's modreg from the module specs, as a registration builds it (ARSO 0.8:
the module's AAS and one for every component of it, in one environment), without the embedded data
specifications (the class and attribute names aas-model repeats on every element, which nothing
here reads). ``*07.json.gz`` are the same modules in ARSO 0.7 (8 Oct 2026: the kind of a skill as its
semantic id, a Module submodel), kept to test that they are still read.
``FillingModuleAAS.json.gz`` and ``StopperingModuleAAS.json.gz`` are the modules as
they were before 8 Oct 2026 (ARSO 0.6, the ESP32 stations' skills), which the HMI's built-in
descriptions and its simulator still are; they cannot be written again. Needs a checkout of iec61499-mgmt-py next to this repository (or
IEC61499_MGMT_PY) with its registration extra installed:

    python tools/aas_fixtures.py
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MGMT = Path(os.environ.get("IEC61499_MGMT_PY", ROOT.parent / "iec61499-mgmt-py"))
OUT = ROOT / "tests" / "data"
MODULES = ("filling", "stoppering")


def strip(element):
    if isinstance(element, dict):
        return {k: strip(v) for k, v in element.items() if k != "embeddedDataSpecifications"}
    if isinstance(element, list):
        return [strip(v) for v in element]
    return element


def environment(module: str) -> dict:
    """The AAS environment modreg registers for a module's pi target."""
    for part in ("iec61499-skill-lib", "iec61499-mgmt-py", "aas61499-tools"):
        if str(MGMT / part) not in sys.path:
            sys.path.insert(0, str(MGMT / part))
    from modgen import SPECS, load
    from modreg import model, profile

    built = [model.build(found) for found in profile.describe_all(load(SPECS / f"{module}.yaml"), "pi")]
    return {"assetAdministrationShells": [shell for env in built for shell in env["assetAdministrationShells"]],
            "submodels": [submodel for env in built for submodel in env["submodels"]]}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for module in MODULES:
        env = strip(environment(module))
        name = env["assetAdministrationShells"][0]["idShort"].removesuffix("AAS")
        target = OUT / f"{name}08.json.gz"
        raw = json.dumps(env, separators=(",", ":"), sort_keys=True).encode()
        target.write_bytes(gzip.compress(raw, 9, mtime=0))
        print(f"{target.relative_to(ROOT)}: {len(raw) // 1024} KB, {target.stat().st_size // 1024} KB packed")


if __name__ == "__main__":
    main()
