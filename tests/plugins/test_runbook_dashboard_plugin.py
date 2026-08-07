"""Tests for the runbooks dashboard plugin backend."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
    assert 'SDK.registerPlugin("runbooks", RunbooksApp);' in bundle
    assert "Approve Save" in bundle
    assert "Start Run" in bundle
