# iec61499-opcua-hmi

A small OPC UA HMI in Python (FastAPI + [asyncua](https://github.com/FreeOpcUa/opcua-asyncio))
for the IEC 61499 modules of the Eclipse 4diac controller. It shows the PackML
module state machine, the skills with their parameters and results, the step
progress of the sequences and the procedures, the sensors, and it operates the
module through methods only.

Both modules described in the specification documents are supported:

| Module | Root object | Skills | Address space |
| --- | --- | --- | --- |
| Filling | `/Objects/Filling` | `MoveNeedleUp`, `MoveNeedleDown`, `AttachNeedle`, `Tare`, `Weigh`, `Dispensing` | `opcua-filling.md` |
| Stoppering | `/Objects/Stoppering` | `LowerPiston`, `RaisePiston`, `ExtendPlunger`, `RetractPlunger`, `MoveArm`, `Stoppering` | `opcua-stoppering.md` |

## Getting started

```bash
pip install -r requirements.txt

# the modules registered on an AAS server (Eclipse BaSyx), each at the endpoint its AAS names
python run.py --aas http://<aas-server>:8081

# against the real controller, with the built-in descriptions
python run.py --modules filling --endpoint filling=opc.tcp://192.168.0.191:4840
python run.py --modules stoppering --endpoint stoppering=opc.tcp://<second-pi>:4840
python run.py --modules filling,stoppering --endpoint filling=opc.tcp://192.168.0.191:4840

# without the machine: the simulator serves the same address space
python run.py --simulate filling,stoppering
```

Then open <http://127.0.0.1:8080>. Add `--host 0.0.0.0` to reach the HMI from
another computer, and `--sim-endpoint opc.tcp://127.0.0.1:4850` if the default
simulator port is taken.

### Command line

| Option | Meaning |
| --- | --- |
| `--aas URL\|FILE` | describe the modules by their AAS: an AAS repository (`http...`, part 2 API) or an AAS environment file (`.json`, `.json.gz`) |
| `--modules filling,stoppering` | which modules to show (default: both built-in ones, or every module the AAS describe) |
| `--endpoint KEY=URL` | endpoint of a module, repeatable |
| `--simulate [KEYS]` | run the simulator for these modules (all when omitted) |
| `--sim-endpoint URL` | endpoint the simulated modules serve on |
| `--fault sensor` | simulator only: end switches never close, so skills that wait for one fail with Timeout (3) |
| `--fault invariant` | simulator only: end switches never open again, so a needle move ends with both on: InvariantViolated (2) |
| `--sim-speed 0.25` | simulator only: quarter speed, so the sequences can be watched in the state machines |
| `--sampling-ms 200` | subscription sampling interval |
| `--host`, `--port` | where the web interface listens |

The module endpoints default to the ones from the specification documents
(`opc.tcp://192.168.0.191:4840` for Filling, `opc.tcp://localhost:4841` for the
simulated Stoppering module).

## Modules from their AAS

A module registers its AAS (iec61499-mgmt-py: `modsync pull --register` sends its profile to
`modreg serve`, which builds the AAS, checks it against the resource ontology and publishes it to
the AAS server). The AAS holds everything the HMI shows, so `--aas` builds the module
descriptions from it (`hmi/aas.py`) instead of taking the built-in ones (`hmi/profiles.py`):

| HMI | From the AAS |
| --- | --- |
| root object, endpoint, every node | Asset Interfaces Description: the OPC UA base and the browse path of every action and property |
| skills: kind, description, parameters (unit, range, default), results (unit), occupied equipment | Skills submodel: each skill with its Parameters, Contract, Occupies; result properties of the interface |
| steps of Execute and Stop, with the constants bound to them | the skill's sequences: each step's skill, instance, bindings and State reference |
| procedures Resetting and Stopping | Skills/Procedures |
| sensors, equipment notes | Hierarchical Structures (the equipment) and its properties in the interface |
| (agents) offered capabilities and the skill realizing each | Capability Description (IDTA 02020), `CapabilityRealizedBy` referring to the skill |

Only shells with a Skills submodel and an OPC UA interface are modules; nothing but plain JSON
is read, so no AAS library is needed. The HMI's own descriptions (`hmi/aas.py`, `hmi/profiles.py`) and its
simulator are still those of the modules before 8 Oct 2026 (the ESP32 stations' skills, AAS in the
structure of ARSO 0.6); `modlink.aas` reads the modules as they are built now as well
(`tests/data/*07.json.gz`). `tests/test_aas.py` checks that the AAS of both modules
(`tests/data`, written by `tools/aas_fixtures.py`) give the same descriptions as the built-in
ones, and runs the simulator and the HMI on a module taken from its AAS.

### The whole flow, with FORTE and BaSyx

`tests/test_end_to_end.py` runs everything for real: 4diac FORTE with the filling module on its
Modbus simulator, `modsync pull --register` into `modreg serve`, Eclipse BaSyx as AAS server,
the HMI built from BaSyx running Dispensing on FORTE, and an agent running the module's Filling
capability through the references of the AAS. It is skipped unless the parts are there:

```bash
tools/forte/build.sh ../forte-build        # FORTE 3.3.0 for Linux with every module (no FBE needed)
# BaSyx AAS environment as a plain jar (Maven Central), no Docker needed:
curl -LO https://repo1.maven.org/maven2/org/eclipse/digitaltwin/basyx/basyx.aasenvironment.component/2.0.0-milestone-15/basyx.aasenvironment.component-2.0.0-milestone-15-exec.jar
FORTE_BIN=../forte-build/build-forte/forte/forte \
BASYX_JAR=basyx.aasenvironment.component-2.0.0-milestone-15-exec.jar python -m pytest tests/test_end_to_end.py
```

`tools/forte/build.sh` does on Linux what iec61499-mgmt-py's `runtime/build-modules.ps1` and
`build-runtime.ps1` do on Windows with the FBE: the 4diac IDE 3.3.0 exports the types of ModLib
and of every module headlessly (under Xvfb), open62541 1.5.4 is built single threaded, and FORTE
3.3.0 with the repository's patches is built with OPC UA, Modbus, IO, GPIO and PWM.

## How it works

```
run.py                 command line entry point: simulator, links and web server on one event loop
modlink/               async OPC UA access to the modules, for the HMI and for agents (below)
hmi/profiles.py        declarative address space of each module (built in, or from the AAS)
hmi/aas.py             the module descriptions from the modules' AAS (file or AAS repository)
hmi/model.py           PackML states, skill states, error codes, button rules, diagrams
hmi/service.py         occupation, command results, message log, snapshot for the page
hmi/app.py             FastAPI routes and the WebSocket that pushes snapshots
hmi/static             the HMI page: diagram.js (state machine drawings), hmi.js, hmi.css
sim/fake_module.py     simulated controller built from the same profiles
tools/                 AAS test data from iec61499-mgmt-py; FORTE for Linux (tools/forte)
tests/                 unit tests and end to end tests against the simulator
```

* **Browse paths, never node ids.** Node ids change on every controller restart,
  so every node is resolved by browse path (`0:Objects / 1:Filling / 1:Module /
  1:State`) at connect time and again after every reconnect. The root object is
  looked up in namespace index 1 first and by browsing if that fails.
* **Subscriptions, not polling.** All variables are monitored with one
  subscription (sampling 100-250 ms). Every change wakes the service, which
  pushes the snapshot to each open page over a WebSocket (`/ws`, at most every
  100 ms); the page polls `/api/snapshot` only while the socket is down. Neither
  touches the controller. The controller notifies changes
  only, so a module at rest is quiet: after 5 s without a notification the link
  reads one value to prove the connection, and reconnects only if that fails.
* **Methods are called on their object.** `Occupation/Occupy` is called on the
  `Occupation` object, `Skills/Weigh/Start` on `Skills/Weigh`. The controller's
  OPC UA server (open62541) refuses any other object with `BadNodeClassInvalid`;
  the simulator does the same, so the tests catch it.
* **Methods only.** Nothing is written to the controller; every command is a
  method call, one at a time, and `Accepted` is always evaluated (the OPC UA
  status is `Good` even when the command is refused).
* **One session id.** The browser creates a UUID, keeps it in local storage and
  sends it with every call. After a reconnect the HMI calls `Occupy` again with
  that id and takes the module back if nobody else has it. After a restart of the
  HMI it asks once with `Occupy` whether the session still owns an occupied
  module (the controller accepts that only for the owner).
* **Reset follows the specification.** `Reset` is accepted only from `Aborted`
  (opcua-filling.md / opcua-stoppering.md, skill command table), so it can be
  pressed there only; a skill in `Succeeded` or `Failed` is re-run with `Start`,
  which needs no `Reset`. A faded command tells why on hover, so it is never a
  dead end.
* **Equipment locks.** A skill holds its equipment while it runs; a module level
  skill holds an equipment from the first step that uses it to the last one, as
  the controller's lock tokens do. Start of a skill that needs held equipment is
  disabled and the card says who holds it.
* **Errors where they belong.** The controller keeps `ErrorID` after Abort and
  Reset; the page shows it only for `Failed` and `Aborted`.
* **Stale values are shown as stale.** On connection loss a warning above the
  module says so, every command is disabled, and the page reconnects by itself.
  Otherwise the page shows no status pills: the drawings show the states.
* **Transitions are logged.** Every refused command with its ErrorID and text,
  and every skill that ends as Failed, goes to the message log, whether or not a
  page is open.
* **A composite skill links its primitives.** A module level skill such as
  `Dispensing` or `Stoppering` does not write the standalone variables of the
  skills it runs, only its own step variables. While such a skill is Running, the
  primitive skill card of the current step is marked, shows the step's state and
  its diagram, and its `Start` is disabled because the composite holds the
  equipment. The step names the primitive it runs (`ArmIn` runs `MoveArm`), and
  the steps of a stop sequence are linked as well. Everything comes from the same subscription as everything else.
* **Commands are pressed on the drawings.** There are no command buttons: every
  command bubble on a drawing is its button (click, or Enter or Space when it has
  the focus), live when the controller would accept the command and faded when
  not. The module is commanded on its state machine, a skill on its card, and the
  occupation on its own small drawing (Free, This HMI, Another session) with
  `Occupy` and `Release`. Start takes the parameters typed on the card.
* **State machine drawings.** Both machines are drawn in the PackML style: blue
  acting states, orange wait states, every command in a bubble on its line, and
  unlabelled lines for state complete. A command that applies to several states
  leaves a dashed frame around them: module Stop from the inner frame, module
  Abort from the outer one; skill Start from the frame around `Idle`, `Succeeded`
  and `Failed`, skill Abort from the frame around every state but `Aborted`.
  `Stop` passes `Stopping` and ends in `Failed` (ErrorID 7); a skill that
  succeeded returns to `Idle` by itself after 1.5 s, and `Reset` (or the module in
  `Clearing` or `Stopped`) brings an aborted one back, as in `SKILL_Control`. The
  dot follows the line from the previous state. Every line has its own lane, which `tests/test_model.py`
  checks: no line through a box or across another line. No box is painted
  over: the current state breathes, and a small orange dot travels along the
  transition it just came through. The page has a light theme by default and a dark theme; the button
  in the header switches and remembers the choice, and `?theme=dark` in the URL
  forces one.
* **Three panels of skills.** `Sequences` holds the module level skills with their
  step lists, `Skills` the single motions and operations.

## modlink: the modules for agents

`modlink` is the OPC UA side of the HMI on its own, asyncio only and without
anything of the web app, so an agent (or a script) drives a module the same way.
Given the module's AAS it needs no knowledge of the address space: it follows
the references of the AAS from an offered capability to the skill realizing it
and on to the browse paths that skill's commands and properties are at.

```python
import asyncio
from modlink import Module, aas

async def main():
    [filling] = aas.load("http://<aas-server>:8081")      # or an AAS environment file
    async with filling.connect() as link:                  # the endpoint the AAS names
        await link.wait_connected(10)
        module = Module(link, filling.root, session="agent-1", resource=filling)
        await module.occupy()
        await module.bring_to_execute()
        run = await module.run_capability("Filling")       # realized by Dispensing
        print(run.skill, run.state, run.error_id, run.results)
        await module.release()

asyncio.run(main())
```

| Part | What it does |
| --- | --- |
| `aas` | reads a module's AAS into a `Resource`: offered capabilities with their values and ranges and the skill realizing each (`CapabilityRealizedBy`), every skill with the browse paths of its commands, state, ErrorID and results and its parameters in call order (unit, range, default), the module's commands, state and occupation, and the `Interface` of the module. It reads the structure of ARSO 0.7 (a skill is its commands, each with an `InterfaceReference` and an Operation; the module's primitives are in the AASs of its components, read with it; state and results are found through the mapping configuration by what a data point means) and the one before it (`Methods`, `StateReference`) |
| `Interface` | the variables and methods of one module by browse path; `discover` browses them from the server |
| `Link` | one supervised connection per endpoint: resolve, one subscription, calls on the parent object, probe when quiet, reconnect with back-off; listeners and `changes()` for every value change |
| `Module` | one module for one occupation session: `occupy`, `command`, `skill`, `run` (start and wait for the end), `run_capability`, `wait_state`; browse paths from the `Resource`, else by the module's naming conventions |
| `codes` | `ModuleState`, `SkillState`, `ErrorId` as in the controller's ModLib |

## API

The page uses these routes; they are handy for scripting as well. Every request
carries the session id in the `X-Session-Id` header (or a `session` query
parameter for `GET /api/snapshot`).

| Route | Body | Answer |
| --- | --- | --- |
| `GET /api/config` | | profiles, states, error codes |
| `GET /api/snapshot` | | values, states, button rules, log for one session |
| `GET /api/log?limit=60` | | message log |
| `WS /ws?session=<id>` | | the snapshot, pushed on every change |
| `POST /api/occupation` | `{"module": "filling", "action": "occupy"\|"release"}` | `accepted`, `errorId` |
| `POST /api/module-command` | `{"module": "filling", "command": "Reset"\|"Start"\|"Stop"\|"Abort"\|"Clear"}` | `accepted`, `errorId` |
| `POST /api/skill-command` | `{"module": "filling", "skill": "Weigh", "command": "Start"\|"Stop"\|"Abort"\|"Reset", "params": {"Duration": 2.0}}` | `accepted`, `errorId` |

```bash
curl -X POST http://127.0.0.1:8080/api/occupation \
     -H "X-Session-Id: $(uuidgen)" -H "Content-Type: application/json" \
     -d '{"module": "filling", "action": "occupy"}'
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

`tests/test_model.py` and `tests/test_profiles.py` check the state machines,
error codes, button rules and the address space against the specification
documents, including that every state of both machines appears in the diagrams.
`tests/test_controller_sync.py` reads a checkout of iec61499-mgmt-py next to this
one (or `IEC61499_MGMT_PY`) and checks the profiles against the module specs in
`cell/modules`, the state and error numbers against `modgen`, and the command
rules and the skill diagram against the generated `MOD_StateLogic` and
`SKILL_Control`; it is skipped without that checkout. `tests/test_modlink.py`
checks discovery, the method calls, a quiet connection and an agent running skills, `tests/test_simulator.py` the simulated
equipment against the module specs.
`tests/test_integration.py` and `tests/test_reconnect.py` start the simulator and
drive the whole API: occupy/release, reset/start/execute, skill starts, equipment
locks, abort and clear, refusals with their ErrorID, the sequences and a
controller restart with automatic re-occupation.

## Simulator

`sim/fake_module.py` builds the address space from the same profiles as the
client, so node names, types and defaults always match. It implements both state
machines as the controller runs them, the occupation, the equipment locks,
the equipment of `cell/modules/filling.yaml` and `stoppering.yaml` and the documented
refusals. As on the controller, a skill
returns from `Succeeded` to `Idle` after 1.5 s (steps too), stays in `Failed`
until the next Start or Abort, a Stop passes
`Stopping` and ends in `Failed` with Interrupted (7), a module Abort aborts every
skill and step, and a module level skill accepts Start and fails with Busy (6)
when a step finds its equipment held; a Stop leaves the steps of a sequence or of
a running Resetting procedure to their parent. The equipment moves as in
`cell/sim/module_sim.py`: the needle travels in 2 s after a 0.15 s dead time with
its 200 ms start boost, the piston in 3 s from half way, the plunger in 8 s; a
skill that waits for an end switch succeeds at once when it is already there,
open-loop skills run exactly their duration. The scale has no hardware, so the
module reads a constant 2.0 g. The simulator goes further: it weighs an empty vial
(9.75 g), `Tare` zeroes the reading, and while the needle is down in the dwell of
`Dispensing` 3 mL per second flow into the vial, at a density of 0.98 to 1.06 g/mL,
so the default 1 s dwell gives about 3000 mg. It is a development
and test aid, not a
controller: it has no start conditions, no stop sequences except the ones in the
documents, and no safety functions.