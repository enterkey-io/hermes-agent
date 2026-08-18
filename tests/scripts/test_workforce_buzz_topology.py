import json
from pathlib import Path

import yaml

from scripts.workforce_buzz_topology import compare, load_topology


ROOT = Path(__file__).parents[2]


def test_topology_preserves_confirmed_admin_and_excludes_friends():
    topology = load_topology(
        ROOT / "workforce" / "buzz-topology.yaml",
        ROOT / "workforce" / "organization.yaml",
    )
    admin = next(room for room in topology["rooms"] if room["name"] == "Admin")
    assert set(admin["members"]) == {"elliott", "aurora", "chloe", "grace", "milena"}
    assert all("amy" not in room["members"] for room in topology["rooms"])
    assert all("kourtnie" not in room["members"] for room in topology["rooms"])
    assert all(
        {"aurora", "chloe"} <= set(room["members"])
        for room in topology["rooms"]
        if room["name"].startswith("director-")
    )


def test_compare_is_read_only_and_reports_membership_drift():
    topology = {
        "rooms": [
            {
                "name": "Admin",
                "status": "confirmed",
                "members": ["elliott", "aurora", "chloe", "grace", "milena"],
            }
        ]
    }
    report = compare(
        topology,
        [{"name": "Admin", "members": ["elliott", "aurora", "grace", "stranger"]}],
    )
    assert report["mutation_performed"] is False
    assert report["rooms"][0]["missing_members"] == ["chloe", "milena"]
    assert report["rooms"][0]["unexpected_members"] == ["stranger"]


def test_topology_rejects_friend_members(tmp_path):
    data = yaml.safe_load((ROOT / "workforce" / "buzz-topology.yaml").read_text())
    data["rooms"][0]["members"].append("amy")
    path = tmp_path / "topology.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    try:
        load_topology(path, ROOT / "workforce" / "organization.yaml")
    except ValueError as exc:
        assert "non-operational" in str(exc) or "friend" in str(exc)
    else:
        raise AssertionError("friend membership should fail")
