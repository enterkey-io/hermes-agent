import json
from pathlib import Path

import yaml

from scripts.workforce_config_stage import stage
from scripts.workforce_cutover_bundle import bundle


def test_config_stage_and_bundle_are_complete_idempotent_and_non_mutating(tmp_path: Path):
    profiles = tmp_path / "profiles"
    agents = []
    for name, manager, reports in (("aurora", "elliott", ["worker"]), ("worker", "aurora", [])):
        profile = profiles / name
        profile.mkdir(parents=True)
        (profile / "AGENTS.md").write_text(f"# {name}\n")
        config = profile / "config.yaml"
        config.write_text("toolsets: [kanban]\nplugins:\n  enabled: [platforms/buzz]\n  disabled: [workforce-control]\n")
        agents.append({
            "agent": name, "display_name": name.title(), "status": "active", "operational": True,
            "department": None, "function": "Chief of Staff" if name == "aurora" else "Specialist",
            "manager": manager, "direct_reports": reports, "mission": "test", "owned_outcomes": ["test"],
            "authority": ["test"], "prohibited_actions": [], "escalation_target": manager,
            "cross_team_request_path": "test", "buzz_rooms": [], "profile_path": str(profile),
        })
    org = tmp_path / "organization.yaml"
    org.write_text(yaml.safe_dump({"schema_version": 1, "workforce_contract_version": "test", "reserved_approvals": [], "agents": [
        {"agent": "elliott", "display_name": "Elliott", "status": "artifact", "operational": False, "department": None, "function": "Owner", "manager": None, "direct_reports": ["aurora"], "mission": "test", "owned_outcomes": ["test"], "authority": ["test"], "prohibited_actions": [], "escalation_target": None, "cross_team_request_path": None, "buzz_rooms": [], "profile_path": None},
        *agents,
    ]}, sort_keys=False))
    before = {name: (profiles / name / "config.yaml").read_bytes() for name in ("aurora", "worker")}
    result = stage(org, tmp_path / "configs")
    assert len(result["profiles"]) == 2
    for row in result["profiles"]:
        candidate = yaml.safe_load(Path(row["candidate"]).read_text())
        assert candidate["toolsets"].count("workforce") == 1
        assert candidate["plugins"]["enabled"].count("workforce-control") == 1
        assert "workforce-control" not in candidate["plugins"]["disabled"]
    assert before == {name: (profiles / name / "config.yaml").read_bytes() for name in before}

    instruction_manifest = tmp_path / "instructions.json"
    instruction_manifest.write_text(json.dumps({"profiles": [
        {"agent": name, "status": "active", "source": str(profiles/name/"AGENTS.md"), "target": str(profiles/name/"AGENTS.md"), "source_sha256": "x", "candidate": str(tmp_path/name/"AGENTS.md"), "candidate_sha256": "y"}
        for name in ("aurora", "worker")
    ]}))
    config_manifest = tmp_path / "configs/manifest.json"
    combined = bundle(instruction_manifest, config_manifest, tmp_path / "bundle.json")
    assert combined["logical_profiles"] == 2
    assert combined["writes"] == 4
