from pathlib import Path

import yaml

from scripts.workforce_buzz_config_generate import generate


ROOT = Path(__file__).parents[2]


def test_config_plan_is_complete_exact_and_non_mutating(tmp_path: Path):
    topology = yaml.safe_load((ROOT / "workforce/buzz-topology.yaml").read_text())
    room_map = {
        room["name"]: f"00000000-0000-4000-8000-{index:012d}"
        for index, room in enumerate(topology["rooms"], 1)
    }
    room_map_path = tmp_path / "rooms.yaml"
    room_map_path.write_text(yaml.safe_dump(room_map))
    profiles = tmp_path / "profiles"
    for name in ("aurora", "main"):
        (profiles / name).mkdir(parents=True)
        (profiles / name / "config.yaml").write_text(
            "gateway:\n  platforms:\n    buzz:\n      extra:\n        channels: old\n        home_channel: old\n        require_mention: false\n"
        )

    report = generate(
        organization_path=ROOT / "workforce/organization.yaml",
        topology_path=ROOT / "workforce/buzz-topology.yaml",
        room_map_path=room_map_path,
        profiles_root=profiles,
    )

    assert report["valid"] is True
    assert report["mutation_performed"] is False
    assert len(report["profiles"]) == 21
    by_agent = {item["agent"]: item for item in report["profiles"]}
    assert by_agent["root"]["profile"] == "main"
    assert by_agent["root"]["home_room"] == "director-operations"
    assert by_agent["chloe"]["home_room"] == "Admin"
    assert all(len(item["commands_not_executed"]) == 3 for item in report["profiles"])
    assert (profiles / "aurora/config.yaml").read_text().find("channels: old") >= 0
