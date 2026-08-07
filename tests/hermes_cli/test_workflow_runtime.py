from __future__ import annotations

from pathlib import Path

from hermes_cli import runbook_store
from hermes_cli import workflow_registry as reg
from hermes_cli.workflow_runtime import (
    build_runbook_agent_prompt,
    sync_runbook_cron_jobs,
)
from hermes_constants import get_hermes_home


def _metadata() -> dict:
    return {
        "id": "wf-daily-brief",
        "slug": "daily-brief",
        "title": "Daily Brief",
        "purpose": "Prepare a concise daily brief.",
        "owner_profile": "default",
        "status": "active",
        "runtime": {"kind": "hermes", "ref": "gateway"},
        "schedules": [
            {
                "id": "morning",
                "profile": "default",
                "schedule": "every 1h",
                "deliver": "local",
                "step_key": "collect",
            }
        ],
        "steps": [
            {
                "step_key": "collect",
                "name": "Collect context",
                "description": "Read current operating context.",
            }
        ],
        "inputs": {},
        "outputs": {},
        "permitted_writes": [],
        "approval_rules": {},
        "retry": {"max_attempts": 2},
        "timeout": {"seconds": 1800},
        "deduplication": {"strategy": "date"},
        "related": {},
    }


def _save_runbook() -> None:
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: openrouter\n"
        "  default: test-model\n"
        "agent:\n"
        "  reasoning_effort: medium\n"
        "  speed: standard\n",
        encoding="utf-8",
    )
    runbook_store.save_runbook(
        _metadata(),
        "# Daily Brief\n\n## Procedure\n\n1. Collect context.\n",
        approved_by="dashboard",
    )
    with reg.connect_closing() as conn:
        reg.create_definition(
            conn,
            id="wf-daily-brief",
            slug="daily-brief",
            name="Daily Brief",
            owner_profile="default",
            status="active",
            runtime_kind="hermes",
        )
        reg.replace_steps(
            conn,
            "wf-daily-brief",
            [{"step_key": "collect", "position": 0, "name": "Collect context"}],
        )


def test_build_runbook_agent_prompt_contains_protocol() -> None:
    _save_runbook()

    prompt = build_runbook_agent_prompt("daily-brief", step_key="collect")

    assert "Run Hermes workflow `daily-brief`." in prompt
    assert "Execute step `collect`: Collect context" in prompt
    assert "[WORKFLOW_STATUS:completed]" in prompt
    assert "[WORKFLOW_STATUS:blocked]" in prompt
    assert "## Procedure" in prompt


def test_sync_runbook_cron_jobs_creates_profile_job_and_registry_link() -> None:
    _save_runbook()

    synced = sync_runbook_cron_jobs("daily-brief")

    assert len(synced) == 1
    job = synced[0]
    assert job["profile"] == "default"
    assert job["workflow_id"] == "wf-daily-brief"
    assert job["workflow_step_key"] == "collect"
    assert job["workflow_schedule_id"] == "morning"
    assert job["track_workflow_status"] is True
    assert "RUNBOOK.md" in job["prompt"]

    from cron.jobs import list_jobs

    jobs = list_jobs(include_disabled=True)
    assert jobs[0]["id"] == job["id"]

    with reg.connect_closing() as conn:
        rows = conn.execute("SELECT * FROM workflow_schedules").fetchall()
    assert len(rows) == 1
    assert rows[0]["cron_job_id"] == job["id"]


def test_sync_runbook_cron_jobs_updates_existing_job() -> None:
    _save_runbook()
    first = sync_runbook_cron_jobs("daily-brief")[0]
    metadata = _metadata()
    metadata["schedules"][0]["schedule"] = "every 2h"
    runbook_store.save_runbook(
        metadata,
        "# Daily Brief\n\n## Procedure\n\n1. Collect updated context.\n",
        approved_by="dashboard",
    )

    second = sync_runbook_cron_jobs("daily-brief")[0]

    assert second["id"] == first["id"]
    assert second["schedule_display"] == "every 120m"
    assert "Collect updated context" in second["prompt"]
