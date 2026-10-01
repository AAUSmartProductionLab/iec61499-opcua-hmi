from hmi import model
from hmi import profiles as prof


def test_every_documented_path_of_filling_exists():
    expected = {
        "Occupation/Occupied",
        "Module/State",
        "Equipment/NeedleAxis/AtTop",
        "Equipment/NeedleAxis/AtBottom",
        "Equipment/Scale/Weight",
        "Skills/Dispensing/Execute/MoveNeedleDown/State",
        "Skills/Dispensing/Execute/Dwell/State",
        "Skills/Dispensing/Execute/Dwell/Parameters/Duration",
        "Skills/Dispensing/Execute/MoveNeedleUp/ErrorID",
        "Skills/Dispensing/Execute/Weigh/Results/Weight",
        "Skills/Dispensing/Stopping/MoveNeedleUp/State",
        "Skills/Dispensing/Results/Weight",
        "Skills/Weigh/Results/Weight",
        "Procedures/Resetting/MoveNeedleUp/State",
        "Procedures/Stopping/MoveNeedleUp/State",
    }
    paths = set(prof.monitored_paths(prof.FILLING))
    assert expected <= paths


def test_every_documented_path_of_stoppering_exists():
    expected = {
        "Equipment/Piston/AtLimit",
        "Module/State",
        "Skills/RaisePiston/Parameters/Duration",
        "Skills/MoveArm/Parameters/Angle",
        "Skills/MoveArm/Parameters/Settle",
        "Skills/Stoppering/Execute/ArmIn/Parameters/Angle",
        "Skills/Stoppering/Execute/ArmOut/Parameters/Angle",
        "Skills/Stoppering/Execute/ExtendPlunger/Parameters/Duration",
        "Skills/Stoppering/Execute/RetractPlunger/Parameters/Duration",
        "Skills/Stoppering/Execute/RaisePiston/Parameters/Duration",
        "Procedures/Resetting/ArmMiddle/Parameters/Angle",
        "Procedures/Resetting/ArmHome/Parameters/Angle",
        "Procedures/Resetting/RetractPlunger/Parameters/Duration",
        "Procedures/Resetting/RaisePiston/Parameters/Duration",
    }
    paths = set(prof.monitored_paths(prof.STOPPERING))
    assert expected <= paths


def test_stoppering_has_no_stopping_procedure():
    names = {procedure.name for procedure in prof.STOPPERING.procedures}
    assert names == {"Resetting"}
    assert not any(skill.stop_steps for skill in prof.STOPPERING.skills)


def test_parameters_match_the_documented_ranges_and_defaults():
    skill = next(item for item in prof.STOPPERING.skills if item.name == "RaisePiston")
    assert skill.params[0].name == "Duration"
    assert (skill.params[0].minimum, skill.params[0].maximum) == (0.0, 10.0)
    assert skill.params[0].default == 2.0

    skill = next(item for item in prof.STOPPERING.skills if item.name == "MoveArm")
    assert [param.name for param in skill.params] == ["Angle", "Settle"]
    assert [(p.minimum, p.maximum, p.default) for p in skill.params] == [
        (0.0, 180.0, 120.0),
        (0.0, 5.0, 2.0),
    ]

    skill = next(item for item in prof.STOPPERING.skills if item.name == "ExtendPlunger")
    assert (skill.params[0].minimum, skill.params[0].maximum, skill.params[0].default) == (
        0.0, 20.0, 10.0
    )


def test_step_angles_of_the_stoppering_cycle():
    skill = next(item for item in prof.STOPPERING.skills if item.name == "Stoppering")
    assert [step.name for step in skill.steps] == [
        "LowerPiston", "ArmIn", "ArmOut", "ExtendPlunger", "RetractPlunger", "RaisePiston",
    ]
    angles = {
        step.name: next(p.default for p in step.params if p.name == "Angle")
        for step in skill.steps
        if any(p.name == "Angle" for p in step.params)
    }
    assert angles == {"ArmIn": 1.0, "ArmOut": 121.0}


def test_dispensing_steps_and_stop_sequence():
    skill = next(item for item in prof.FILLING.skills if item.name == "Dispensing")
    assert [step.name for step in skill.steps] == [
        "MoveNeedleDown", "Dwell", "MoveNeedleUp", "Weigh",
    ]
    assert [step.name for step in skill.stop_steps] == ["MoveNeedleUp"]
    assert skill.module_level


def test_method_paths_cover_every_command():
    paths = set(prof.method_paths(prof.FILLING))
    assert {"Occupation/Occupy", "Occupation/Release"} <= paths
    for command in model.MODULE_COMMANDS:
        assert f"Module/{command}" in paths
    for skill in prof.FILLING.skills:
        for command in model.SKILL_COMMANDS:
            assert f"Skills/{skill.name}/{command}" in paths


def test_monitored_paths_are_unique_and_namespaced():
    for profile in prof.PROFILES.values():
        paths = prof.monitored_paths(profile)
        assert len(paths) == len(set(paths))
        for path in paths:
            assert not path.startswith("/")
            assert path == path.strip()
            assert all(part for part in path.split("/"))
            browse = prof.browse_path(path)
            assert all(part.startswith(f"{prof.NAMESPACE_INDEX}:") for part in browse)


def test_root_browse_path_follows_the_specification():
    assert prof.root_browse_path(prof.FILLING) == ["0:Objects", "1:Filling"]
    assert prof.root_browse_path(prof.STOPPERING) == ["0:Objects", "1:Stoppering"]


def test_profile_payload_is_json_ready():
    for profile in prof.PROFILES.values():
        payload = profile.to_dict()
        assert payload["root"] == payload["title"]
        assert payload["namespaceIndex"] == 1
        assert payload["skills"]
        for skill in payload["skills"]:
            for param in skill["params"] + skill["results"]:
                assert {"name", "unit", "minimum", "maximum", "default"} <= set(param)


def test_unknown_module_key():
    try:
        prof.get_profile("nope")
    except KeyError as error:
        assert "filling" in str(error)
    else:
        raise AssertionError("expected a KeyError")