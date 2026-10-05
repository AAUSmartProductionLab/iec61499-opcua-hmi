"""Linux version of iec61499-mgmt-py's runtime/build-modules.ps1 and validate.ps1: export ModLib and
every module project with the 4diac IDE (headless, under Xvfb) and post-process the exports the
same way (generic comm FBs and unexported types removed, forte-compat headers, links).

    python export_types.py WORK_DIR [PROJECT ...]     # WORK_DIR holds 4diac-ide/; exports go to WORK_DIR/modules
"""
import json
import re
import shutil
import subprocess
import sys
import os
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MGMT = Path(os.environ.get("IEC61499_MGMT_PY", ROOT.parent / "iec61499-mgmt-py"))
RUNTIME = MGMT / "runtime"
WORK = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
IDE = WORK / "4diac-ide" / "4diac-ide"
OUT = WORK / "modules"


def export(project: str, directory: str, manifest: str, package: str) -> None:
    target = OUT / package
    if target.exists():
        shutil.rmtree(target)
    workspace = WORK / f"ws-{uuid.uuid4().hex}"
    log = WORK / "logs" / f"export-{project}.log"
    log.parent.mkdir(exist_ok=True)
    with log.open("w") as out:
        result = subprocess.run(
            ["xvfb-run", "-a", str(IDE), "-nosplash", "--launcher.suppressErrors",
             "-application", "org.eclipse.ant.core.antRunner", "-data", str(workspace),
             "-buildfile", str(RUNTIME / "validate.xml"), f"-Dproject.path={directory}",
             f"-Dproject.name={project}", f"-Dexport.path={target}", "-vmargs", "-Djava.awt.headless=true"],
            stdout=out, stderr=subprocess.STDOUT)
    text = log.read_text()
    eclipse = workspace / ".metadata" / ".log"
    if eclipse.exists() and re.search("Error loading type|Application error|Error during template generation",
                                      eclipse.read_text()):
        sys.exit(f"IDE loading/export errors: {eclipse}")
    if result.returncode or "BUILD SUCCESSFUL" not in text:
        sys.exit(f"export of {project} failed, see {log}")
    types = json.loads(Path(manifest).read_text())
    skip = [t["type"].split("::")[-1] for t in types if not t["exported"]]
    skip += ["GEN_SERVER", "GEN_CLIENT", "GEN_PUBLISH", "GEN_SUBSCRIBE"]
    for short in skip:
        for f in target.rglob(f"{short}_*"):
            f.unlink()
        for lists in target.rglob("CMakeLists.txt"):
            lines = lists.read_text().splitlines()
            kept = [line for line in lines if not re.search(rf"\b{short}_(fbt|dtp)\.(cpp|h)\b", line)]
            lists.write_text("\n".join(kept) + "\n")
    shutil.copytree(RUNTIME / "forte-compat" / "include", target / "include", dirs_exist_ok=True)
    with (target / "CMakeLists.txt").open("a") as f:
        f.write(f"""
# Added by validate.ps1: headers of standard FBs used inside the composites.
foreach (lib forte-events forte-net)
        if (TARGET ${{lib}})
                target_link_libraries(forte-{package} PUBLIC ${{lib}})
        endif ()
endforeach ()
""")
        if project != "ModLib":
            f.write(f"target_link_libraries(forte-{package} PUBLIC forte-modlib)\n")
    expected = sum(1 for t in types if t["exported"])
    exported = [*target.rglob("*_fbt.cpp"), *target.rglob("*_dtp.cpp")]
    if len(exported) != expected:
        sys.exit(f"{project}: expected {expected} exported types, got {len(exported)}")
    shutil.rmtree(workspace, ignore_errors=True)
    print(f"{project}: exported {expected} types to {target}")


def main() -> None:
    listing = subprocess.run([sys.executable, "-m", "modgen", "--list"], cwd=MGMT, check=True,
                             capture_output=True, text=True).stdout.split()
    wanted = set(sys.argv[2:])
    for line in listing:
        project, directory, manifest, package = line.split("|")
        if not wanted or project in wanted or package in wanted:
            export(project, directory, manifest, package)


if __name__ == "__main__":
    main()
