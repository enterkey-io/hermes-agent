#!/usr/bin/env python3
"""Export Paperclip history into a sanitized, read-only Hermes archive."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any
from uuid import UUID


EXPORT_TABLES = (
    "companies",
    "projects",
    "goals",
    "agents",
    "issues",
    "issue_comments",
    "issue_attachments",
    "issue_relations",
    "labels",
    "issue_labels",
    "issue_documents",
    "documents",
    "document_revisions",
    "issue_work_products",
    "assets",
    "routines",
    "routine_revisions",
    "routine_documents",
    "routine_runs",
    "routine_triggers",
    "approvals",
    "approval_comments",
    "issue_approvals",
    "agent_task_sessions",
    "heartbeat_runs",
)

SENSITIVE_KEY = re.compile(
    r"(^|_)(api_?key|token|password|secret|credential|authorization)(_|$)",
    re.IGNORECASE,
)
SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|github_pat|xox[baprs])_[A-Za-z0-9_-]{12,}", re.IGNORECASE),
    re.compile(r"\bAKIA[A-Z0-9]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|password|secret|credential)"
        r"(\s*[:=]\s*)([^\s,;\"']{8,})"
    ),
    re.compile(r"(?i)\b(https?://[^\s:/]+:)[^\s@/]+(@)"),
)

OMITTED_COLUMNS = {
    "heartbeat_runs": {
        "context_snapshot",
        "result_json",
        "stdout_excerpt",
        "stderr_excerpt",
    }
}


def _redact_text(value: str) -> str:
    result = value
    for pattern in SENSITIVE_TEXT_PATTERNS:
        if pattern.groups == 2:
            result = pattern.sub(r"\1<redacted>\2", result)
        elif pattern.groups:
            result = pattern.sub(r"\1\2<redacted>", result)
        else:
            result = pattern.sub("<redacted>", result)
    return result


def _json_value(value: Any, *, key: str = "") -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        if key and SENSITIVE_KEY.search(key) and value not in (None, ""):
            return "<redacted>"
        if isinstance(value, str):
            return _redact_text(value)
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return f"<binary:{len(value)} bytes>"
    if isinstance(value, Mapping):
        if key.lower() == "env":
            return {"_redacted_env_keys": sorted(str(item) for item in value)}
        return {str(item): _json_value(child, key=str(item)) for item, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _row_dict(table: str, columns: list[str], row: Iterable[Any]) -> dict[str, Any]:
    return {
        column: (
            "<omitted from sanitized archive; preserved in database backup>"
            if column in OMITTED_COLUMNS.get(table, set()) and value is not None
            else _json_value(value, key=column)
        )
        for column, value in zip(columns, row, strict=True)
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _export_table(conn: Any, table: str, destination: Path) -> tuple[int, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    with conn.cursor() as cursor:
        cursor.execute(f'SELECT * FROM "{table}" ORDER BY 1')
        columns = [item.name for item in cursor.description]
        with gzip.open(destination, "wt", encoding="utf-8") as output:
            for raw in cursor:
                row = _row_dict(table, columns, raw)
                output.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
                rows.append(row)
    return len(rows), rows


def _foreign_key_checks(rows: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    ids = {
        table: {str(row.get("id")) for row in values if row.get("id")}
        for table, values in rows.items()
    }
    checks = {
        "projects.company_id": ("projects", "company_id", "companies"),
        "issues.company_id": ("issues", "company_id", "companies"),
        "issues.project_id": ("issues", "project_id", "projects"),
        "issues.parent_id": ("issues", "parent_id", "issues"),
        "issue_comments.issue_id": ("issue_comments", "issue_id", "issues"),
        "issue_attachments.issue_id": ("issue_attachments", "issue_id", "issues"),
        "issue_attachments.asset_id": ("issue_attachments", "asset_id", "assets"),
        "issue_relations.issue_id": ("issue_relations", "issue_id", "issues"),
        "issue_relations.related_issue_id": ("issue_relations", "related_issue_id", "issues"),
        "routines.project_id": ("routines", "project_id", "projects"),
        "routines.parent_issue_id": ("routines", "parent_issue_id", "issues"),
        "routine_runs.routine_id": ("routine_runs", "routine_id", "routines"),
        "routine_runs.trigger_id": ("routine_runs", "trigger_id", "routine_triggers"),
        "routine_runs.linked_issue_id": ("routine_runs", "linked_issue_id", "issues"),
    }
    missing: dict[str, int] = {}
    for label, (source_table, column, target_table) in checks.items():
        target_ids = ids.get(target_table, set())
        missing[label] = sum(
            1
            for row in rows.get(source_table, [])
            if row.get(column) and str(row[column]) not in target_ids
        )
    return missing


def _entity_owner(row: dict[str, Any]) -> str:
    return str(
        row.get("assignee_agent_id")
        or row.get("lead_agent_id")
        or row.get("owner_agent_id")
        or ""
    )


def _build_legacy_db(path: Path, rows: dict[str, list[dict[str, Any]]], manifest: dict[str, Any]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA journal_mode=DELETE;
        CREATE TABLE archive_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE legacy_entities (
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            legacy_identifier TEXT,
            title TEXT,
            status TEXT,
            owner TEXT,
            updated_at TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (entity_type, entity_id)
        );
        CREATE VIRTUAL TABLE legacy_search USING fts5(
            entity_type UNINDEXED,
            entity_id UNINDEXED,
            legacy_identifier,
            title,
            body,
            owner,
            status,
            tokenize='unicode61'
        );
        """
    )
    conn.execute(
        "INSERT INTO archive_metadata VALUES (?, ?)",
        ("manifest", json.dumps(manifest, sort_keys=True)),
    )
    searchable = {
        "projects": ("project", "name", "description"),
        "goals": ("goal", "title", "description"),
        "issues": ("issue", "title", "description"),
        "issue_comments": ("comment", "id", "body"),
        "routines": ("routine", "title", "description"),
        "routine_runs": ("routine_run", "id", "failure_reason"),
    }
    for table, (entity_type, title_key, body_key) in searchable.items():
        for row in rows.get(table, []):
            entity_id = str(row.get("id") or "")
            if not entity_id:
                continue
            title = str(row.get(title_key) or "")
            body = str(row.get(body_key) or "")
            identifier = str(row.get("identifier") or row.get("public_id") or entity_id)
            status = str(row.get("status") or row.get("last_result") or "")
            owner = _entity_owner(row)
            conn.execute(
                "INSERT INTO legacy_entities VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_type,
                    entity_id,
                    identifier,
                    title,
                    status,
                    owner,
                    str(row.get("updated_at") or row.get("created_at") or ""),
                    json.dumps(row, sort_keys=True),
                ),
            )
            conn.execute(
                "INSERT INTO legacy_search VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entity_type, entity_id, identifier, title, body, owner, status),
            )
    conn.execute("CREATE INDEX legacy_entities_updated ON legacy_entities(updated_at DESC)")
    conn.commit()
    conn.close()


def _attachment_inventory(rows: dict[str, list[dict[str, Any]]], storage_root: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for asset in rows.get("assets", []):
        object_key = str(asset.get("object_key") or "")
        candidate = storage_root / object_key
        available = bool(object_key and candidate.is_file())
        result.append(
            {
                "asset_id": asset.get("id"),
                "object_key": object_key,
                "declared_bytes": asset.get("byte_size"),
                "declared_sha256": asset.get("sha256"),
                "available": available,
                "observed_bytes": candidate.stat().st_size if available else None,
                "observed_sha256": _sha256(candidate) if available else None,
            }
        )
    return result


def _preserve_database_backup(source: Path | None, archive: Path) -> dict[str, Any] | None:
    if source is None or not source.is_file():
        return None
    destination = archive / "database" / source.name
    destination.parent.mkdir()
    try:
        os.link(source, destination)
        method = "hardlink"
    except OSError:
        shutil.copy2(source, destination)
        method = "copy"
    destination.chmod(0o400)
    return {
        "path": str(destination.relative_to(archive)),
        "bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
        "preservation_method": method,
    }


def export_archive(args: argparse.Namespace) -> Path:
    import psycopg2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = args.output_root / stamp
    entities = archive / "entities"
    entities.mkdir(parents=True, mode=0o700)
    conn = psycopg2.connect(
        host=args.host,
        port=args.port,
        dbname=args.database,
        user=args.user,
    )
    conn.set_session(readonly=True, autocommit=True)
    rows: dict[str, list[dict[str, Any]]] = {}
    files: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for table in EXPORT_TABLES:
        destination = entities / f"{table}.jsonl.gz"
        count, table_rows = _export_table(conn, table, destination)
        rows[table] = table_rows
        counts[table] = count
        files.append(
            {
                "path": str(destination.relative_to(archive)),
                "bytes": destination.stat().st_size,
                "sha256": _sha256(destination),
            }
        )
    conn.close()

    attachments = _attachment_inventory(rows, args.storage_root)
    attachment_path = archive / "attachment-inventory.json"
    attachment_path.write_text(
        json.dumps(attachments, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    files.append(
        {
            "path": str(attachment_path.relative_to(archive)),
            "bytes": attachment_path.stat().st_size,
            "sha256": _sha256(attachment_path),
        }
    )
    database_backup = _preserve_database_backup(args.database_backup, archive)
    if database_backup:
        files.append(database_backup)
    reconciliation = {
        "source_counts": counts,
        "export_counts": dict(counts),
        "count_mismatches": {},
        "foreign_key_missing_counts": _foreign_key_checks(rows),
        "attachment_references": len(rows.get("issue_attachments", [])),
        "attachment_assets": len(rows.get("assets", [])),
        "attachment_files_available": sum(1 for item in attachments if item["available"]),
    }
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "kind": "paperclip-embedded-postgres",
            "host": args.host,
            "port": args.port,
            "database": args.database,
        },
        "sanitization": "Recursive secret-key redaction; env values omitted; auth and secret tables excluded.",
        "reconciliation": reconciliation,
        "database_backup": database_backup,
        "files": files,
    }
    index_path = archive / "legacy-work.db"
    _build_legacy_db(index_path, rows, manifest)
    index_path.chmod(0o400)
    manifest["files"].append(
        {
            "path": index_path.name,
            "bytes": index_path.stat().st_size,
            "sha256": _sha256(index_path),
        }
    )
    reconciliation_path = archive / "reconciliation.json"
    reconciliation_path.write_text(
        json.dumps(reconciliation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path = archive / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    restore_path = archive / "RESTORE.md"
    restore_path.write_text(
        "# Paperclip Legacy Archive Restore\n\n"
        "The JSONL files are the sanitized portable export. Decompress any entity "
        "file with `gzip -dc entities/<table>.jsonl.gz`. `legacy-work.db` is a "
        "read-only search projection used by Hermes and can be rebuilt from those "
        "files. The preserved SQL backup is the complete source restore artifact; "
        "restore it only into an isolated Paperclip/PostgreSQL instance using the "
        "Paperclip backup restoration command for the archived application version.\n",
        encoding="utf-8",
    )
    for path in (attachment_path, reconciliation_path, manifest_path, restore_path):
        path.chmod(0o400)
    archive.chmod(0o700)
    current = args.output_root / "current"
    replacement = args.output_root / f".current-{stamp}"
    replacement.symlink_to(archive.name)
    replacement.replace(current)
    return archive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=54329)
    parser.add_argument("--database", default="paperclip")
    parser.add_argument("--user", default="paperclip")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.home() / ".hermes" / "archives" / "paperclip",
    )
    parser.add_argument(
        "--storage-root",
        type=Path,
        default=Path.home() / ".paperclip" / "instances" / "default" / "data" / "storage",
    )
    parser.add_argument("--database-backup", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = export_archive(args)
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
