import json
from pathlib import Path

import yaml

from hermes_cli.runbook_schema import render_frontmatter, split_frontmatter
from scripts.workforce_registry_link_stage import stage


def test_stages_explicit_runbook_link_without_mutating_job_or_runbook(tmp_path: Path):
    profiles = tmp_path / "profiles"
    profile = profiles / "xenia"
    (profile / "cron").mkdir(parents=True)
    (profile / "AGENTS.md").write_text("# Xenia\n")
    (profile / "config.yaml").write_text("{}\n")
    jobs = profile / "cron/jobs.json"
    jobs.write_text(json.dumps({"jobs": [{
        "id": "late", "name": "Late reconciliation", "enabled": True,
        "prompt": "Resolve canonical runbook `xenia-vt` before work.",
        "schedule": {"expr": "55 14 * * 1-5"},
    }]}))
    org = tmp_path / "org.yaml"
    common = {"mission": "test", "owned_outcomes": ["test"], "authority": ["test"], "prohibited_actions": [], "buzz_rooms": []}
    org.write_text(yaml.safe_dump({"schema_version": 1, "workforce_contract_version": "test", "reserved_approvals": [], "agents": [
        {"agent": "elliott", "display_name": "Elliott", "status": "artifact", "operational": False, "department": None, "function": "Owner", "manager": None, "direct_reports": ["xenia"], "escalation_target": None, "cross_team_request_path": None, "profile_path": None, **common},
        {"agent": "xenia", "display_name": "Xenia", "status": "active", "operational": True, "department": "Trading", "function": "Director", "manager": "elliott", "direct_reports": [], "escalation_target": "elliott", "cross_team_request_path": "test", "profile_path": str(profile), **common},
    ]}, sort_keys=False))
    runbooks = tmp_path / "runbooks"
    source = runbooks / "xenia-vt/RUNBOOK.md"
    source.parent.mkdir(parents=True)
    source.write_text(render_frontmatter({
        "id": "wf-xenia", "slug": "xenia-vt", "title": "VT", "purpose": "test",
        "owner_profile": "xenia", "status": "active", "runtime": {"kind": "hermes"},
        "schedules": [], "steps": [{"step_key": "main", "name": "Main"}],
        "inputs": {}, "outputs": {}, "permitted_writes": [], "approval_rules": {},
        "retry": {}, "timeout": {}, "deduplication": {}, "related": {},
    }, "# VT\n"))
    before_job, before_runbook = jobs.read_bytes(), source.read_bytes()
    report = stage(profiles, org, runbooks, tmp_path / "output")
    assert report["valid"] is True and report["linked_candidates"] == 1
    assert jobs.read_bytes() == before_job and source.read_bytes() == before_runbook
    candidate = split_frontmatter(Path(report["changes"][0]["candidate_runbook"]).read_text())
    assert any(step["step_key"] == "job_late" for step in candidate.metadata["steps"])
    assert report["changes"][0]["job_patch_not_executed"]["workflow_id"] == "wf-xenia"
