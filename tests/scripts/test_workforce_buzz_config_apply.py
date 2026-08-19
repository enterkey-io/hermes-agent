import json
from pathlib import Path

import pytest

from scripts import workforce_buzz_config_apply as apply


def plan(tmp_path: Path) -> dict:
    result = {
        "valid": True,
        "mutation_performed": False,
        "profiles": [],
    }
    for name in ("aurora", "root"):
        commands = [
            ["hermes", "config", "set", "gateway.platforms.buzz.extra.channels", "uuid"],
            ["hermes", "config", "set", "gateway.platforms.buzz.extra.home_channel", "uuid"],
            ["hermes", "config", "set", "gateway.platforms.buzz.extra.require_mention", "true"],
        ]
        inverse = [
            ["hermes", "config", "unset", "gateway.platforms.buzz.extra.channels"],
            ["hermes", "config", "unset", "gateway.platforms.buzz.extra.home_channel"],
            ["hermes", "config", "unset", "gateway.platforms.buzz.extra.require_mention"],
        ]
        result["profiles"].append(
            {
                "profile": name,
                "environment": {"HERMES_HOME": str(tmp_path / name)},
                "commands_not_executed": commands,
                "inverse_commands_not_executed": inverse,
            }
        )
    return result


def test_load_rejects_unsafe_command(tmp_path: Path):
    value = plan(tmp_path)
    value["profiles"][0]["commands_not_executed"][0] = ["hermes", "update"]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="unsafe"):
        apply.load_plan(path)


def test_apply_and_rollback_use_profile_environment(tmp_path: Path):
    hermes = tmp_path / "hermes"
    hermes.write_text("binary")
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs["env"]["HERMES_HOME"]))

    value = plan(tmp_path)
    report = apply.execute_plan(value, hermes_bin=hermes, rollback=False, runner=runner)
    assert report["command_count"] == 6
    assert calls[0][1].endswith("/aurora")
    calls.clear()
    report = apply.execute_plan(value, hermes_bin=hermes, rollback=True, runner=runner)
    assert report["rolled_back"] is True
    assert calls[0][1].endswith("/root")


def test_apply_compensates_completed_profiles(tmp_path: Path):
    hermes = tmp_path / "hermes"
    hermes.write_text("binary")
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 4:
            raise RuntimeError("failure")

    with pytest.raises(RuntimeError):
        apply.execute_plan(plan(tmp_path), hermes_bin=hermes, rollback=False, runner=runner)
    assert calls[-3][2] == "unset"
    assert calls[-1][2] == "unset"
