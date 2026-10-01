"""from hmi import model"""

import re
from pathlib import Path

from hmi import model

DIAGRAM_SOURCE = (Path(__file__).resolve().parents[1] / "hmi" / "static" / "diagram.js").read_text(
    encoding="utf-8"
)
_MINI_BLOCK = DIAGRAM_SOURCE.split("const MINI_POSITIONS = {", 1)[1].split("};", 1)[0]
MINI_SKILL_POSITIONS = {
    int(code): (int(x), int(y))
    for code, x, y in re.findall(r"^\s+(\d+):\s*\[\s*(\d+)\s*,\s*(\d+)\s*\]", _MINI_BLOCK, re.M)
}
MINI_SKILL_HEIGHT = int(
    re.search(r"const MINI_BOX = \{ w: \d+, h: (\d+) \}", DIAGRAM_SOURCE).group(1)
)
MINI_SKILL_WIDTH = int(
    re.search(r"const MINI_BOX = \{ w: (\d+), h: \d+ \}", DIAGRAM_SOURCE).group(1)
)


def test_module_state_names():
    assert model.module_state_name(2) == "Stopped"
    assert model.module_state_name(15) == "Resetting"
    assert model.module_state_name(6) == "Execute"
    assert model.module_state_name(None) == "unknown"
    assert model.module_state_name(99) == "unknown (99)"


def test_skill_state_names():
    assert model.skill_state_name(0) == "Idle"
    assert model.skill_state_name(3) == "Succeeded"
    assert model.skill_state_name(5) == "Aborted"
    assert model.skill_state_name("x") == "unknown"


def test_error_text_covers_every_code():
    for code in range(0, 9):
        assert model.ERROR_CODES[code] in model.error_text(code)
    assert model.error_text(8) == "8 OutOfRange: " + model.ERROR_TEXT[8]
    assert model.error_text(42) == "unknown error 42"
    assert model.error_text(None) == "unknown"


def test_module_commands_follow_the_specification_table():
    assert model.module_command_enabled("Reset", model.STOPPED, True)
    assert not model.module_command_enabled("Reset", model.IDLE, True)
    assert not model.module_command_enabled("Reset", model.STOPPED, False)

    assert model.module_command_enabled("Start", model.IDLE, True)
    assert not model.module_command_enabled("Start", model.STOPPED, True)

    for state in (model.RESETTING, model.IDLE, model.EXECUTE):
        assert model.module_command_enabled("Stop", state, True)
    for state in (model.STOPPED, model.STOPPING, model.ABORTED):
        assert not model.module_command_enabled("Stop", state, True)

    for state in (model.STOPPED, model.RESETTING, model.IDLE, model.EXECUTE, model.STOPPING):
        assert model.module_command_enabled("Abort", state, True)
    assert not model.module_command_enabled("Abort", model.ABORTED, True)

    assert model.module_command_enabled("Clear", model.ABORTED, True)
    assert not model.module_command_enabled("Clear", model.STOPPED, True)


def test_module_commands_need_known_state_and_command():
    assert not model.module_command_enabled("Reset", None, True)
    assert not model.module_command_enabled("Nonsense", model.STOPPED, True)


def test_skill_commands_follow_the_specification_table():
    for skill_state in (model.SK_IDLE, model.SK_SUCCEEDED, model.SK_FAILED):
        assert model.skill_command_enabled("Start", skill_state, model.EXECUTE, True)
    assert not model.skill_command_enabled("Start", model.SK_IDLE, model.IDLE, True)
    assert not model.skill_command_enabled("Start", model.SK_RUNNING, model.EXECUTE, True)
    assert not model.skill_command_enabled("Start", model.SK_STOPPING, model.EXECUTE, True)
    assert not model.skill_command_enabled("Start", model.SK_ABORTED, model.EXECUTE, True)
    assert not model.skill_command_enabled("Start", model.SK_IDLE, model.EXECUTE, True, False)
    assert not model.skill_command_enabled("Start", model.SK_IDLE, model.EXECUTE, False)

    assert model.skill_command_enabled("Stop", model.SK_RUNNING, model.EXECUTE, True)
    for skill_state in (model.SK_IDLE, model.SK_SUCCEEDED, model.SK_FAILED, model.SK_ABORTED):
        assert not model.skill_command_enabled("Stop", skill_state, model.EXECUTE, True)

    for skill_state in (model.SK_IDLE, model.SK_RUNNING, model.SK_STOPPING,
                        model.SK_SUCCEEDED, model.SK_FAILED):
        assert model.skill_command_enabled("Abort", skill_state, model.EXECUTE, True)
    assert not model.skill_command_enabled("Abort", model.SK_ABORTED, model.EXECUTE, True)

    assert model.skill_command_enabled("Reset", model.SK_ABORTED, model.EXECUTE, True)
    for skill_state in (model.SK_IDLE, model.SK_RUNNING, model.SK_SUCCEEDED, model.SK_FAILED):
        assert not model.skill_command_enabled("Reset", skill_state, model.EXECUTE, True)

    assert not model.skill_command_enabled("Nonsense", model.SK_IDLE, model.EXECUTE, True)


def test_every_button_needs_the_occupation():
    for command in model.MODULE_COMMANDS:
        assert not model.module_command_enabled(command, model.STOPPED, False)
    for command in model.SKILL_COMMANDS:
        assert not model.skill_command_enabled(command, model.SK_IDLE, model.EXECUTE, False)


def test_module_diagram_targets_are_known_states():
    for source, target, label in model.MODULE_DIAGRAM:
        assert source in model.MODULE_STATES, label
        assert target in model.MODULE_STATES, label


def test_module_diagram_covers_every_state():
    used = {code for edge in model.MODULE_DIAGRAM for code in edge[:2]}
    assert used == set(model.MODULE_STATES)


def test_module_diagram_has_a_reset_and_a_clear():
    assert (model.STOPPED, model.RESETTING, "Reset") in model.MODULE_DIAGRAM
    assert (model.ABORTED, model.CLEARING, "Clear") in model.MODULE_DIAGRAM
    assert (model.CLEARING, model.STOPPED, "SC") in model.MODULE_DIAGRAM


def test_skill_diagram_uses_known_states_and_styles():
    for source, target, label, style in model.SKILL_DIAGRAM:
        assert source in model.SKILL_STATES, label
        assert target in model.SKILL_STATES, label
        assert style in ("command", "sc"), label


def test_skill_diagram_covers_every_state_and_documented_transitions():
    used = {code for edge in model.SKILL_DIAGRAM for code in edge[:2]}
    assert used == set(model.SKILL_STATES)
    pairs = {(source, target) for source, target, _, _ in model.SKILL_DIAGRAM}
    for expected in (
        (model.SK_IDLE, model.SK_RUNNING),
        (model.SK_RUNNING, model.SK_SUCCEEDED),
        (model.SK_RUNNING, model.SK_FAILED),
        (model.SK_RUNNING, model.SK_STOPPING),
        (model.SK_STOPPING, model.SK_FAILED),
        (model.SK_ABORTED, model.SK_IDLE),
    ):
        assert expected in pairs


def _drawn_skill_edges() -> set[tuple[int, int]]:
    """(from, to) of the skill drawing, Abort out of the frame expanded to its states."""
    frame = [int(code) for code in re.search(
        r"const MINI_ABORTABLE = \[([\d,\s]+)\]", DIAGRAM_SOURCE).group(1).split(",")]
    block = DIAGRAM_SOURCE.split("const MINI_EDGES = [", 1)[1].split("];", 1)[0]
    pairs: set[tuple[int, int]] = set()
    for source, target in re.findall(r"\[\s*('frame'|\d+)\s*,\s*(\d+)\s*,", block):
        sources = frame if source == "'frame'" else [int(source)]
        pairs.update((code, int(target)) for code in sources)
    return pairs


def test_small_skill_drawing_has_every_state_of_the_controller():
    assert set(MINI_SKILL_POSITIONS) == set(model.SKILL_STATES)


def test_small_skill_drawing_is_the_controller_machine():
    """Every transition of SKILL_Control is drawn, and nothing else."""
    assert _drawn_skill_edges() == {(source, target) for source, target, _, _ in model.SKILL_DIAGRAM}


def test_only_reset_returns_to_idle():
    assert {source for source, target in _drawn_skill_edges() if target == model.SK_IDLE} == {
        model.SK_ABORTED
    }


def test_small_skill_drawing_has_no_overlapping_boxes():
    boxes = {
        code: (x, y, x + MINI_SKILL_WIDTH, y + MINI_SKILL_HEIGHT)
        for code, (x, y) in MINI_SKILL_POSITIONS.items()
    }
    codes = sorted(boxes)
    for index, first in enumerate(codes):
        for second in codes[index + 1:]:
            left, right = boxes[first], boxes[second]
            apart_x = right[0] >= left[2] or left[0] >= right[2]
            apart_y = right[1] >= left[3] or left[1] >= right[3]
            assert apart_x or apart_y, f"states {first} and {second} overlap"


def test_small_skill_drawing_leaves_room_between_running_and_idle():
    idle = MINI_SKILL_POSITIONS[model.SK_IDLE]
    running = MINI_SKILL_POSITIONS[model.SK_RUNNING]
    assert running[0] - (idle[0] + MINI_SKILL_WIDTH) >= 15, "the arrow is too short"


def test_skill_state_kinds_match_the_command_table():
    assert model.SKILL_STATE_KIND[model.SK_RUNNING] == "acts"
    assert model.SKILL_STATE_KIND[model.SK_STOPPING] == "acts"
    for code in (model.SK_IDLE, model.SK_SUCCEEDED, model.SK_FAILED, model.SK_ABORTED):
        assert model.SKILL_STATE_KIND[code] == "waits"


def test_module_state_kinds_are_known():
    for code in model.MODULE_STATES:
        assert model.MODULE_STATE_KIND[code] in ("acts", "waits")

def test_error_is_shown_only_in_the_states_it_describes():
    assert model.shown_error(model.SK_FAILED, model.ERR_TIMEOUT) == model.ERR_TIMEOUT
    assert model.shown_error(model.SK_ABORTED, model.ERR_INTERRUPTED) == model.ERR_INTERRUPTED
    for state in (model.SK_IDLE, model.SK_RUNNING, model.SK_STOPPING, model.SK_SUCCEEDED):
        assert model.shown_error(state, model.ERR_INTERRUPTED) == 0
    assert model.shown_error(None, 3) == 0


def test_start_needs_free_equipment():
    assert not model.skill_command_enabled(
        "Start", model.SK_IDLE, model.EXECUTE, True, equipment_free=False
    )
