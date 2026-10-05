"""The numbers a module publishes: PackML module states, skill states and ErrorIDs.

The same tables as the controller's ModLib (iec61499-mgmt-py: MOD_StateLogic,
SKILL_Control). They are IntEnums, so they compare equal to the plain values
read over OPC UA.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ModuleState(IntEnum):
    """PackML state of the module (``Module/State``)."""

    CLEARING = 1
    STOPPED = 2
    STARTING = 3
    IDLE = 4
    EXECUTE = 6
    STOPPING = 7
    ABORTING = 8
    ABORTED = 9
    RESETTING = 15


class SkillState(IntEnum):
    """State of a skill, a step or a procedure step (``.../State``)."""

    IDLE = 0
    RUNNING = 1
    STOPPING = 2
    SUCCEEDED = 3
    FAILED = 4
    ABORTED = 5


class ErrorId(IntEnum):
    """Why a call was refused or a skill failed (``ErrorID``)."""

    NONE = 0
    PRECONDITION_VIOLATED = 1
    INVARIANT_VIOLATED = 2
    TIMEOUT = 3
    NOT_READY = 4
    NOT_PERMITTED = 5
    BUSY = 6
    INTERRUPTED = 7
    OUT_OF_RANGE = 8


# The names the controller and the AAS use (PascalCase).
MODULE_STATE_NAMES: dict[int, str] = {s.value: s.name.title() for s in ModuleState}
SKILL_STATE_NAMES: dict[int, str] = {s.value: s.name.title() for s in SkillState}
ERROR_NAMES: dict[int, str] = {
    e.value: "".join(part.title() for part in e.name.split("_")) for e in ErrorId
}

MODULE_COMMANDS: tuple[str, ...] = ("Reset", "Start", "Stop", "Abort", "Clear")
SKILL_COMMANDS: tuple[str, ...] = ("Start", "Stop", "Abort", "Reset")

# A skill run is over in these states.
SKILL_ENDS = frozenset({SkillState.SUCCEEDED, SkillState.FAILED, SkillState.ABORTED})


def as_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
