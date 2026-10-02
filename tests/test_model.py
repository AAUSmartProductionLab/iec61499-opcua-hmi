"""from hmi import model"""

import re
from pathlib import Path

from hmi import model

DIAGRAM_SOURCE = (Path(__file__).resolve().parents[1] / "hmi" / "static" / "diagram.js").read_text(
    encoding="utf-8"
)
FRAME_MEMBER = re.compile(r"\{ name: '(\w+)'.*?members: \[([\d,\s]+)\]")
EDGE = re.compile(r"\[\s*('\w+'|\d+),\s*(\d+),\s*'([^']*)',\s*'([^']*)',\s*(\d+),\s*(\d+)\]")


def drawing(name: str) -> dict:
    """Positions, box size, frames and edges of a machine drawn in diagram.js."""
    block = DIAGRAM_SOURCE.split(f"const {name} = {{", 1)[1].split("\n};", 1)[0]
    width, height = (int(v) for v in re.search(r"box: \{ w: (\d+), h: (\d+) \}", block).groups())
    positions = {
        int(code): (int(x), int(y))
        for code, x, y in re.findall(r"^\s+(\d+): \[(\d+), (\d+)\]", block, re.M)
    }
    frames = {
        frame: [int(code) for code in members.split(",")]
        for frame, members in FRAME_MEMBER.findall(block)
    }
    edges = [
        (src.strip("'") if src.startswith("'") else int(src), int(dst), path, label, int(x), int(y))
        for src, dst, path, label, x, y in EDGE.findall(block)
    ]
    return {"w": width, "h": height, "positions": positions, "frames": frames, "edges": edges}


def drawn_pairs(machine: dict) -> set[tuple[int, int]]:
    pairs: set[tuple[int, int]] = set()
    for src, dst, *_ in machine["edges"]:
        sources = machine["frames"][src] if isinstance(src, str) else [src]
        pairs.update((code, dst) for code in sources)
    return pairs


def segments(path: str) -> list[tuple[float, float, float, float]]:
    """Straight segments of an M/H/V path."""
    out, x, y = [], 0.0, 0.0
    for cmd, value in re.findall(r"([MHV])\s*([\d.,]+)", path):
        if cmd == "M":
            x, y = (float(v) for v in value.split(","))
        elif cmd == "H":
            out.append((x, y, float(value), y))
            x = float(value)
        else:
            out.append((x, y, x, float(value)))
            y = float(value)
    return out


def hits(segment, box) -> bool:
    """Whether a segment runs through the inside of a box (touching its border is fine)."""
    x1, y1, x2, y2 = segment
    left, top, right, bottom = box
    lo_x, hi_x, lo_y, hi_y = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
    return lo_x < right - 1 and hi_x > left + 1 and lo_y < bottom - 1 and hi_y > top + 1


def boxes(machine: dict) -> dict[int, tuple[int, int, int, int]]:
    return {
        code: (x, y, x + machine["w"], y + machine["h"])
        for code, (x, y) in machine["positions"].items()
    }


def bubble(label: str, x: int, y: int) -> tuple[float, float, float, float]:
    width = max(40, len(label) * 6.8 + 14)
    return (x - width / 2, y - 10, x + width / 2, y + 10)


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


def test_drawings_have_every_state_of_the_controller():
    assert set(drawing("MODULE")["positions"]) == set(model.MODULE_STATES)
    assert set(drawing("SKILL")["positions"]) == set(model.SKILL_STATES)


def test_drawings_are_the_controller_machines():
    """Every transition of MOD_StateLogic and SKILL_Control is drawn, and nothing else."""
    assert drawn_pairs(drawing("MODULE")) == {(a, b) for a, b, _ in model.MODULE_DIAGRAM}
    assert drawn_pairs(drawing("SKILL")) == {(a, b) for a, b, _, _ in model.SKILL_DIAGRAM}


def test_only_reset_returns_a_skill_to_idle():
    assert {a for a, b in drawn_pairs(drawing("SKILL")) if b == model.SK_IDLE} == {model.SK_ABORTED}


def test_every_command_is_written_on_its_line():
    labels = {e[3] for e in drawing("SKILL")["edges"] if e[3]}
    assert labels == {"Start", "Stop", "Abort", "Reset"}
    labels = {e[3] for e in drawing("MODULE")["edges"] if e[3]}
    assert labels == set(model.MODULE_COMMANDS)


def test_drawings_are_tidy():
    """No box on a box, no line through a box or across another line, no label on a foreign line."""
    for name in ("MODULE", "SKILL"):
        machine = drawing(name)
        states = boxes(machine)
        codes = sorted(states)
        for i, a in enumerate(codes):
            for b in codes[i + 1:]:
                left, right = states[a], states[b]
                assert right[0] >= left[2] or left[0] >= right[2] or right[1] >= left[3] \
                    or left[1] >= right[3], f"{name}: states {a} and {b} overlap"
        labels = [(i, bubble(label, x, y)) for i, (_, _, _, label, x, y) in enumerate(machine["edges"]) if label]
        for i, (src, dst, path, label, x, y) in enumerate(machine["edges"]):
            for segment in segments(path):
                for code, box in states.items():
                    assert not hits(segment, box), f"{name}: {path} runs through state {code}"
                for j, box in labels:
                    if j != i:
                        assert not hits(segment, box), f"{name}: {path} runs through a label"
            for j, (_, _, other, _, _, _) in enumerate(machine["edges"]):
                if j <= i:
                    continue
                for s1 in segments(path):
                    for s2 in segments(other):
                        horizontal, vertical = (s1, s2) if s1[1] == s1[3] else (s2, s1)
                        if horizontal[1] != horizontal[3] or vertical[0] != vertical[2]:
                            continue
                        crosses = (min(horizontal[0], horizontal[2]) < vertical[0] < max(horizontal[0], horizontal[2])
                                   and min(vertical[1], vertical[3]) < horizontal[1] < max(vertical[1], vertical[3]))
                        assert not crosses, f"{name}: {path} crosses {other}"
        for a, (_, box_a) in enumerate(labels):
            for _, box_b in labels[a + 1:]:
                assert not hits(box_a, box_b), f"{name}: labels overlap"
            for code, box in states.items():
                assert not hits(box_a, box), f"{name}: label on state {code}"


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
