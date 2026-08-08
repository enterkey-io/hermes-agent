from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_paperclip_legacy.py"


def _load_exporter():
    spec = importlib.util.spec_from_file_location("export_paperclip_legacy", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_json_value_redacts_secrets_and_environment_values() -> None:
    exporter = _load_exporter()
    value = exporter._json_value(
        {
            "model": "gpt-test",
            "apiKey": "secret-value",
            "nested": {"access_token": "token-value", "region": "local"},
            "env": {"SAFE_NAME": "private", "TOKEN": "also-private"},
            "log": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            "command": "API_KEY=abcdefghijklmnopqrstuvwxyz",
        }
    )

    serialized = json.dumps(value)
    assert "secret-value" not in serialized
    assert "token-value" not in serialized
    assert "also-private" not in serialized
    assert "abcdefghijklmnopqrstuvwxyz" not in serialized
    assert value["model"] == "gpt-test"
    assert value["env"] == {"_redacted_env_keys": ["SAFE_NAME", "TOKEN"]}


def test_heartbeat_raw_output_is_omitted() -> None:
    exporter = _load_exporter()
    row = exporter._row_dict(
        "heartbeat_runs",
        ["id", "status", "stdout_excerpt", "result_json"],
        ["run-1", "succeeded", "private output", {"result": "private result"}],
    )

    assert row["status"] == "succeeded"
    assert row["stdout_excerpt"].startswith("<omitted")
    assert row["result_json"].startswith("<omitted")


def test_legacy_index_is_searchable_and_contains_payload(tmp_path: Path) -> None:
    exporter = _load_exporter()
    path = tmp_path / "legacy-work.db"
    rows = {
        "projects": [
            {
                "id": "project-1",
                "name": "Historical launch",
                "description": "Archived project",
                "status": "completed",
                "updated_at": "2026-08-01T00:00:00Z",
            }
        ],
        "issues": [
            {
                "id": "issue-1",
                "identifier": "EK-100",
                "title": "Verify migration",
                "description": "Legacy task body",
                "status": "done",
                "assignee_agent_id": "agent-1",
                "updated_at": "2026-08-02T00:00:00Z",
            }
        ],
    }

    exporter._build_legacy_db(path, rows, {"schema_version": 1})

    conn = sqlite3.connect(path)
    result = conn.execute(
        "SELECT entity_type, legacy_identifier FROM legacy_search WHERE legacy_search MATCH ?",
        ('"migration"',),
    ).fetchone()
    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM legacy_entities WHERE entity_type='issue' AND entity_id='issue-1'"
        ).fetchone()[0]
    )
    conn.close()
    assert result == ("issue", "EK-100")
    assert payload["title"] == "Verify migration"


def test_foreign_key_checks_report_missing_targets() -> None:
    exporter = _load_exporter()
    checks = exporter._foreign_key_checks(
        {
            "companies": [{"id": "company-1"}],
            "projects": [{"id": "project-1", "company_id": "company-1"}],
            "issues": [{"id": "issue-1", "project_id": "missing-project"}],
            "issue_comments": [{"id": "comment-1", "issue_id": "issue-1"}],
        }
    )

    assert checks["projects.company_id"] == 0
    assert checks["issues.project_id"] == 1
    assert checks["issue_comments.issue_id"] == 0
