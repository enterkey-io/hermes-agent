from __future__ import annotations

import json

from hermes_cli import runbook_store
from hermes_cli.runbook_projection import project_runbook
from hermes_cli import workflow_registry as workflow_registry
from tools import runbook_tools


def _metadata() -> dict:
    return {
        "id": "wf-test-runbook",
        "slug": "test-runbook",
        "title": "Test Runbook",
        "purpose": "Verify the agent tool contract.",
        "owner_profile": "grace",
        "status": "active",
        "runtime": {"kind": "hermes", "ref": "profile:grace"},
        "schedules": [],
        "steps": [{"step_key": "run", "name": "Run"}],
        "inputs": {},
        "outputs": {},
        "permitted_writes": [],
        "approval_rules": {},
        "retry": {"max_attempts": 1},
        "timeout": {},
        "deduplication": {"strategy": "manual"},
        "related": {},
    }


def _save() -> str:
    record = runbook_store.save_runbook(
        _metadata(),
        "# Test Runbook\n\n1. Run it.\n",
        approved_by="test",
    )
    project_runbook(record)
    return record.slug


def test_list_search_get_and_validate() -> None:
    slug = _save()

    listed = json.loads(runbook_tools._list({"owner_profile": "grace"}))
    assert listed["count"] == 1
    searched = json.loads(runbook_tools._list({"query": "agent tool"}))
    assert searched["runbooks"][0]["slug"] == slug
    fetched = json.loads(runbook_tools._get({"slug": slug}))
    assert fetched["metadata"]["id"] == "wf-test-runbook"
    assert "Run it" in fetched["body"]
    markdown = runbook_store.runbook_path(slug).read_text(encoding="utf-8")
    validated = json.loads(runbook_tools._validate({"markdown": markdown}))
    assert validated["valid"] is True


def test_proposal_does_not_activate_and_runs_are_readable(monkeypatch) -> None:
    slug = _save()
    path = runbook_store.runbook_path(slug)
    active = path.read_text(encoding="utf-8")
    candidate = active.replace("Run it", "Proposed only")
    monkeypatch.setenv("HERMES_HOME", str(path.parents[2] / "profiles" / "grace"))

    proposed = json.loads(
        runbook_tools._propose(
            {"slug": slug, "markdown": candidate, "summary": "Test proposal"}
        )
    )

    assert proposed["success"] is True
    assert path.read_text(encoding="utf-8") == active
    with workflow_registry.connect_closing() as conn:
        definition = workflow_registry.get_definition_by_slug(conn, slug)
        workflow_registry.start_run(conn, definition.id, trigger_kind="manual")
    runs = json.loads(runbook_tools._runs({"slug": slug}))
    assert runs["count"] == 1
    assert runs["runs"][0]["status"] == "running"
