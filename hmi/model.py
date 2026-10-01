"""State machines, error codes and command rules shared by all modules.

Everything in here is pure data or pure functions over the OPC UA values so it
can be unit tested without a controller.
"""

from __future__ import annotations

from typing import Any

# --- PackML module states -------------------------------------------------

STOPPED = 2
STARTING = 3
IDLE = 4
EXECUTE = 6
STOPPING = 7
ABORTING = 8
ABORTED = 9
CLEARING = 1
RESETTING = 15

MODULE_STATES: dict[int, str] = {
    CLEARING: "Clearing",
    STOPPED: "Stopped",
    STARTING: "Starting",
    IDLE: "Idle",
    EXECUTE: "Execute",
    STOPPING: "Stopping",
    ABORTING: "Aborting",
    ABORTED: "Aborted",
    RESETTING: "Resetting",
}

MODULE_STATE_KIND: dict[int, str] = {
    CLEARING: "acts",
    STOPPED: "waits",
    STARTING: "acts",
    IDLE: "waits",
    EXECUTE: "waits",
    STOPPING: "acts",
    ABORTING: "acts",
    ABORTED: "waits",
    RESETTING: "acts",
}

# --- Skill states --------------------------------------------------------

SK_IDLE = 0
SK_RUNNING = 1
SK_STOPPING = 2
SK_SUCCEEDED = 3
SK_FAILED = 4
SK_ABORTED = 5

SKILL_STATES: dict[int, str] = {
    SK_IDLE: "Idle",
    SK_RUNNING: "Running",
    SK_STOPPING: "Stopping",
    SK_SUCCEEDED: "Succeeded",
    SK_FAILED: "Failed",
    SK_ABORTED: "Aborted",
}

SKILL_STATES_CSS: dict[int, str] = {
    SK_IDLE: "idle",
    SK_RUNNING: "running",
    SK_STOPPING: "stopping",
    SK_SUCCEEDED: "ok",
    SK_FAILED: "failed",
    SK_ABORTED: "aborted",
}

SKILL_STATE_KIND: dict[int, str] = {
    SK_IDLE: "waits",
    SK_RUNNING: "acts",
    SK_STOPPING: "acts",
    SK_SUCCEEDED: "waits",
    SK_FAILED: "waits",
    SK_ABORTED: "waits",
}

# --- Error codes ---------------------------------------------------------

ERR_NONE = 0
ERR_PRECONDITION = 1
ERR_INVARIANT = 2
ERR_TIMEOUT = 3
ERR_NOT_READY = 4
ERR_NOT_PERMITTED = 5
ERR_BUSY = 6
ERR_INTERRUPTED = 7
ERR_OUT_OF_RANGE = 8

ERROR_CODES: dict[int, str] = {
    ERR_NONE: "None",
    ERR_PRECONDITION: "PreconditionViolated",
    ERR_INVARIANT: "InvariantViolated",
    ERR_TIMEOUT: "Timeout",
    ERR_NOT_READY: "NotReady",
    ERR_NOT_PERMITTED: "NotPermitted",
    ERR_BUSY: "Busy",
    ERR_INTERRUPTED: "Interrupted",
    ERR_OUT_OF_RANGE: "OutOfRange",
}

ERROR_TEXT: dict[int, str] = {
    ERR_NONE: "No error",
    ERR_PRECONDITION: "The start condition of the skill does not hold.",
    ERR_INVARIANT: "A condition that must hold during the run was broken.",
    ERR_TIMEOUT: "The end condition was not reached in time.",
    ERR_NOT_READY: "Not valid in the current state.",
    ERR_NOT_PERMITTED: "This session does not occupy the module.",
    ERR_BUSY: "Already running, or the equipment is held by another skill.",
    ERR_INTERRUPTED: "Stopped or aborted while running.",
    ERR_OUT_OF_RANGE: "A start argument is outside the parameter range.",
}


def error_text(error_id: Any) -> str:
    """Human readable text for an ErrorID read from the controller."""
    try:
        code = int(error_id)
    except (TypeError, ValueError):
        return "unknown"
    name = ERROR_CODES.get(code)
    if name is None:
        return f"unknown error {code}"
    return f"{code} {name}: {ERROR_TEXT[code]}"


def state_name(value: Any, table: dict[int, str]) -> str:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return "unknown"
    return table.get(code, f"unknown ({code})")


def module_state_name(value: Any) -> str:
    return state_name(value, MODULE_STATES)


def skill_state_name(value: Any) -> str:
    return state_name(value, SKILL_STATES)


# --- Commands ------------------------------------------------------------

MODULE_COMMANDS: tuple[str, ...] = ("Reset", "Start", "Stop", "Abort", "Clear")
SKILL_COMMANDS: tuple[str, ...] = ("Start", "Stop", "Abort", "Reset")

MODULE_COMMAND_FROM_STATES: dict[str, frozenset[int]] = {
    "Reset": frozenset({STOPPED}),
    "Start": frozenset({IDLE}),
    "Stop": frozenset({RESETTING, IDLE, EXECUTE}),
    "Abort": frozenset({STOPPED, RESETTING, IDLE, EXECUTE, STOPPING}),
    "Clear": frozenset({ABORTED}),
}

MODULE_COMMAND_TARGET: dict[str, int | None] = {
    "Reset": RESETTING,
    "Start": EXECUTE,
    "Stop": STOPPED,
    "Abort": ABORTED,
    "Clear": STOPPED,
}

SKILL_STATES_ALLOWED_TO_START = frozenset({SK_IDLE, SK_SUCCEEDED, SK_FAILED})

# --- Diagrams --------------------------------------------------------------
#
# Both machines are the ones the controller runs (iec61499-mgmt-py, ModLib:
# MOD_StateLogic and SKILL_Control); "SC" is state complete.

MODULE_DIAGRAM: tuple[tuple[int, int, str], ...] = (
    (STOPPED, RESETTING, "Reset"),
    (RESETTING, IDLE, "SC"),
    (RESETTING, ABORTING, "Abort, Resetting procedure failed"),
    (IDLE, STARTING, "Start"),
    (STARTING, EXECUTE, "SC"),
    (RESETTING, STOPPING, "Stop"),
    (IDLE, STOPPING, "Stop"),
    (EXECUTE, STOPPING, "Stop"),
    (STOPPING, STOPPED, "SC"),
    (STOPPING, ABORTING, "Abort, stop timeout, Stopping procedure failed"),
    (STOPPED, ABORTING, "Abort"),
    (IDLE, ABORTING, "Abort"),
    (EXECUTE, ABORTING, "Abort"),
    (ABORTING, ABORTED, "SC"),
    (ABORTED, CLEARING, "Clear"),
    (CLEARING, STOPPED, "SC"),
)

SKILL_DIAGRAM: tuple[tuple[int, int, str, str], ...] = (
    (SK_IDLE, SK_RUNNING, "Start", "command"),
    (SK_SUCCEEDED, SK_RUNNING, "Start", "command"),
    (SK_FAILED, SK_RUNNING, "Start", "command"),
    (SK_RUNNING, SK_SUCCEEDED, "goal reached", "sc"),
    (SK_RUNNING, SK_FAILED, "fault 1, 2, 3, 6", "sc"),
    (SK_RUNNING, SK_STOPPING, "Stop, module Stop", "command"),
    (SK_STOPPING, SK_FAILED, "stopped, ErrorID 7", "sc"),
    (SK_IDLE, SK_ABORTED, "Abort, module Abort", "command"),
    (SK_RUNNING, SK_ABORTED, "Abort, module Abort", "command"),
    (SK_STOPPING, SK_ABORTED, "Abort, module Abort", "command"),
    (SK_SUCCEEDED, SK_ABORTED, "Abort, module Abort", "command"),
    (SK_FAILED, SK_ABORTED, "Abort, module Abort", "command"),
    (SK_ABORTED, SK_IDLE, "Reset, module Clearing or Stopped", "command"),
)

# ErrorID keeps its last value in Idle and after an abort of a skill that was
# not active (the controller clears it only on Start and on success), so it
# describes the current state only in these states.
SKILL_STATES_WITH_ERROR = frozenset({SK_FAILED, SK_ABORTED})


def shown_error(skill_state: Any, error_id: Any) -> int:
    """The ErrorID that belongs to the current skill state, else 0."""
    try:
        state, code = int(skill_state), int(error_id)
    except (TypeError, ValueError):
        return 0
    return code if state in SKILL_STATES_WITH_ERROR else 0


def module_command_enabled(command: str, state: Any, occupant: bool) -> bool:
    """Whether a module command button should be enabled.

    Mirrors the acceptance rules of the controller (section 4 of the specs).
    """
    if not occupant or command not in MODULE_COMMAND_FROM_STATES:
        return False
    try:
        code = int(state)
    except (TypeError, ValueError):
        return False
    return code in MODULE_COMMAND_FROM_STATES[command]


def skill_command_enabled(
    command: str,
    skill_state: Any,
    module_state: Any,
    occupant: bool,
    parameters_in_range: bool = True,
    equipment_free: bool = True,
) -> bool:
    """Whether a skill command button should be enabled.

    Mirrors the acceptance rules of the controller (sections 5 and 10).
    ``equipment_free`` is False while another skill holds equipment this one
    needs: the controller refuses a primitive with Busy (6) and lets a module
    level skill fail with Busy once its step finds the equipment taken.
    """
    if not occupant or command not in SKILL_COMMANDS:
        return False
    try:
        skill = int(skill_state)
    except (TypeError, ValueError):
        return False
    try:
        module = int(module_state)
    except (TypeError, ValueError):
        return False
    if command == "Start":
        return (
            module == EXECUTE
            and skill in SKILL_STATES_ALLOWED_TO_START
            and parameters_in_range
            and equipment_free
        )
    if command == "Stop":
        return skill == SK_RUNNING
    if command == "Abort":
        return skill != SK_ABORTED
    if command == "Reset":
        return skill == SK_ABORTED
    return False