import json
from pathlib import Path

import pytest
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
        assert candidate["kanban"]["auto_decompose"] is False
        assert candidate["kanban"]["dispatch_in_gateway"] is False
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


def test_config_stage_routes_auxiliary_models_and_assigns_one_dispatch_owner(tmp_path: Path):
    profiles = tmp_path / "profiles"
    agents = []
    for agent_id, profile_name in (("aurora", "aurora"), ("root", "main")):
        profile = profiles / profile_name
        profile.mkdir(parents=True)
        (profile / "AGENTS.md").write_text(f"# {agent_id}\n")
        (profile / "config.yaml").write_text(
            "model: {provider: old, default: old}\n"
            "agent: {reasoning_effort: low}\n"
            "auxiliary:\n  compression: {timeout: 77}\n"
        )
        agents.append({
            "agent": agent_id, "display_name": agent_id.title(), "status": "active",
            "operational": True, "department": None, "function": "Director",
            "manager": "elliott" if agent_id == "aurora" else "aurora",
            "direct_reports": ["root"] if agent_id == "aurora" else [],
            "mission": "test", "owned_outcomes": ["test"],
            "authority": ["test"], "prohibited_actions": [],
            "escalation_target": "elliott" if agent_id == "aurora" else "aurora",
            "cross_team_request_path": "test", "buzz_rooms": [],
            "profile_path": str(profile),
        })
    org = tmp_path / "organization.yaml"
    org.write_text(yaml.safe_dump({
        "schema_version": 1, "workforce_contract_version": "test",
        "reserved_approvals": [], "agents": [
            {"agent": "elliott", "display_name": "Elliott", "status": "artifact",
             "operational": False, "department": None, "function": "Owner",
             "manager": None, "direct_reports": ["aurora"], "mission": "test",
             "owned_outcomes": ["test"], "authority": ["test"],
             "prohibited_actions": [], "escalation_target": None,
             "cross_team_request_path": None, "buzz_rooms": [], "profile_path": None},
            *agents,
        ]
    }, sort_keys=False))
    models = tmp_path / "models.yaml"
    models.write_text(yaml.safe_dump({
        "presets": {"p": {"provider": "openai-codex", "model": "primary", "reasoning_effort": "medium"}},
        "assignments": {
            "aurora": {"profile": "aurora", "preset": "p"},
            "root": {"profile": "main", "preset": "p"},
        },
        "auxiliary_policy": {
            "general_low_cost": {"tasks": ["background_review", "compression"], "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
            "vision": {"tasks": ["vision"], "provider": "openai-codex", "model": "gpt-5.6-terra", "reasoning_effort": "high"},
        },
    }, sort_keys=False))

    result = stage(org, tmp_path / "out", models)
    by_agent = {row["agent"]: yaml.safe_load(Path(row["candidate"]).read_text()) for row in result["profiles"]}
    assert by_agent["aurora"]["kanban"]["dispatch_in_gateway"] is False
    assert by_agent["root"]["kanban"]["dispatch_in_gateway"] is True
    assert by_agent["aurora"]["auxiliary"]["background_review"]["enabled"] is True
    assert by_agent["aurora"]["auxiliary"]["compression"] == {
        "timeout": 77, "provider": "openai-codex", "model": "gpt-5.6-luna",
        "reasoning_effort": "xhigh",
    }
    assert by_agent["root"]["auxiliary"]["vision"]["model"] == "gpt-5.6-terra"


def test_config_stage_removes_only_a_proven_env_migrated_buzz_key(tmp_path: Path):
    profile = tmp_path / "profiles" / "worker"
    profile.mkdir(parents=True)
    secret = "a" * 64
    (profile / "AGENTS.md").write_text("# worker\n")
    (profile / "config.yaml").write_text(
        f"BUZZ_PRIVATE_KEY: {secret}\ntoolsets: []\nplugins: {{enabled: []}}\n"
    )
    (profile / ".env").write_text(f"BUZZ_PRIVATE_KEY={secret}\n")
    org = tmp_path / "organization.yaml"
    org.write_text(yaml.safe_dump({
        "schema_version": 1, "workforce_contract_version": "test",
        "reserved_approvals": [], "agents": [
            {"agent": "elliott", "display_name": "Elliott", "status": "artifact",
             "operational": False, "department": None, "function": "Owner",
             "manager": None, "direct_reports": ["worker"], "mission": "test",
             "owned_outcomes": ["test"], "authority": ["test"],
             "prohibited_actions": [], "escalation_target": None,
             "cross_team_request_path": None, "buzz_rooms": [], "profile_path": None},
            {"agent": "worker", "display_name": "Worker", "status": "active",
             "operational": True, "department": None, "function": "Specialist",
             "manager": "elliott", "direct_reports": [], "mission": "test",
             "owned_outcomes": ["test"], "authority": ["test"],
             "prohibited_actions": [], "escalation_target": "elliott",
             "cross_team_request_path": "test", "buzz_rooms": [],
             "profile_path": str(profile)},
        ]
    }, sort_keys=False))

    result = stage(org, tmp_path / "out")
    candidate = yaml.safe_load(Path(result["profiles"][0]["candidate"]).read_text())
    assert "BUZZ_PRIVATE_KEY" not in candidate
    assert "credential-location:BUZZ_PRIVATE_KEY:config-to-env" in result["profiles"][0]["managed_change"]

    (profile / ".env").write_text("BUZZ_PRIVATE_KEY=" + "b" * 64 + "\n")
    with pytest.raises(RuntimeError, match="migrate the exact legacy"):
        stage(org, tmp_path / "mismatch")
