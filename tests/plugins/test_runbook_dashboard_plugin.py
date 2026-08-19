"""Tests for the runbooks dashboard plugin backend."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "runbooks" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_runbooks_test",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/runbooks")
    return TestClient(app)


def _runbook_markdown(slug: str = "daily-brief") -> str:
    return f"""---
id: wf_daily_brief
slug: {slug}
title: Daily Brief
purpose: Prepare a concise daily operating brief.
owner_profile: alina
status: active
runtime:
  kind: hermes
  ref: gateway
schedules:
  - id: cron_daily_brief
    profile: alina
    cron_job_id: daily-brief
    enabled: true
steps:
  - step_key: collect
    name: Collect context
    executor_profile: alina
  - step_key: publish
    name: Publish brief
    executor_profile: alina
inputs: {{}}
outputs: {{}}
permitted_writes: []
approval_rules: {{}}
retry:
  max_attempts: 2
timeout:
  seconds: 1800
deduplication:
  strategy: date
related: {{}}
---
# Daily Brief

## Procedure

1. Collect context.
2. Publish the brief.
"""


def test_save_runbook_projects_workflow_and_steps(client):
    response = client.put(
        "/api/plugins/runbooks/runbooks/daily-brief",
        json={"markdown": _runbook_markdown(), "approved_by": "dashboard"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["runbook"]["slug"] == "daily-brief"
    workflow = payload["workflow"]
    assert workflow["id"] == "wf_daily_brief"
    assert workflow["owner_profile"] == "alina"
    assert workflow["status"] == "active"
    assert [step["step_key"] for step in workflow["steps"]] == ["collect", "publish"]

    overview = client.get("/api/plugins/runbooks/overview").json()
    assert overview["counts"]["runbooks"] == 1
    assert overview["counts"]["workflows"] == 1
    assert overview["workflows"][0]["steps"][0]["step_key"] == "collect"
    assert overview["workflows"][0]["steps"][0]["name"] == "Collect context"

    workflows = client.get("/api/plugins/runbooks/workflows").json()["workflows"]
    assert workflows[0]["steps"][0]["step_key"] == "collect"
    assert overview["counts"]["active_workflows"] == 1


def test_overview_lists_registered_and_unregistered_profile_schedules(client, tmp_path):
    profiles = tmp_path / ".hermes" / "profiles"
    grace_jobs = profiles / "grace" / "cron" / "jobs.json"
    grace_jobs.parent.mkdir(parents=True)
    grace_jobs.write_text(
        """{"jobs": [
          {"id": "morning", "name": "Morning", "enabled": true,
           "schedule": {"kind": "cron", "expr": "15 6 * * *"}},
          {"id": "heartbeat", "name": "Heartbeat", "enabled": true,
           "schedule_display": "7 8-19 * * 1-5", "workflow_id": "wf_heartbeat",
           "workflow_slug": "grace-heartbeat"},
          {"id": "old", "name": "Old", "enabled": false, "schedule": "@daily"}
        ]}""",
        encoding="utf-8",
    )

    overview = client.get("/api/plugins/runbooks/overview").json()

    assert overview["counts"]["schedules"] == 3
    assert overview["counts"]["enabled_schedules"] == 2
    assert overview["counts"]["registered_schedules"] == 1
    assert overview["counts"]["unregistered_schedules"] == 1
    assert overview["schedules"][0]["job_id"] == "morning"
    assert overview["schedules"][0]["registration_status"] == "unregistered"
    enabled = client.get("/api/plugins/runbooks/schedules").json()["schedules"]
    assert len(enabled) == 2
    all_schedules = client.get(
        "/api/plugins/runbooks/schedules?include_disabled=true"
    ).json()["schedules"]
    assert len(all_schedules) == 3


def test_proposal_does_not_replace_active_runbook(client):
    active = _runbook_markdown()
    assert client.put(
        "/api/plugins/runbooks/runbooks/daily-brief",
        json={"markdown": active, "approved_by": "dashboard"},
    ).status_code == 200

    candidate = active.replace("Daily Brief", "Daily Brief Candidate", 1)
    response = client.post(
        "/api/plugins/runbooks/runbooks/daily-brief/proposals",
        json={
            "markdown": candidate,
            "proposed_by": "alina",
            "summary": "Rename only",
        },
    )

    assert response.status_code == 200, response.text
    current = client.get("/api/plugins/runbooks/runbooks/daily-brief").json()
    assert current["metadata"]["title"] == "Daily Brief"
    assert current["proposals"][0]["summary"] == "Rename only"


def test_new_runbook_proposal_is_visible_and_can_be_activated(client):
    markdown = _runbook_markdown("proposed-daily-brief").replace(
        "status: active", "status: draft"
    )
    response = client.post(
        "/api/plugins/runbooks/runbooks/proposed-daily-brief/proposals",
        json={
            "markdown": markdown,
            "proposed_by": "xenia",
            "summary": "New workflow proposal",
        },
    )
    assert response.status_code == 200, response.text

    overview = client.get("/api/plugins/runbooks/overview").json()
    assert overview["counts"]["runbooks"] == 1
    assert overview["counts"]["workflows"] == 0
    candidate = overview["runbooks"][0]
    assert candidate["slug"] == "proposed-daily-brief"
    assert candidate["canonical"] is False
    assert candidate["pending_proposal_count"] == 1

    fetched = client.get(
        "/api/plugins/runbooks/runbooks/proposed-daily-brief"
    ).json()
    assert fetched["canonical"] is False
    assert fetched["record"]["owner_profile"] == "alina"
    assert fetched["proposals"][0]["summary"] == "New workflow proposal"
    assert fetched["markdown"] == markdown

    activated = client.put(
        "/api/plugins/runbooks/runbooks/proposed-daily-brief",
        json={"markdown": markdown},
    )
    assert activated.status_code == 200, activated.text
    current = client.get(
        "/api/plugins/runbooks/runbooks/proposed-daily-brief"
    ).json()
    assert current["canonical"] is True
    assert current["record"]["pending_proposal_count"] == 1


def test_run_and_step_state_transitions(client):
    assert client.put(
        "/api/plugins/runbooks/runbooks/daily-brief",
        json={"markdown": _runbook_markdown(), "approved_by": "dashboard"},
    ).status_code == 200

    run = client.post(
        "/api/plugins/runbooks/runs",
        json={"workflow_slug": "daily-brief", "trigger_kind": "manual"},
    ).json()["run"]
    assert run["status"] == "running"

    step = client.post(
        f"/api/plugins/runbooks/runs/{run['id']}/steps",
        json={"step_key": "collect"},
    ).json()["step_run"]
    assert step["status"] == "running"

    finished = client.post(
        f"/api/plugins/runbooks/step-runs/{step['id']}/finish",
        json={"status": "succeeded", "summary": "Collected"},
    ).json()["step_run"]
    assert finished["status"] == "succeeded"

    completed = client.post(
        f"/api/plugins/runbooks/runs/{run['id']}/complete",
        json={"status": "succeeded", "summary": "Done"},
    ).json()["run"]
    assert completed["status"] == "succeeded"

    runs = client.get("/api/plugins/runbooks/runs").json()["runs"]
    assert runs[0]["steps"][0]["summary"] == "Collected"


def test_preview_diff_and_bundle_registration(client):
    markdown = _runbook_markdown()
    preview = client.post(
        "/api/plugins/runbooks/runbooks/preview",
        json={"markdown": markdown},
    )
    assert preview.status_code == 200
    assert "Daily Brief" in preview.json()["html"]

    diff = client.post(
        "/api/plugins/runbooks/runbooks/daily-brief/diff",
        json={"markdown": markdown},
    )
    assert diff.status_code == 200
    assert "candidate" in diff.json()["diff"]

    bundle = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "runbooks"
        / "dashboard"
        / "dist"
        / "index.js"
    ).read_text(encoding="utf-8")
    assert 'window.__HERMES_PLUGINS__.register("runbooks", RunbooksApp);' in bundle
    assert "SDK.registerPlugin" not in bundle
    assert "Save definition" in bundle
    assert "Approve Save" not in bundle
    assert "Start Run" not in bundle
    assert '"Approver"' not in bundle
    assert '}, "Evernote")' not in bundle
    assert '}, "Workflows")' in bundle
    assert '}, "Timeline")' in bundle
    assert '}, "Runs")' in bundle
    assert '}, "Archive")' in bundle
    assert "Edit in Cron" in bundle
    assert 'window.location.assign("/cron?"' in bundle
    assert "schedulesFor" in bundle
    assert "runsFor" in bundle
    assert "DialogContent" in bundle
    assert "CardContent" in bundle
    assert "TabsList" in bundle
    assert "usefulPurpose" in bundle
    assert "departmentFilter" in bundle
    assert "ownerFilter" in bundle
    assert "controlWorkflow" in bundle
    assert '}, "Outcomes")' in bundle
    assert "renderOutcomes" in bundle
    assert "workforce_control" in bundle
    assert '}, "Pause")' in bundle
    assert '}, "Resume")' in bundle
    assert "item.canonical === false" in bundle
    assert "hermes-runbooks-workflow-detail" not in bundle

    styles = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "runbooks"
        / "dashboard"
        / "dist"
        / "style.css"
    ).read_text(encoding="utf-8")
    assert ".hermes-runbooks-workflow-card" in styles
    assert ".hermes-runbooks-dialog" in styles
    assert ".hermes-runbooks-detail-section" in styles
    assert ".hermes-runbooks-workflow-detail" not in styles
    assert ".hermes-runbooks-relationship-grid" not in styles
    assert "max-height: 50vh" not in styles
    assert "max-height: 34vh" not in styles


def test_workflow_control_requires_confirmation_and_controls_linked_schedule(
    client, tmp_path
):
    from cron import jobs as cron_jobs

    profile_home = tmp_path / ".hermes" / "profiles" / "alina"
    profile_home.mkdir(parents=True)
    with cron_jobs.use_cron_store(profile_home):
        job = cron_jobs.create_job(
            prompt="Prepare the brief",
            schedule="every 1h",
            name="Daily brief",
            deliver="local",
            provider="openrouter",
            model="openai/gpt-4o-mini",
        )

    markdown = _runbook_markdown().replace(
        "cron_job_id: daily-brief", f"cron_job_id: {job['id']}"
    )
    created = client.put(
        "/api/plugins/runbooks/runbooks/daily-brief",
        json={"markdown": markdown},
    )
    assert created.status_code == 200, created.text
    workflow = created.json()["workflow"]

    denied = client.post(
        f"/api/plugins/runbooks/workflows/{workflow['id']}/control",
        json={
            "action": "pause",
            "expected_version": workflow["version"],
            "confirmed": False,
        },
    )
    assert denied.status_code == 400

    paused = client.post(
        f"/api/plugins/runbooks/workflows/{workflow['id']}/control",
        json={
            "action": "pause",
            "expected_version": workflow["version"],
            "confirmed": True,
            "approver": "spoofed",
        },
    )
    assert paused.status_code == 200, paused.text
    payload = paused.json()
    assert payload["workflow"]["status"] == "paused"
    assert payload["control"]["actor"] == "testclient"
    assert payload["control"]["schedule_results"][0]["state"] == "paused"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job["id"])["enabled"] is False

    resumed = client.post(
        f"/api/plugins/runbooks/workflows/{workflow['id']}/control",
        json={
            "action": "resume",
            "expected_version": payload["workflow"]["version"],
            "confirmed": True,
        },
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["workflow"]["status"] == "active"
    with cron_jobs.use_cron_store(profile_home):
        assert cron_jobs.get_job(job["id"])["enabled"] is True

    events = client.get(
        f"/api/plugins/runbooks/events?entity_type=workflow_definition&entity_id={workflow['id']}"
    ).json()["events"]
    controls = [event for event in events if event["event_type"] == "dashboard_control"]
    assert [event["payload"]["action"] for event in controls] == ["pause", "resume"]


def test_workflow_control_restores_registry_when_linked_schedule_is_missing(client):
    created = client.put(
        "/api/plugins/runbooks/runbooks/daily-brief",
        json={"markdown": _runbook_markdown()},
    )
    workflow = created.json()["workflow"]
    response = client.post(
        f"/api/plugins/runbooks/workflows/{workflow['id']}/control",
        json={
            "action": "pause",
            "expected_version": workflow["version"],
            "confirmed": True,
        },
    )
    assert response.status_code == 500
    current = client.get(
        f"/api/plugins/runbooks/workflows/{workflow['id']}"
    ).json()["workflow"]
    assert current["status"] == "active"


def test_legacy_work_is_read_only_and_searchable(client, tmp_path):
    archive = (
        tmp_path
        / ".hermes"
        / "archives"
        / "paperclip"
        / "current"
    )
    archive.mkdir(parents=True)
    conn = sqlite3.connect(archive / "legacy-work.db")
    conn.executescript(
        """
        CREATE TABLE archive_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE legacy_entities (
            entity_type TEXT, entity_id TEXT, legacy_identifier TEXT, title TEXT,
            status TEXT, owner TEXT, updated_at TEXT, payload_json TEXT
        );
        CREATE VIRTUAL TABLE legacy_search USING fts5(
            entity_type UNINDEXED, entity_id UNINDEXED, legacy_identifier,
            title, body, owner, status, tokenize='unicode61'
        );
        """
    )
    manifest = {
        "created_at": "2026-08-07T00:00:00Z",
        "reconciliation": {
            "source_counts": {"projects": 1, "issues": 1, "issue_comments": 2},
            "count_mismatches": {},
            "foreign_key_missing_counts": {},
        },
    }
    conn.execute("INSERT INTO archive_metadata VALUES ('manifest', ?)", (json.dumps(manifest),))
    payload = {"id": "issue-1", "identifier": "EK-100", "title": "Migration history"}
    conn.execute(
        "INSERT INTO legacy_entities VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("issue", "issue-1", "EK-100", "Migration history", "done", "agent-1", "2026-08-07", json.dumps(payload)),
    )
    conn.execute(
        "INSERT INTO legacy_search VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("issue", "issue-1", "EK-100", "Migration history", "Archived task", "agent-1", "done"),
    )
    conn.execute(
        "INSERT INTO legacy_entities VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("comment", "comment-1", "comment-1", "comment-1", "", "", "2026-08-08", json.dumps({"id": "comment-1", "body": "migration note"})),
    )
    conn.execute(
        "INSERT INTO legacy_search VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("comment", "comment-1", "comment-1", "comment-1", "migration note", "", ""),
    )
    conn.commit()
    conn.close()

    response = client.get("/api/plugins/runbooks/legacy?q=migration")
    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) == 1
    assert response.json()["results"][0]["legacy_identifier"] == "EK-100"
    assert response.json()["summary"]["source_counts"]["issues"] == 1

    detail = client.get("/api/plugins/runbooks/legacy/issue/issue-1")
    assert detail.status_code == 200
    assert detail.json()["entity"]["title"] == "Migration history"
    assert client.post("/api/plugins/runbooks/legacy", json={}).status_code == 405


def test_mutations_deny_non_elliott_and_cross_origin(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = FastAPI()

    @app.middleware("http")
    async def add_session(request, call_next):
        request.state.session = SimpleNamespace(
            user_id=request.headers.get("x-test-user", "viewer")
        )
        return await call_next(request)

    app.include_router(_load_plugin_router(), prefix="/api/plugins/runbooks")
    guarded = TestClient(app)
    body = {"markdown": _runbook_markdown(), "approved_by": "spoofed"}

    denied = guarded.put(
        "/api/plugins/runbooks/runbooks/daily-brief",
        json=body,
        headers={"origin": "http://testserver"},
    )
    assert denied.status_code == 403
    cross_origin = guarded.put(
        "/api/plugins/runbooks/runbooks/daily-brief",
        json=body,
        headers={"x-test-user": "elliott", "origin": "https://attacker.invalid"},
    )
    assert cross_origin.status_code == 403
    allowed = guarded.put(
        "/api/plugins/runbooks/runbooks/daily-brief",
        json=body,
        headers={"x-test-user": "elliott", "origin": "http://testserver"},
    )
    assert allowed.status_code == 200, allowed.text
    index = json.loads(
        (home / "runbooks" / "daily-brief" / ".index.json").read_text(encoding="utf-8")
    )
    assert index["approved_by"] == "elliott"
