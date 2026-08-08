#!/usr/bin/env python3
"""Register audited external workflow engines in the Hermes registry."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import runbook_store
from hermes_cli.runbook_projection import project_runbook


SIM_ROOT = Path(
    "/home/elliott/.hermes/profiles/grace/profiles/grace/skills/"
    "productivity/plaud-sim-workflow"
)


def main() -> int:
    skill = SIM_ROOT / "SKILL.md"
    workflow = SIM_ROOT / "workflow" / "plaud-grace-evernote-nirvana.workflow.json"
    metadata = {
        "id": "wf_external_grace_plaud_sim",
        "slug": "grace-plaud-sim-workflow",
        "title": "Grace Plaud Sim Workflow",
        "purpose": "Preserve the audited Plaud Sim workflow definition as historical reference; this execution path is not retained.",
        "owner_profile": "grace",
        "status": "retired",
        "runtime": {"kind": "sim", "ref": "sim:not-retained"},
        "schedules": [],
        "steps": [
            {"step_key": "discover", "name": "Discover eligible Plaud recording", "executor_profile": "grace"},
            {"step_key": "transcribe", "name": "Attest source and transcribe", "executor_profile": "grace"},
            {"step_key": "analyze", "name": "Classify and verify analysis", "executor_profile": "grace"},
            {"step_key": "evernote", "name": "Persist and verify Evernote note", "executor_profile": "grace"},
            {"step_key": "nirvana", "name": "Persist and verify Elliott-owned actions", "executor_profile": "grace"},
        ],
        "inputs": {"source": "official Plaud MCP stable file IDs"},
        "outputs": {"destinations": ["Evernote", "Nirvana"]},
        "permitted_writes": ["Guarded connector CAS operations only"],
        "approval_rules": {
            "current": "Retired. Do not activate, import, schedule, or execute this Sim workflow."
        },
        "retry": {"max_attempts": 1},
        "timeout": {},
        "deduplication": {"strategy": "stable-plaud-file-id-and-connector-ledger"},
        "related": {
            "source_workflow": str(workflow),
            "source_sha256": hashlib.sha256(workflow.read_bytes()).hexdigest(),
            "schedule_authorized": False,
            "deployment_manifest": "unconfigured",
            "retirement_disposition": "not-retained",
            "retired_at": "2026-08-07",
            "code_and_data_preserved": True,
            "services": [
                "plaud-sim-connector.service",
                "plaud-sim-egress.service",
                "plaud-sim-plaud-broker.service",
                "plaud-sim-public-mcp.service",
                "plaud-sim-tunnel.service",
            ],
            "audit": "Services disabled 2026-08-07; code and data preserved. Prior 2026-07-31 audio egress failed with HTTP 403.",
        },
    }
    body = (
        "# Grace Plaud Sim Workflow\n\n"
        "> Registry status: retired. This Sim path was not retained. Its services "
        "were disabled on 2026-08-07; code and data are preserved for historical "
        "reference only. Do not activate, import, schedule, or execute it.\n\n"
        + skill.read_text(encoding="utf-8").split("---\n", 2)[-1].lstrip()
    )
    record = runbook_store.save_runbook(
        metadata,
        body,
        approved_by="system-admin-external-workflow-audit-2026-08-07",
    )
    workflow_record = project_runbook(record)
    print(f"registered {workflow_record['slug']} ({workflow_record['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
