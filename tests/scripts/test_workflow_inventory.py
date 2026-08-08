from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "workflow_inventory.py"


def _load_inventory():
    spec = importlib.util.spec_from_file_location("workflow_inventory", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_inventory_redacts_secrets_and_excludes_archives_from_active_count(tmp_path: Path) -> None:
    inv = _load_inventory()
    hermes_root = tmp_path / ".hermes"
    profile = hermes_root / "profiles" / "grace"
    _write_json(
        profile / "cron" / "jobs.json",
        [
            {
                "id": "morning",
                "name": "Morning Message",
                "schedule": "0 8 * * *",
                "prompt": "Use Paperclip only as historical evidence. token=sk-abcdefghijklmnopqrstuvwxyz123456",
                "deliver": "telegram",
            }
        ],
    )
    (profile / "AGENTS.md").write_text(
        "Active runtime says call Paperclip at PAPERCLIP_API_TOKEN=super-secret-token-value",
        encoding="utf-8",
    )
    archive = profile / "conversations" / "daily"
    archive.mkdir(parents=True)
    (archive / "2026-08-07.md").write_text(
        "Historical Paperclip transcript with PAPERCLIP_API_TOKEN=should-not-count-active",
        encoding="utf-8",
    )
    paperclip_root = tmp_path / ".paperclip"
    (paperclip_root / "plugins").mkdir(parents=True)
    (paperclip_root / "plugins" / "package.json").write_text("{}", encoding="utf-8")

    options = inv.InventoryOptions(
        hermes_root=hermes_root,
        scan_roots=[hermes_root],
        paperclip_roots=[paperclip_root],
        output_dir=tmp_path / "reports",
        skip_host_commands=True,
    )

    inventory = inv.build_inventory(options)
    rendered = json.dumps(inventory)

    assert inventory["counts"]["profiles"] == 1
    assert inventory["counts"]["cron_jobs"] == 1
    assert inventory["counts"]["enabled_cron_jobs"] == 1
    assert inventory["paperclip_active_dependencies"]["active_dependency_count"] == 2
    assert "super-secret-token-value" not in rendered
    assert "should-not-count-active" not in rendered
    assert "<redacted>" in rendered
    assert inventory["notification_path_inventory"]["notification_path_count"] >= 1


def test_reports_are_written_with_expected_names(tmp_path: Path) -> None:
    inv = _load_inventory()
    hermes_root = tmp_path / ".hermes"
    _write_json(hermes_root / "profiles" / "main" / "cron" / "jobs.json", [])

    options = inv.InventoryOptions(
        hermes_root=hermes_root,
        scan_roots=[hermes_root],
        paperclip_roots=[tmp_path / "missing-paperclip"],
        output_dir=tmp_path / "reports",
        skip_host_commands=True,
    )
    inventory = inv.build_inventory(options)
    inv.write_reports(inventory, options.output_dir)

    for name in inv.DEFAULT_REPORT_NAMES:
        assert (options.output_dir / name).exists(), name

    assert "Workflow Inventory" in (options.output_dir / "workflow-inventory.md").read_text(
        encoding="utf-8"
    )
    assert "Active Automation Authority Map" in (
        options.output_dir / "active-automation-authority-map.md"
    ).read_text(encoding="utf-8")


def test_detects_enabled_schedule_collisions(tmp_path: Path) -> None:
    inv = _load_inventory()
    hermes_root = tmp_path / ".hermes"
    job = {
        "id": "digest",
        "name": "Digest",
        "schedule": "0 8 * * *",
        "prompt": "Summarize.",
        "deliver": "email",
    }
    _write_json(hermes_root / "profiles" / "grace" / "cron" / "jobs.json", [job])
    _write_json(
        hermes_root / "profiles" / "aurora" / "cron" / "jobs.json",
        [dict(job, id="digest-2")],
    )

    inventory = inv.build_inventory(
        inv.InventoryOptions(
            hermes_root=hermes_root,
            scan_roots=[hermes_root],
            paperclip_roots=[],
            output_dir=tmp_path / "reports",
            skip_host_commands=True,
        )
    )

    collisions = inventory["schedule_collision_report"]["collisions"]
    assert len(collisions) == 1
    assert {source["profile"] for source in collisions[0]["sources"]} == {"grace", "aurora"}


def test_inventory_reports_runbook_registration_and_migration_candidates(tmp_path: Path) -> None:
    inv = _load_inventory()
    hermes_root = tmp_path / ".hermes"
    _write_json(
        hermes_root / "profiles" / "grace" / "cron" / "jobs.json",
        [
            {"id": "linked", "name": "Linked", "enabled": True, "workflow_id": "wf-1"},
            {"id": "missing", "name": "Missing", "enabled": True},
        ],
    )
    runbook = hermes_root / "runbooks" / "linked" / "RUNBOOK.md"
    runbook.parent.mkdir(parents=True)
    runbook.write_text("# fixture\n", encoding="utf-8")
    _write_json(
        hermes_root / "runbook-migrations" / "evernote.json",
        {"candidates": [{"source": "old.md", "classification": "historical-superseded"}]},
    )

    inventory = inv.build_inventory(
        inv.InventoryOptions(
            hermes_root=hermes_root,
            scan_roots=[hermes_root],
            paperclip_roots=[],
            output_dir=tmp_path / "reports",
            skip_host_commands=True,
        )
    )

    assert inventory["counts"]["canonical_runbooks"] == 1
    assert inventory["counts"]["registered_enabled_cron_jobs"] == 1
    assert inventory["counts"]["unregistered_enabled_cron_jobs"] == 1
    assert inventory["counts"]["runbook_migration_candidates"] == 1
    assert inventory["runbook_registry"]["migration_dispositions"] == {
        "historical-superseded": 1
    }


def test_inventory_separates_retained_and_archived_external_workflows(tmp_path: Path) -> None:
    inv = _load_inventory()
    hermes_root = tmp_path / ".hermes"
    hermes_root.mkdir()
    db_path = hermes_root / "workflow_registry.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE workflow_definitions (
            id TEXT, slug TEXT, name TEXT, owner_profile TEXT, status TEXT,
            runtime_kind TEXT, runtime_ref TEXT, source_path TEXT,
            source_hash TEXT, version INTEGER, updated_at TEXT
        )"""
    )
    conn.executemany(
        "INSERT INTO workflow_definitions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("active", "active-external", "Active", "grace", "active", "n8n", "n8n:1", "", "", 1, ""),
            ("retired", "retired-sim", "Retired", "grace", "retired", "sim", "sim:not-retained", "", "", 1, ""),
        ],
    )
    for table in ("workflow_steps", "workflow_schedules", "workflow_runs", "workflow_step_runs"):
        conn.execute(f"CREATE TABLE {table} (id TEXT)")
    conn.commit()
    conn.close()

    inventory = inv.build_inventory(
        inv.InventoryOptions(
            hermes_root=hermes_root,
            scan_roots=[hermes_root],
            paperclip_roots=[],
            output_dir=tmp_path / "reports",
            skip_host_commands=True,
        )
    )

    assert inventory["counts"]["retained_external_runtime_workflows"] == 1
    assert inventory["counts"]["archived_external_runtime_workflows"] == 1
    assert [item["slug"] for item in inventory["runbook_registry"]["archived_external_runtime_definitions"]] == ["retired-sim"]
