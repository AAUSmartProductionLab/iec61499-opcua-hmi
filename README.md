# iec61499-opcua-hmi

A small OPC UA HMI in Python (Flask + [asyncua](https://github.com/FreeOpcUa/python-opcua))
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

# against the real controller
python run.py --modules filling --endpoint-filling opc.tcp://192.168.0.191:4840
python run.py --modules stoppering --endpoint-stoppering opc.tcp://<second-pi>:4840
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
| `--modules filling,stoppering` | which modules to show |
| `--endpoint KEY=URL` | endpoint of a module, repeatable |
| `--simulate [KEYS]` | run the simulator for these modules (all when omitted) |
| `--sim-endpoint URL` | endpoint the simulated modules serve on |
| `--fault sensor` | simulator only: end switch sensors never reach their target, so skills fail with Timeout (3) |
| `--fault invariant` | simulator only: both needle end switches are on, so the needle skills fail with InvariantViolated (2) |
| `--sim-speed 0.25` | simulator only: quarter speed, so the sequences can be watched in the state machines |
| `--sampling-ms 200` | subscription sampling interval |
| `--host`, `--port` | where the web interface listens |

The module endpoints default to the ones from the specification documents
(`opc.tcp://192.168.0.191:4840` for Filling, `opc.tcp://localhost:4841` for the
simulated Stoppering module).

## How it works

```
run.py                 command line entry point
hmi/profiles.py        declarative address space of each module (single source of truth)
hmi/model.py           PackML states, skill states, error codes, button rules, diagrams
hmi/link.py            OPC UA client: connect, resolve, subscribe, call, reconnect
hmi/service.py         occupation, command results, message log, snapshot for the page
hmi/app.py             Flask routes
hmi/static             the HMI page: diagram.js (state machine drawings), hmi.js, hmi.css
sim/fake_module.py     simulated controller built from the same profiles
tests/                 unit tests and end to end tests against the simulator
```

* **Browse paths, never node ids.** Node ids change on every controller restart,
  so every node is resolved by browse path (`0:Objects / 1:Filling / 1:Module /
  1:State`) at connect time and again after every reconnect. The root object is
  looked up in namespace index 1 first and by browsing if that fails.
* **Subscriptions, not polling.** All variables are monitored with one
  subscription (sampling 100-250 ms). The browser polls the Flask snapshot every
  250 ms, which never touches the controller.
* **Methods only.** Nothing is written to the controller; every command is a
  method call, one at a time, and `Accepted` is always evaluated (the OPC UA
  status is `Good` even when the command is refused).
* **One session id.** The browser creates a UUID, keeps it in local storage and
  sends it with every call. After a reconnect the HMI calls `Occupy` again with
  that id and takes the module back if nobody else has it.
* **Reset follows the specification.** `Reset` is accepted only from `Aborted`
  (opcua-filling.md / opcua-stoppering.md, skill command table), so the button
  is enabled there only; a skill in `Succeeded` or `Failed` is re-run with
  `Start`, which needs no `Reset`. Each skill card states which applies in its
  current state, so the greyed button is never a dead end.
* **Stale values are shown as stale.** On connection loss all values are marked
  stale and every command is disabled; the page reconnects by itself.
* **Transitions are logged.** Every refused command with its ErrorID and text,
  and every skill that ends as Failed, goes to the message log.
* **A composite skill links its primitives.** A module level skill such as
  `Dispensing` or `Stoppering` does not write the standalone variables of the
  skills it runs, only its own step variables. While such a skill is Running, the
  primitive skill card of the current step is marked, shows the step's state and
  its diagram, and its `Start` is disabled because the composite holds the
  equipment. Everything comes from the same subscription as everything else.
* **State machine drawings.** The module state machine (PackML) is drawn in the
  PackML style: blue acting states, orange wait states, PackML command bubbles on
  the lines and `SC` for state complete. Every skill card carries the reduced
  skill machine: `Idle → Running`, the three end states `Succeeded`, `Failed` and
  `Aborted`, and a return to `Idle` from each of them. `Stopping` is left out on
  purpose, it is too short to see and the machine clears itself. No box is painted
  over: the current state breathes, and a dot travels along the transition it just
  came through. The page has a light theme by default and a dark theme; the button
  in the header switches and remembers the choice, and `?theme=dark` in the URL
  forces one.
* **Three panels of skills.** `Sequences` holds the module level skills with their
  step lists, `Skills` the single motions and operations.

## API

The page uses these routes; they are handy for scripting as well. Every request
carries the session id in the `X-Session-Id` header (or a `session` query
parameter for `GET /api/snapshot`).

| Route | Body | Answer |
| --- | --- | --- |
| `GET /api/config` | | profiles, states, error codes |
| `GET /api/snapshot` | | values, states, button rules, log for one session |
| `GET /api/log?limit=60` | | message log |
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
`tests/test_integration.py` and `tests/test_reconnect.py` start the simulator and
drive the whole API: occupy/release, reset/start/execute, skill starts, equipment
locks, abort and clear, refusals with their ErrorID, the sequences and a
controller restart with automatic re-occupation.

## Simulator

`sim/fake_module.py` builds the address space from the same profiles as the
client, so node names, types and defaults always match. It implements both state
machines, the occupation, the equipment locks, realistic motion times and the
documented refusals. A skill that reached its goal returns to `Idle` by itself
after a short pause, as a machine that clears itself would; `Failed` and
`Aborted` stay until the next command. The needle axis is a single axis: its two
end switches are never both on, which is why a move does not succeed at once. The
scale has no hardware, so a random weight is published about once per second and
`Tare` brings it back to zero. It is a development and test aid, not a
controller: it has no start conditions, no stop sequences except the ones in the
documents, and no safety functions.