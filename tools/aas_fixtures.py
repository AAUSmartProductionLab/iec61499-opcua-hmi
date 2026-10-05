"""Write the AAS of the filling and stoppering modules as test data (tests/data/*.json.gz).

Built by iec61499-mgmt-py's modreg from the module specs, as a registration builds it, without the
embedded data specifications (the class and attribute names aas-model repeats on every element,
which the HMI does not read). Needs a checkout of iec61499-mgmt-py next to this repository (or
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

    return model.build(profile.describe(load(SPECS / f"{module}.yaml"), "pi"))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for module in MODULES:
        env = strip(environment(module))
        name = env["assetAdministrationShells"][0]["idShort"]
        target = OUT / f"{name}.json.gz"
        raw = json.dumps(env, separators=(",", ":"), sort_keys=True).encode()
        target.write_bytes(gzip.compress(raw, 9, mtime=0))
        print(f"{target.relative_to(ROOT)}: {len(raw) // 1024} KB, {target.stat().st_size // 1024} KB packed")


if __name__ == "__main__":
    main()
