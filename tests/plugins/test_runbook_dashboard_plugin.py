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
    assert "Approve Save" in bundle
    assert "Start Run" in bundle
    assert '"Approver"' not in bundle
    assert '}, "Evernote")' in bundle
    assert "Schedules" in bundle
    assert "Legacy Work" in bundle


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
